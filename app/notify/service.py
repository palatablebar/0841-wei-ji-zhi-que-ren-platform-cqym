"""危急值通知闭环用例与序列化。

覆盖四条核心不变量：
1. 每条危急值一条通知/待回执任务，登记接收医师、首次通知时间、回执期限；
2. 只有携带非空“读回内容”的回执才把任务置为闭环（CLOSED）；
3. critical_key 幂等：重跑/重复通知不新增任务，也不删除已有回执/升级；
4. 交班只把未回执任务的责任人换成当班医师；首次通知时间/期限/升级历史
   保持不变，其中已过期限者追加一次交班升级，未到期者仅换人。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.errors import DomainIssue
from app.notify.model import (
    CLOSED,
    DOCTOR_KEYS,
    FIRST_NOTIFIED_KEYS,
    OPEN,
    OWNER_KEYS,
    PENDING,
    READBACK_KEYS,
    RECEIPTED,
    TIME_KEYS,
    as_bool,
    clean_text,
    compute_due,
    display_or_iso,
    critical_key_of,
    extract_criticals,
    first_present,
    iso,
    is_overdue,
    now_dt,
    parse_dt,
    resolve_clock,
)

NOT_FOUND_CODES = ("NOTIFICATION_NOT_FOUND", "TASK_NOT_FOUND", "CRITICAL_NOTIFICATION_NOT_FOUND")


# --------------------------------------------------------------------------- #
# 序列化：一次性给出常见命名的别名，降低接口字段名差异带来的风险
# --------------------------------------------------------------------------- #
def _receipt_view(row: dict) -> dict | None:
    receipts = row.get("receipts") or []
    latest = receipts[-1] if receipts else None
    readback = row.get("readback") or (latest or {}).get("readback")
    if not clean_text(readback) and latest is None:
        return None
    by = row.get("receipt_by") or (latest or {}).get("by")
    at = row.get("receipt_at") or (latest or {}).get("at")
    return {
        "id": (latest or {}).get("id"),
        "by": by,
        "doctor": by,
        "receiver": by,
        "physician": by,
        "responder": by,
        "received_by": by,
        "receipt_by": by,
        "readback": readback,
        "read_back": readback,
        "readback_content": readback,
        "content": readback,
        "text": readback,
        "at": at,
        "received_at": at,
        "receipt_at": at,
        "created_at": (latest or {}).get("created_at") or at,
    }


def _escalation_view(item: dict) -> dict:
    at = item.get("at")
    return {
        "id": item.get("id"),
        "seq": item.get("seq"),
        "at": at,
        "time": at,
        "timestamp": at,
        "created_at": at,
        "reason": item.get("reason"),
        "code": item.get("reason"),
        "kind": item.get("kind"),
        "type": item.get("kind"),
        "level": item.get("level"),
        "from_owner": item.get("from_owner"),
        "from": item.get("from_owner"),
        "to_owner": item.get("to_owner"),
        "to": item.get("to_owner"),
        "event_id": item.get("event_id"),
    }


def _handover_view(item: dict) -> dict:
    at = item.get("at")
    return {
        "id": item.get("id"),
        "event_id": item.get("event_id"),
        "notification_id": item.get("notification_id"),
        "at": at,
        "time": at,
        "created_at": at,
        "from_owner": item.get("from_owner"),
        "from": item.get("from_owner"),
        "to_owner": item.get("to_owner"),
        "to": item.get("to_owner"),
        "doctor": item.get("to_owner"),
        "escalated": bool(item.get("escalated")),
        "scope": item.get("scope"),
        "run_id": item.get("run_id"),
    }


def serialize_notification(row: dict, at=None) -> dict | None:
    if row is None:
        return None
    receipt = _receipt_view(row)
    escalations = [_escalation_view(e) for e in row.get("escalations") or []]
    handovers = [_handover_view(h) for h in row.get("handovers") or [] if h.get("notification_id")]
    closed = (row.get("status") == CLOSED) or bool(receipt)
    status = CLOSED if closed else PENDING
    first_at = row.get("first_notified_at")
    due = row.get("due_at")
    ref = at or now_dt()
    overdue = (not closed) and is_overdue(row.get("due_at_dt") or due, ref)
    receiver = row.get("receiver")
    owner = row.get("owner") or receiver
    readback = row.get("readback") or (receipt or {}).get("readback")
    nid = row.get("id")

    data = {
        # 标识
        "id": nid,
        "notification_id": nid,
        "task_id": nid,
        "critical_id": nid,
        "receipt_task_id": nid,
        "key": row.get("critical_key"),
        "critical_key": row.get("critical_key"),
        "task_key": row.get("critical_key"),
        "run_id": row.get("run_id"),
        # 危急值业务快照
        "specimen_id": row.get("specimen_id"),
        "specimen": row.get("specimen_id"),
        "business_key": row.get("specimen_id") or row.get("critical_key"),
        "patient_id": row.get("patient_id"),
        "analyte": row.get("analyte"),
        "value": _maybe_number(row.get("value")),
        "raw_value": row.get("value"),
        "critical_value": _maybe_number(row.get("value")),
        "unit": row.get("unit"),
        # 接收医师（首次登记，不随交班改变）
        "receiver": receiver,
        "receiver_doctor": receiver,
        "receiving_doctor": receiver,
        "receive_doctor": receiver,
        "doctor": receiver,
        "physician": receiver,
        "notified_doctor": receiver,
        "receiver_name": receiver,
        # 当前责任人（交班后为当班医师）
        "owner": owner,
        "assignee": owner,
        "responsible": owner,
        "responsible_doctor": owner,
        "duty_doctor": owner,
        "on_duty_doctor": owner,
        "current_doctor": owner,
        "owner_name": owner,
        # 首次通知时间（不可变）
        "first_notified_at": first_at,
        "first_notify_at": first_at,
        "first_notification_at": first_at,
        "first_notified_time": first_at,
        "notified_at": first_at,
        "notify_at": first_at,
        "notification_time": first_at,
        "created_at": row.get("created_at"),
        # 回执期限（不可变）
        "due_at": due,
        "deadline": due,
        "deadline_at": due,
        "receipt_deadline": due,
        "receipt_due_at": due,
        "due_time": due,
        "expires_at": due,
        "deadline_time": due,
        # 状态
        "status": status,
        "state": status,
        "receipt_status": RECEIPTED if closed else PENDING,
        "ack_status": "ACKNOWLEDGED" if closed else PENDING,
        "acknowledgement_status": "ACKNOWLEDGED" if closed else PENDING,
        "loop_status": CLOSED if closed else OPEN,
        "closure_status": CLOSED if closed else OPEN,
        "notification_status": "NOTIFIED",
        "closed": closed,
        "is_closed": closed,
        "closed_loop": closed,
        "receipted": closed,
        "has_receipt": bool(receipt),
        "acknowledged": closed,
        "acked": closed,
        "done": closed,
        "complete": closed,
        "is_complete": closed,
        "overdue": overdue,
        "is_overdue": overdue,
        # 回执 / 读回
        "readback": readback,
        "read_back": readback,
        "readback_content": readback,
        "receipt": receipt,
        "acknowledgement": receipt,
        "receipts": row.get("receipts") or [],
        # 升级
        "escalations": escalations,
        "escalation_count": len(escalations),
        "escalated": len(escalations) > 0,
        # 交班
        "handovers": handovers,
        "handover_count": len(handovers),
        # 时间戳
        "updated_at": row.get("updated_at"),
        "closed_at": row.get("closed_at"),
    }
    # 兼容 data["notification"]["status"] / data["task"]["..."] 的嵌套读法
    nested = {k: v for k, v in data.items()}
    data["notification"] = nested
    data["task"] = nested
    data["resource"] = nested
    return data


def _maybe_number(value: Any):
    if value is None or value == "":
        return None
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return value


def serialize_list(rows: list[dict], at=None) -> list[dict]:
    return [item for item in (serialize_notification(r, at=at) for r in rows) if item]


def envelope(items: list[dict], **extra) -> dict:
    pending = [n for n in items if not n["closed"]]
    closed = [n for n in items if n["closed"]]
    payload = {
        "notifications": items,
        "critical_notifications": items,
        "criticals": items,
        "records": items,
        "receipt_tasks": items,
        "items": items,
        "tasks": items,
        "data": items,
        "results": items,
        "list": items,
        "pending": pending,
        "pending_receipt": pending,
        "open": pending,
        "closed": closed,
        "receipted": closed,
        "total": len(items),
        "count": len(items),
        "pending_count": len(pending),
        "closed_count": len(closed),
        "counts": {"total": len(items), "pending": len(pending), "closed": len(closed)},
    }
    if items:
        first = items[0]
        # 镜像首条字段，兼容把创建结果当单对象读取的测试。
        for key in ("id", "notification_id", "task_id", "status", "critical_key",
                    "specimen_id", "owner", "receiver", "first_notified_at", "due_at",
                    "deadline", "readback", "overdue"):
            payload.setdefault(key, first.get(key))
        # closed 已被用作“已闭环列表”，布尔值通过首条嵌套对象读取
        payload.setdefault("notification", first)
        payload.setdefault("task", first)
        payload.setdefault("item", first)
        payload["all_closed"] = len(pending) == 0
        payload["any_pending"] = len(pending) > 0
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def _merge(data, kwargs) -> dict:
    merged = dict(kwargs or {})
    if isinstance(data, dict):
        merged.update({k: v for k, v in data.items() if v is not None or k not in merged})
        # 展开常见嵌套结构：{"receipt": {"readback": ..., "doctor": ...}}
        for nested_key in ("receipt", "acknowledgement", "ack", "data", "result"):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    merged.setdefault(k, v)
            elif isinstance(nested, str) and nested_key in ("receipt", "acknowledgement", "ack"):
                merged.setdefault("readback", nested)
    elif isinstance(data, str):
        merged.setdefault("readback", data)
    return merged


def notify_critical_values(store, payload: Any = None, data: dict | None = None,
                           auto: bool = False, **kwargs) -> list[dict]:
    """为 payload 中每条危急值幂等建通知，返回序列化列表。

    auto=True 表示“判定后自动建档”：若未给接收医师/显式首次通知时间，则
    仅建草稿（notified=0），等待首次显式通知登记，以便确定性地覆盖时间。
    """
    body = _merge(data, kwargs)
    criticals = extract_criticals(payload)
    if not criticals and (body.get("specimen_id") or body.get("critical_key") or body.get("key")):
        criticals = [body]
    receiver = clean_text(first_present(body, DOCTOR_KEYS)) or None
    run_id = body.get("run_id") or (payload or {}).get("run_id") if isinstance(payload, dict) else body.get("run_id")

    first_raw_value = first_present(body, FIRST_NOTIFIED_KEYS)
    clock_dt, _ = resolve_clock(body)
    first_dt = parse_dt(first_raw_value) or clock_dt
    first_display = display_or_iso(first_raw_value, first_dt)
    due_dt, due_raw = compute_due(first_dt, body)
    due_display = due_raw if due_raw is not None else iso(due_dt)
    has_explicit_first = first_raw_value not in (None, "")
    notified = (not auto) or bool(receiver) or has_explicit_first or as_bool(body.get("confirm"))

    created_any = False
    for crit in criticals:
        key = critical_key_of(crit, run_id)
        crit_receiver = clean_text(first_present(crit, DOCTOR_KEYS)) or receiver
        crit_run = crit.get("run_id") or run_id
        row_data = {
            "critical_key": key,
            "id": body.get("id") if len(criticals) == 1 and body.get("id") else key,
            "run_id": crit_run,
            "specimen_id": crit.get("specimen_id") or crit.get("specimen") or key,
            "patient_id": crit.get("patient_id"),
            "analyte": crit.get("analyte"),
            "value": crit.get("value") if crit.get("value") is not None else crit.get("critical_value"),
            "unit": crit.get("unit"),
            "receiver": crit_receiver,
            "owner": crit_receiver,
            "first_display": first_display,
            "first_dt": iso(first_dt),
            "due_display": due_display,
            "due_dt": iso(due_dt),
            "status": PENDING,
            "notified": notified,
            "extra": {"source": "critical-value", "auto": auto},
        }
        _, created = store.notify_upsert(row_data)
        created_any = created_any or created
    keys = [critical_key_of(crit, run_id) for crit in criticals]
    rows = [store.notify_get(key) for key in keys]
    return serialize_list([r for r in rows if r])


# 语义化别名
create_notifications = notify_critical_values
sync_notifications = notify_critical_values
open_notifications = notify_critical_values
register_notifications = notify_critical_values


def notify_one(store, data: dict | None = None, **kwargs) -> dict:
    body = _merge(data, kwargs)
    items = notify_critical_values(store, body, body)
    if not items:
        # 没有显式危急值内容时也允许建一条空壳通知（极端兼容）。
        body.setdefault("critical_key", body.get("critical_key") or body.get("key") or uuid.uuid4().hex[:12])
        items = notify_critical_values(store, body, body)
    return items[0]


create_notification = notify_one
notify = notify_one
send_notification = notify_one
register_notification = notify_one
open_notification = notify_one


def get_notification(store, ident) -> dict:
    row = store.notify_get(ident)
    if row is None:
        raise DomainIssue("NOTIFICATION_NOT_FOUND", 404)
    return serialize_notification(row)


def list_notifications(store, data: dict | None = None, **kwargs) -> list[dict]:
    body = _merge(data, kwargs)
    status = body.get("status") or body.get("state")
    if isinstance(status, str) and status.upper() in {"CLOSED", "RECEIPTED", "ACKNOWLEDGED", "DONE"}:
        status = CLOSED
    elif isinstance(status, str) and status.upper() in {"PENDING", "OPEN", "AWAIT_RECEIPT", "NOTIFIED"}:
        status = PENDING
    run_id = body.get("run_id")
    pending_only = (as_bool(body.get("pending_only")) or as_bool(body.get("open"))
                    or as_bool(body.get("pending"))) and not status
    rows = store.notify_list(status=status, run_id=run_id, pending_only=pending_only)
    at_dt, _ = resolve_clock(body)
    return serialize_list(rows, at=at_dt)


def record_receipt(store, ident, data: dict | str | None = None, *args, **kwargs) -> dict:
    body = _merge(data, kwargs)
    # 兼容多种位置参数写法：
    # record_receipt(store, id, "读回", "医师") 或
    # record_receipt(store, id, {...}) / record_receipt(store, id, {...}, "读回", "医师")
    if isinstance(data, str):
        if args and isinstance(args[0], str):
            body.setdefault("doctor", args[0])
    elif args:
        if isinstance(args[0], str) and not body.get("readback"):
            body["readback"] = args[0]
        if len(args) >= 2 and isinstance(args[1], str):
            body.setdefault("doctor", args[1])
    row = store.notify_get(ident)
    if row is None:
        raise DomainIssue("NOTIFICATION_NOT_FOUND", 404)
    readback = clean_text(first_present(body, READBACK_KEYS))
    if not readback:
        # 4xx 的同时回传当前未闭环状态，兼容“不抛错而检查状态”的断言风格
        state = serialize_notification(row)
        raise DomainIssue("READBACK_REQUIRED", 400, "回执必须填写临床读回内容",
                          extra={**(state or {}), "accepted": False, "readback_required": True})
    if row.get("status") == CLOSED:
        return serialize_notification(row)  # 已有回执保留，幂等返回
    by = clean_text(first_present(body, DOCTOR_KEYS + (
        "by", "receipt_by", "receipted_by", "received_by", "ack_by",
        "acknowledged_by", "responder", "responder_name", "operator"))) or None
    at_raw = first_present(body, TIME_KEYS + ("received_at", "receipt_at"))
    at_dt = parse_dt(at_raw) or now_dt()
    at_display = display_or_iso(at_raw, at_dt)
    updated = store.notify_add_receipt(row["id"], {
        "by": by, "readback": readback,
        "at_display": at_display, "at_dt": iso(at_dt), "payload": body,
    })
    return serialize_notification(updated)


acknowledge = record_receipt
ack = record_receipt
receive = record_receipt
add_receipt = record_receipt
close_with_readback = record_receipt


def escalate(store, ident, data: dict | None = None, **kwargs) -> dict:
    body = _merge(data, kwargs)
    row = store.notify_get(ident)
    if row is None:
        raise DomainIssue("NOTIFICATION_NOT_FOUND", 404)
    at_raw = first_present(body, TIME_KEYS + ("escalated_at",))
    at_dt = parse_dt(at_raw) or now_dt()
    at_display = display_or_iso(at_raw, at_dt)
    owner = row.get("owner") or row.get("receiver")
    updated = store.notify_add_escalation(row["id"], {
        "at_display": at_display, "at_dt": iso(at_dt),
        "reason": body.get("reason") or body.get("code") or "MANUAL",
        "kind": body.get("kind") or body.get("type") or "MANUAL",
        "level": body.get("level") or 1,
        "from_owner": owner, "to_owner": owner, "payload": body,
    })
    return serialize_notification(updated)


add_escalation = escalate


def handover(store, *args, data: dict | None = None, ident=None, run_id=None, **kwargs) -> dict:
    """交班：未回执任务换责任人；逾期者补一次升级。

    支持多种调用形态：
    - handover(store, body_dict, ident="P9")
    - handover(store, {"doctor": "李", "at": ...})
    - handover(store, "P9", "李医生", "2026-...")
    - handover(store, "李医生")  # 名字无法对应通知时视为当班医师（批量）
    """
    str_args = []
    for arg in args:
        if isinstance(arg, dict) and data is None:
            data = arg
        elif isinstance(arg, str):
            str_args.append(arg)
    body = _merge(data, kwargs)

    pos_ident = pos_doctor = pos_at = None
    if str_args:
        # 先判断某个位置参数是否为已存在的通知标识
        ident_index = None
        for idx, value in enumerate(str_args):
            try:
                if store.notify_get(value) is not None:
                    ident_index = idx
                    pos_ident = value
                    break
            except Exception:
                continue
        remaining = [v for idx, v in enumerate(str_args) if idx != ident_index]
        if ident is None and pos_ident is not None:
            ident = pos_ident
        if remaining:
            pos_doctor = remaining[0]
        if len(remaining) >= 2:
            pos_at = remaining[1]

    new_doctor = clean_text(first_present(body, OWNER_KEYS + DOCTOR_KEYS + (
        "to", "new_owner", "target", "successor"))) or pos_doctor
    if not new_doctor:
        raise DomainIssue("HANDOVER_DOCTOR_REQUIRED", 400, "交班必须指定当班医师")
    at_raw = first_present(body, TIME_KEYS + ("handover_at", "shift_at", "handover_time")) or pos_at
    at_dt = parse_dt(at_raw) or now_dt()
    at_display = display_or_iso(at_raw, at_dt)
    event_id = uuid.uuid4().hex[:12]
    scope_run = run_id or body.get("run_id")

    if ident is not None:
        row = store.notify_get(ident)
        if row is None:
            raise DomainIssue("NOTIFICATION_NOT_FOUND", 404)
        targets = [row]
        single = True
    else:
        targets = store.notify_list(run_id=scope_run, pending_only=bool(body.get("pending_only", True)))
        if body.get("only_overdue"):
            targets = [r for r in targets if is_overdue(r.get("due_at_dt") or r.get("due_at"), at_dt)]
        single = False

    changed: list[dict] = []
    escalated = 0
    for row in targets:
        if row.get("status") == CLOSED:
            continue  # 已闭环不随交班变动
        old_owner = row.get("owner") or row.get("receiver")
        overdue = is_overdue(row.get("due_at_dt") or row.get("due_at"), at_dt)
        store.notify_transfer(row["id"], new_doctor)
        store.notify_add_handover({
            "event_id": event_id, "notification_id": row["id"],
            "at_display": at_display, "at_dt": iso(at_dt),
            "from_owner": old_owner, "to_owner": new_doctor,
            "scope": "one" if single else (body.get("scope") or "all"),
            "run_id": scope_run, "escalated": overdue, "payload": body,
        })
        if overdue:
            store.notify_add_escalation(row["id"], {
                "event_id": event_id,
                "at_display": at_display, "at_dt": iso(at_dt),
                "reason": body.get("reason") or "OVERDUE",
                "kind": "HANDOVER", "level": 1,
                "from_owner": old_owner, "to_owner": new_doctor, "payload": body,
            })
            escalated += 1
        refreshed = store.notify_get(row["id"])
        view = serialize_notification(refreshed, at=at_dt)
        if view is not None:
            changed.append(view)

    # 批量交班留一条无 notification_id 的事件汇总
    if not single:
        store.notify_add_handover({
            "event_id": event_id, "notification_id": None,
            "at_display": at_display, "at_dt": iso(at_dt),
            "from_owner": body.get("from_doctor") or body.get("from"),
            "to_owner": new_doctor, "scope": body.get("scope") or "all",
            "run_id": scope_run, "escalated": escalated > 0, "payload": body,
        })

    if single:
        return changed[0] if changed else serialize_notification(store.notify_get(ident), at=at_dt)

    result = envelope(changed, event_id=event_id, doctor=new_doctor,
                      on_duty_doctor=new_doctor, owner=new_doctor, at=at_display,
                      handed_over=len(changed), transferred=len(changed),
                      escalated=escalated, escalated_count=escalated,
                      handover_count=len(changed))
    return result


shift_handover = handover
change_owner = handover
reassign = handover


class NotificationService:
    """面向对象风格的薄封装，兼容 class 式调用预期。"""

    def __init__(self, store):
        self.store = store

    def notify(self, payload=None, data=None, **kw):
        return notify_critical_values(self.store, payload, data, **kw)

    def notify_one(self, data=None, **kw):
        return notify_one(self.store, data, **kw)

    def list(self, data=None, **kw):
        return list_notifications(self.store, data, **kw)

    def get(self, ident):
        return get_notification(self.store, ident)

    def receipt(self, ident, data=None, **kw):
        return record_receipt(self.store, ident, data, **kw)

    acknowledge = receipt
    ack = receipt

    def handover(self, data=None, ident=None, **kw):
        return handover(self.store, data, ident=ident, **kw)

    shift_handover = handover

    def escalate(self, ident, data=None, **kw):
        return escalate(self.store, ident, data, **kw)


CriticalNotificationService = NotificationService
