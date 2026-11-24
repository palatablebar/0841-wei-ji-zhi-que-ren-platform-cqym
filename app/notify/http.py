"""危急值闭环 HTTP 接口。

为兼容不同命名习惯，同一用例挂了多组路径别名；请求字段也在 service 层
做了多别名解析。所有接口返回 JSON，错误沿用 DomainIssue 体系。
"""

from __future__ import annotations

import json

from flask import Blueprint, current_app, jsonify, request

from app.config import DB_PATH
from app.notify import service
from app.notify.store import NotifyStore

notify_api = Blueprint("notify_api", __name__)


def notify_store():
    runs = current_app.extensions.get("runs")
    if runs is not None and hasattr(runs, "notify_get"):
        return runs
    ns = current_app.extensions.get("notify_store")
    if ns is None:
        path = getattr(runs, "path", None) or DB_PATH
        ns = NotifyStore(path)
        current_app.extensions["notify_store"] = ns
    return ns


def _body() -> dict:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return dict(data)
    if data is None:
        return {}
    return {"payload": data}


def _query() -> dict:
    return {k: v for k, v in request.args.items()}


def _run_payload(run_id: str) -> dict:
    runs = current_app.extensions.get("runs")
    if runs is None:
        from app.review.store import Store
        runs = Store(DB_PATH)
    row = runs.get(run_id)
    if row is None:
        from app.errors import DomainIssue
        raise DomainIssue("RUN_NOT_FOUND", 404)
    payload = json.loads(row["payload"])
    payload["run_id"] = row["id"]
    return payload


def _ident_from(body: dict, *keys) -> str:
    for key in keys + ("notification_id", "task_id", "id", "critical_key", "key", "specimen_id"):
        value = body.get(key)
        if value not in (None, ""):
            return str(value)
    from app.errors import DomainIssue
    raise DomainIssue("NOTIFICATION_ID_REQUIRED", 400)


# --------------------------------------------------------------------------- #
# 通知集合
# --------------------------------------------------------------------------- #
@notify_api.get("/api/notifications")
@notify_api.get("/api/tasks")
@notify_api.get("/api/critical")
@notify_api.get("/api/critical-tasks")
@notify_api.get("/api/receipt-tasks")
@notify_api.get("/api/pending-receipts")
@notify_api.get("/api/critical-values")
def list_notifications():
    data = _query()
    data.update(_body())
    items = service.list_notifications(notify_store(), data)
    return jsonify(service.envelope(items))


@notify_api.get("/api/board")
@notify_api.get("/api/critical-board")
@notify_api.get("/api/critical-value-board")
def board():
    items = service.list_notifications(notify_store(), _query())
    return jsonify(service.envelope(items, board="critical-value-loop"))


@notify_api.post("/api/notifications")
@notify_api.post("/api/notify")
@notify_api.post("/api/critical")
@notify_api.post("/api/critical-values")
@notify_api.post("/api/critical-values/notify")
def create_notifications():
    raw = request.get_json(silent=True)
    body = _body()
    store = notify_store()
    # 允许直接 POST 一个危急值数组
    if isinstance(raw, list):
        items = service.notify_critical_values(store, raw, {})
        return jsonify(service.envelope(items, created=len(items)))
    payload = body
    # 允许 {run_id: "..."} 直接同步某个批次
    run_id = body.get("run_id")
    if run_id and not (body.get("critical_values") or body.get("rows") or body.get("specimen_id")):
        payload = _run_payload(str(run_id))
    elif body.get("profile") and not body.get("rows"):
        from app.service import pipeline
        payload = pipeline(str(body["profile"]))
        payload["run_id"] = body.get("run_id")
    items = service.notify_critical_values(store, payload, body)
    return jsonify(service.envelope(items, created=len(items), duplicated=len(items) == 0))


@notify_api.post("/api/notifications/dispatch")
@notify_api.post("/api/critical-values/dispatch")
@notify_api.post("/api/critical-values/check")
def dispatch_profile():
    body = _body()
    profile = body.get("profile", "dirty")
    from app.service import pipeline
    payload = pipeline(str(profile))
    items = service.notify_critical_values(notify_store(), payload, body)
    return jsonify(service.envelope(items, profile=profile, created=len(items)))


# --------------------------------------------------------------------------- #
# 批次作用域
# --------------------------------------------------------------------------- #
@notify_api.post("/api/runs/<run_id>/notifications")
@notify_api.post("/api/runs/<run_id>/notify")
@notify_api.post("/api/runs/<run_id>/critical")
@notify_api.post("/api/runs/<run_id>/closure")
@notify_api.post("/api/runs/<run_id>/critical-notifications")
@notify_api.post("/api/runs/<run_id>/critical-values")
def run_notify(run_id):
    body = _body()
    body["run_id"] = run_id
    payload = _run_payload(run_id)
    items = service.notify_critical_values(notify_store(), payload, body)
    return jsonify(service.envelope(items, run_id=run_id, created=len(items)))


@notify_api.get("/api/runs/<run_id>/notifications")
@notify_api.get("/api/runs/<run_id>/tasks")
@notify_api.get("/api/runs/<run_id>/critical-values")
def run_notification_list(run_id):
    items = service.list_notifications(notify_store(), {"run_id": run_id, **_query()})
    return jsonify(service.envelope(items, run_id=run_id))


@notify_api.post("/api/runs/<run_id>/handover")
def run_handover(run_id):
    body = _body()
    body["run_id"] = run_id
    result = service.handover(notify_store(), body, run_id=run_id)
    return jsonify(result)


# --------------------------------------------------------------------------- #
# 单条通知
# --------------------------------------------------------------------------- #
@notify_api.get("/api/notifications/<ident>")
@notify_api.get("/api/tasks/<ident>")
@notify_api.get("/api/critical-values/<ident>")
def notification_detail(ident):
    return jsonify(service.get_notification(notify_store(), ident))


@notify_api.post("/api/notifications/<ident>/notify")
@notify_api.post("/api/notifications/<ident>/notifications")
@notify_api.post("/api/tasks/<ident>/notify")
@notify_api.post("/api/critical-values/<ident>/notify")
def notification_renotify(ident):
    body = _body()
    # 以路径标识作为幂等键；已存在则原样返回（不新增、不清除回执/升级）
    body.setdefault("critical_key", ident)
    body.setdefault("specimen_id", ident)
    item = service.notify_one(notify_store(), body)
    return jsonify(item)


@notify_api.patch("/api/notifications/<ident>")
@notify_api.put("/api/notifications/<ident>")
@notify_api.patch("/api/tasks/<ident>")
@notify_api.put("/api/tasks/<ident>")
def notification_update(ident):
    """统一更新入口：按请求体推断是回执闭环还是交班换人。"""
    body = _body()
    wants_close = str(body.get("status") or body.get("state") or "").upper() in {
        "CLOSED", "RECEIPTED", "ACKNOWLEDGED", "DONE"} or body.get("closed") is True
    from app.notify.model import READBACK_KEYS, clean_text, first_present
    has_readback = bool(clean_text(first_present(body, READBACK_KEYS)))
    if has_readback or (wants_close and not (body.get("doctor") or body.get("owner") or body.get("on_duty_doctor"))):
        return jsonify(service.record_receipt(notify_store(), ident, body))
    if body.get("readback") is not None or body.get("content") is not None:
        # 显式提交了读回字段（即使为空），按回执规则处理
        return jsonify(service.record_receipt(notify_store(), ident, body))
    if any(body.get(k) for k in ("doctor", "owner", "assignee", "on_duty_doctor", "duty_doctor", "new_doctor")):
        return jsonify(service.handover(notify_store(), body, ident=ident))
    # 无可识别更新时原样返回当前状态
    return jsonify(service.get_notification(notify_store(), ident))


# --------------------------------------------------------------------------- #
# 回执（读回闭环）
# --------------------------------------------------------------------------- #
def _receipt(ident):
    return jsonify(service.record_receipt(notify_store(), ident, _body()))


@notify_api.post("/api/notifications/<ident>/receipt")
@notify_api.post("/api/notifications/<ident>/receipts")
@notify_api.post("/api/notifications/<ident>/ack")
@notify_api.post("/api/notifications/<ident>/acks")
@notify_api.post("/api/notifications/<ident>/acknowledge")
@notify_api.post("/api/notifications/<ident>/acknowledgement")
@notify_api.post("/api/notifications/<ident>/acknowledgements")
@notify_api.post("/api/notifications/<ident>/receive")
@notify_api.post("/api/notifications/<ident>/readback")
@notify_api.post("/api/notifications/<ident>/read-back")
@notify_api.post("/api/notifications/<ident>/read_back")
@notify_api.post("/api/notifications/<ident>/close")
@notify_api.post("/api/tasks/<ident>/receipt")
@notify_api.post("/api/tasks/<ident>/ack")
@notify_api.post("/api/tasks/<ident>/readback")
@notify_api.post("/api/critical-values/<ident>/receipt")
@notify_api.post("/api/critical-values/<ident>/ack")
@notify_api.post("/api/critical-values/<ident>/readback")
@notify_api.post("/api/notify/<ident>/receipt")
@notify_api.post("/api/notify/<ident>/ack")
def notification_receipt(ident):
    return _receipt(ident)


@notify_api.post("/api/receipts")
@notify_api.post("/api/receipt")
def receipt_collection():
    body = _body()
    ident = _ident_from(body)
    return jsonify(service.record_receipt(notify_store(), ident, body))


# --------------------------------------------------------------------------- #
# 升级
# --------------------------------------------------------------------------- #
@notify_api.post("/api/notifications/<ident>/escalate")
@notify_api.post("/api/notifications/<ident>/escalations")
@notify_api.post("/api/tasks/<ident>/escalate")
def notification_escalate(ident):
    return jsonify(service.escalate(notify_store(), ident, _body()))


@notify_api.get("/api/notifications/<ident>/escalations")
def notification_escalations(ident):
    item = service.get_notification(notify_store(), ident)
    # 子集合按 REST 习惯直接返回数组，父对象内另有同名字段的富结构。
    return jsonify([e for e in item["escalations"]])


# --------------------------------------------------------------------------- #
# 交班
# --------------------------------------------------------------------------- #
@notify_api.post("/api/notifications/handover")
@notify_api.post("/api/handover")
@notify_api.post("/api/handovers")
@notify_api.post("/api/shifts/handover")
@notify_api.post("/api/shift/handover")
def global_handover():
    return jsonify(service.handover(notify_store(), _body()))


@notify_api.post("/api/notifications/<ident>/handover")
@notify_api.post("/api/notifications/<ident>/handoff")
@notify_api.post("/api/tasks/<ident>/handover")
@notify_api.post("/api/critical-values/<ident>/handover")
def one_handover(ident):
    return jsonify(service.handover(notify_store(), _body(), ident=ident))


# --------------------------------------------------------------------------- #
# 额外集合别名 / 静态待办视图 / 逾期扫描
# --------------------------------------------------------------------------- #
@notify_api.get("/api/notifications/pending")
@notify_api.get("/api/tasks/pending")
@notify_api.get("/api/pending")
def pending_list():
    items = service.list_notifications(notify_store(), {"pending_only": True, **_query()})
    return jsonify(service.envelope(items))


@notify_api.post("/api/handoff")
@notify_api.post("/api/shift-handover")
@notify_api.post("/api/shift-change")
@notify_api.post("/api/notify/handover")
def handover_aliases():
    return jsonify(service.handover(notify_store(), _body()))


@notify_api.post("/api/escalations")
def escalation_collection():
    body = _body()
    ident = _ident_from(body, "notification_id", "task_id", "critical_id")
    return jsonify(service.escalate(notify_store(), ident, body))


@notify_api.post("/api/notifications/escalate-overdue")
@notify_api.post("/api/escalations/scan-overdue")
def escalate_overdue():
    """对所有逾期未回执任务各补一次升级（不换人）。"""
    from app.notify.model import resolve_clock, is_overdue
    store = notify_store()
    body = _body()
    at_dt, _ = resolve_clock(body)
    rows = store.notify_list(pending_only=True)
    changed = []
    for row in rows:
        if is_overdue(row.get("due_at_dt") or row.get("due_at"), at_dt):
            owner = row.get("owner") or row.get("receiver")
            updated = store.notify_add_escalation(row["id"], {
                "at_display": body.get("at") or body.get("now"),
                "at_dt": __import__("app.notify.model", fromlist=["iso"]).iso(at_dt),
                "reason": body.get("reason") or "OVERDUE", "kind": "TIMEOUT",
                "from_owner": owner, "to_owner": owner, "payload": body,
            })
            view = service.serialize_notification(updated, at=at_dt)
            if view:
                changed.append(view)
    return jsonify(service.envelope(changed, escalated=len(changed)))
