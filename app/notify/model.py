"""危急值通知闭环：纯领域辅助。

这里不依赖 Flask / sqlite，便于单测直接驱动。时间统一解析为带时区的
UTC datetime 进行比较；若调用方显式传入时间字符串，原始字符串会保留，
序列化时优先原样回显，以保证“传入即所得”。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.catalog import SPEC

PENDING = "PENDING"
CLOSED = "CLOSED"
RECEIPTED = "RECEIPTED"
OPEN = "OPEN"

DEFAULT_SLA_MINUTES = 30

DOCTOR_KEYS = (
    "doctor", "physician", "receiver", "receive_doctor", "receiving_doctor",
    "receiver_doctor", "notified_doctor", "notify_doctor", "to_doctor",
    "receiver_name", "doctor_name", "md",
)
OWNER_KEYS = (
    "owner", "assignee", "responsible", "responsible_doctor", "duty_doctor",
    "on_duty_doctor", "current_doctor", "handover_doctor", "new_doctor",
    "target_doctor", "owner_name", "duty",
)
READBACK_KEYS = (
    "readback", "read_back", "readback_content", "read_back_content",
    "readback_text", "readback_result", "repeat_back", "echo", "echo_content",
    "echo_text", "content", "text", "message", "reply", "readback_value",
)
TIME_KEYS = ("now", "at", "time", "timestamp", "as_of", "handover_at", "moment")
FIRST_NOTIFIED_KEYS = (
    "first_notified_at", "first_notify_at", "first_notification_at",
    "first_notified_time", "notified_at", "notify_at", "notification_time",
    "notified_time", "sent_at",
)
DUE_KEYS = (
    "due_at", "deadline", "deadline_at", "receipt_deadline", "receipt_due_at",
    "due_time", "expires_at", "expire_at", "limit_at", "deadline_time",
)
DURATION_KEYS = (
    "deadline_minutes", "receipt_minutes", "sla_minutes", "timeout_minutes",
    "within_minutes", "minutes",
)
DURATION_SECONDS_KEYS = ("deadline_seconds", "receipt_seconds", "sla_seconds")


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def display_or_iso(raw: Any, parsed: datetime | None) -> str | None:
    """字符串入参原样回显；datetime/其它入参用标准 ISO。"""
    if isinstance(raw, str) and raw.strip():
        return raw
    return iso(parsed)


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in ("", "0", "false", "no", "off", "否")


def parse_dt(value: Any) -> datetime | None:
    """宽容解析时间，统一为 aware UTC。无法解析返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 1e12:  # 毫秒
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def first_present(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("name", "doctor", "label", "value", "text"):
            if value.get(key):
                return clean_text(value[key])
        return ""
    return str(value).strip()


def resolve_clock(data: dict[str, Any] | None = None, *, default: datetime | None = None):
    """从请求体里取显式时钟；测试可传 now/at 保证确定性。"""
    raw = first_present(data or {}, TIME_KEYS)
    parsed = parse_dt(raw)
    if parsed is not None:
        return parsed, raw
    return (default or now_dt()), None


def duration_from(data: dict[str, Any]) -> timedelta | None:
    minutes = first_present(data, DURATION_KEYS)
    if minutes is not None:
        try:
            return timedelta(minutes=float(minutes))
        except (TypeError, ValueError):
            pass
    seconds = first_present(data, DURATION_SECONDS_KEYS)
    if seconds is not None:
        try:
            return timedelta(seconds=float(seconds))
        except (TypeError, ValueError):
            pass
    hours = data.get("deadline_hours") or data.get("receipt_hours")
    if hours is not None:
        try:
            return timedelta(hours=float(hours))
        except (TypeError, ValueError):
            pass
    return None


def default_sla() -> timedelta:
    import os
    raw = os.getenv("CRITICAL_RECEIPT_MINUTES") or os.getenv("RECEIPT_DEADLINE_MINUTES")
    try:
        return timedelta(minutes=float(raw)) if raw else timedelta(minutes=DEFAULT_SLA_MINUTES)
    except (TypeError, ValueError):
        return timedelta(minutes=DEFAULT_SLA_MINUTES)


def compute_due(first_dt: datetime, data: dict[str, Any] | None = None) -> tuple[datetime, str | None]:
    """返回 (截止时间, 原始输入字符串或 None)。"""
    data = data or {}
    delta = duration_from(data)
    if delta is not None:
        return first_dt + delta, None
    due_raw = first_present(data, DUE_KEYS)
    # 纯数字且数值不大时按“分钟时长”处理，例如 deadline=0 / 30。
    if isinstance(due_raw, (int, float)) and not isinstance(due_raw, bool) and abs(float(due_raw)) < 100000:
        return first_dt + timedelta(minutes=float(due_raw)), str(due_raw)
    if isinstance(due_raw, str):
        text = due_raw.strip().lower()
        for suffix, unit in (("分钟", "minutes"), ("min", "minutes"), ("m", "minutes"),
                             ("小时", "hours"), ("h", "hours"), ("天", "days"), ("d", "days")):
            if text.endswith(suffix):
                number = text[: -len(suffix)].strip()
                try:
                    return first_dt + timedelta(**{unit: float(number)}), due_raw
                except ValueError:
                    break
    due_dt = parse_dt(due_raw)
    if due_dt is not None:
        return due_dt, str(due_raw)
    return first_dt + default_sla(), None


def is_overdue(due_at: Any, at: datetime | None = None) -> bool:
    due_dt = due_at if isinstance(due_at, datetime) else parse_dt(due_at)
    if due_dt is None:
        return False
    at = at or now_dt()
    return at >= due_dt


def critical_key_of(item: dict[str, Any], run_id: str | None = None) -> str:
    explicit = first_present(item, ("critical_key", "notification_key", "task_key", "key"))
    if explicit is not None:
        return clean_text(explicit)
    ident = first_present(item, ("specimen_id", SPEC["key"], "specimen", "sample_id",
                                 "notification_id", "task_id", "id", "code"))
    if ident is not None and clean_text(ident):
        return clean_text(ident)
    basis = (run_id or "") + "|" + clean_text(item.get("analyte")) + "|" + clean_text(item.get("value"))
    return "CV-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _is_critical_row(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("critical") is True:
        return True
    if clean_text(row.get("flag")).upper() == "CRITICAL":
        return True
    if clean_text(row.get("reason")).upper() == "CRITICAL_VALUE":
        return True
    code = clean_text(row.get("code")).upper()
    if code in {"CRITICAL_VALUE", "CRITICAL"}:
        return True
    # 本项目判定层中 REJECT 即危急阻断（其它阻断在行级隔离，不进入结果）
    if clean_text(row.get("verdict")).upper() == "REJECT":
        return True
    return False


_KNOWN_VERDICTS = {"PASS", "REVIEW", "REJECT"}
_KNOWN_REASONS = {"CRITICAL_VALUE", "OUT_OF_RANGE", "RATE_ALERT", "IN_RANGE", "OK"}
_KNOWN_FLAGS = {"CRITICAL", "OUT_OF_RANGE", "RATE_ALERT", "NORMAL"}


def _has_decision_marker(row: dict[str, Any]) -> bool:
    if clean_text(row.get("flag")).upper() in _KNOWN_FLAGS:
        return True
    if clean_text(row.get("verdict")).upper() in _KNOWN_VERDICTS:
        return True
    if clean_text(row.get("reason")).upper() in _KNOWN_REASONS:
        return True
    return False


_TRUSTED_CONTAINERS = ("critical_values", "criticals", "critical_notifications",
                       "alerts", "notifications")
_ROW_CONTAINERS = ("rows", "results", "records", "items", "data")


def extract_criticals(payload: Any) -> list[dict[str, Any]]:
    """从 pipeline/run payload / 列表 / 单条对象中提取危急值行。

    显式的危急值集合（critical_values/criticals/...）被视为“调用方已指定”，
    不再过滤；最外层列表若带有判定标记（verdict/reason/flag），说明是完整
    判定结果，需要只保留危急值，否则按显式危急值列表整体信任。
    rows/results 等可能混有正常行，按 CRITICAL 标记过滤。
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
        if any(_has_decision_marker(row) for row in rows):
            return [row for row in rows if _is_critical_row(row)]
        return rows
    if not isinstance(payload, dict):
        return []

    for container in _TRUSTED_CONTAINERS:
        value = payload.get(container)
        if isinstance(value, list) and value:
            return [row for row in value if isinstance(row, dict)]

    for container in _ROW_CONTAINERS:
        value = payload.get(container)
        if isinstance(value, list) and value:
            return [row for row in value if isinstance(row, dict) and _is_critical_row(row)]

    if _is_critical_row(payload):
        return [payload]
    # 单个显式危急值（带业务主键即可），交给上层兜底，不在此臆断。
    return []
