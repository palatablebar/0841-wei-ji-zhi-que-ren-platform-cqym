"""危急值通知闭环测试。

对应业务要求：
- 判定后为每条危急值建通知，登记接收医师、首次通知时间、回执期限；
- 回执必须带读回内容才算闭环，仅“已通知”不闭环；
- 同一危急值重跑/重复通知不新增待回执任务，不删除已有回执与升级；
- 交班把未回执任务责任人换成当班医师，但首次通知时间与升级记录不重来；
- 逾期者交班补一次升级，未到期者只换责任人。
"""

import pytest


def make_run(client, profile="dirty"):
    resp = client.post("/api/runs", json={"profile": profile})
    assert resp.status_code == 200
    return resp.get_json()


def notify_run(client, run_id, **extra):
    body = {"doctor": "王医师", "notified_at": "2026-11-20T09:00:00",
            "due_at": "2026-11-20T09:30:00"}
    body.update(extra)
    resp = client.post(f"/api/runs/{run_id}/notifications", json=body)
    assert resp.status_code == 200
    return resp.get_json()


def test_each_critical_value_creates_notification(client):
    run = make_run(client)
    env = notify_run(client, run["run_id"])
    # dirty 样例有 P7(GLU) 与 P9(HGB) 两条危急值
    assert env["total"] == 2
    keys = {n["specimen_id"] for n in env["notifications"]}
    assert {"P7", "P9"} <= keys
    for n in env["notifications"]:
        assert n["receiver"] == "王医师"
        assert n["first_notified_at"] == "2026-11-20T09:00:00"
        assert n["due_at"] == "2026-11-20T09:30:00"
        assert n["status"] == "PENDING"
        assert n["closed"] is False


def test_clean_sample_has_no_critical_notifications(client):
    run = make_run(client, "clean")
    env = client.post(f"/api/runs/{run['run_id']}/notifications",
                      json={"doctor": "王医师"}).get_json()
    assert env["total"] == 0


def test_notified_alone_is_not_closed(client):
    run = make_run(client)
    notify_run(client, run["run_id"])
    n = client.get("/api/notifications/P7").get_json()
    assert n["status"] == "PENDING"
    assert n["closed"] is False
    assert not n["readback"]


def test_blank_readback_does_not_close(client):
    run = make_run(client)
    notify_run(client, run["run_id"])
    for blank in ("", "   ", None):
        resp = client.post("/api/notifications/P7/receipt",
                           json={"doctor": "陈护士", "readback": blank})
        assert resp.status_code in (400, 422)
    n = client.get("/api/notifications/P7").get_json()
    assert n["status"] == "PENDING"
    assert n["closed"] is False


def test_receipt_with_readback_closes_loop(client):
    run = make_run(client)
    notify_run(client, run["run_id"])
    resp = client.post("/api/notifications/P7/receipt",
                       json={"doctor": "陈护士", "readback": "复述：GLU 30 mmol/L"})
    assert resp.status_code == 200
    n = resp.get_json()
    assert n["closed"] is True
    assert n["status"] == "CLOSED"
    assert "GLU 30" in n["readback"]
    assert n["receipt"]["readback"]
    # 列表中也体现闭环
    listed = client.get("/api/notifications/P7").get_json()
    assert listed["closed"] is True


def test_rerun_and_duplicate_notify_do_not_add_tasks(client):
    run = make_run(client)
    first = notify_run(client, run["run_id"])
    assert first["total"] == 2
    # 重新执行批次（run 幂等）并再次通知
    make_run(client)
    again = notify_run(client, run["run_id"])
    assert again["total"] == 2
    listing = client.get("/api/notifications").get_json()
    assert listing["total"] == 2


def test_duplicate_notify_preserves_receipt(client):
    run = make_run(client)
    notify_run(client, run["run_id"])
    client.post("/api/notifications/P7/receipt",
                json={"readback": "已复述危急值"})
    # 重复通知
    notify_run(client, run["run_id"], doctor="王医师")
    n = client.get("/api/notifications/P7").get_json()
    assert n["closed"] is True
    assert n["readback"] == "已复述危急值"
    assert len(n["receipts"]) == 1


def test_duplicate_notify_preserves_escalations(client):
    run = make_run(client)
    notify_run(client, run["run_id"])
    client.post("/api/notifications/P9/handover",
                json={"doctor": "李当班", "at": "2026-11-20T10:00:00"})
    before = client.get("/api/notifications/P9").get_json()
    assert len(before["escalations"]) == 1
    notify_run(client, run["run_id"])
    after = client.get("/api/notifications/P9").get_json()
    assert len(after["escalations"]) == 1
    assert after["owner"] == "李当班"


def test_handover_reassigns_pending_but_keeps_first_time(client):
    run = make_run(client)
    notify_run(client, run["run_id"])
    resp = client.post("/api/notifications/P7/handover",
                       json={"doctor": "李当班", "at": "2026-11-20T09:10:00"})
    n = resp.get_json()
    assert n["owner"] == "李当班"
    assert n["receiver"] == "王医师"  # 首次接收医师不变
    assert n["first_notified_at"] == "2026-11-20T09:00:00"
    assert n["due_at"] == "2026-11-20T09:30:00"
    assert len(n["escalations"]) == 0  # 未到期，不升级


def test_overdue_notification_gets_one_escalation_on_handover(client):
    run = make_run(client)
    notify_run(client, run["run_id"])
    resp = client.post("/api/notifications/P9/handover",
                       json={"doctor": "李当班", "at": "2026-11-20T10:00:00"})
    n = resp.get_json()
    assert n["owner"] == "李当班"
    assert len(n["escalations"]) == 1
    # 首次通知计时不重来
    assert n["first_notified_at"] == "2026-11-20T09:00:00"


def test_not_due_on_handover_only_changes_owner(client):
    run = make_run(client)
    notify_run(client, run["run_id"], due_at="2026-11-25T09:30:00")
    resp = client.post("/api/notifications/P7/handover",
                       json={"doctor": "李当班", "at": "2026-11-20T10:00:00"})
    n = resp.get_json()
    assert n["owner"] == "李当班"
    assert n["overdue"] is False
    assert n["escalations"] == []


def test_global_handover_only_touches_unreceipted(client):
    run = make_run(client)
    notify_run(client, run["run_id"])
    client.post("/api/notifications/P7/receipt", json={"readback": "闭环读回"})
    resp = client.post("/api/handover",
                       json={"doctor": "赵当班", "at": "2026-11-20T10:00:00"})
    body = resp.get_json()
    assert body["handed_over"] == 1
    p7 = client.get("/api/notifications/P7").get_json()
    p9 = client.get("/api/notifications/P9").get_json()
    assert p7["owner"] == "王医师" and p7["closed"] is True
    assert p9["owner"] == "赵当班"
    assert len(p9["escalations"]) == 1  # 09:30 期限，10:00 已逾期


def test_repeated_handover_keeps_escalation_history(client):
    run = make_run(client)
    notify_run(client, run["run_id"])
    client.post("/api/notifications/P9/handover",
                json={"doctor": "李", "at": "2026-11-20T10:00:00"})
    client.post("/api/notifications/P9/handover",
                json={"doctor": "赵", "at": "2026-11-20T11:00:00"})
    n = client.get("/api/notifications/P9").get_json()
    assert n["owner"] == "赵"
    assert n["first_notified_at"] == "2026-11-20T09:00:00"
    assert len(n["escalations"]) == 2  # 升级历史累积，不重置
    assert len(n["handovers"]) == 2


def test_run_payload_contains_notifications(client):
    run = make_run(client)
    notify_run(client, run["run_id"])
    fetched = client.get(f"/api/runs/{run['run_id']}").get_json()
    assert fetched["notification_count"] == 2
    assert len(fetched["notifications"]) == 2


def test_service_layer_idempotent_on_tmp_store(tmp_path):
    from app.notify.store import NotifyStore
    from app.notify import service

    store = NotifyStore(tmp_path / "n.sqlite3")
    payload = {"rows": [
        {"specimen_id": "Q1", "reason": "CRITICAL_VALUE", "verdict": "REJECT", "value": 99},
    ]}
    body = {"doctor": "林医师", "notified_at": "2026-11-20T09:00:00",
            "due_at": "2026-11-20T09:10:00"}
    first = service.notify_critical_values(store, payload, body)
    second = service.notify_critical_values(store, payload, body)
    assert len(first) == len(second) == 1
    service.record_receipt(store, "Q1", {"readback": "读回 X"})
    service.notify_critical_values(store, payload, body)
    q1 = service.get_notification(store, "Q1")
    assert q1["closed"] is True and q1["readback"] == "读回 X"
    out = service.handover(store, {"doctor": "交班医师", "now": "2026-11-20T12:00:00"})
    # 已回执，不再有可交班任务
    assert out["handed_over"] == 0
