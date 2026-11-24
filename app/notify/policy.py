"""危急值闭环规则谓词（无 IO，便于直接单测）。"""

from __future__ import annotations

from typing import Any

from app.notify.model import CLOSED, PENDING, clean_text, is_overdue, parse_dt


def has_readback(receipt_or_text: Any) -> bool:
    if receipt_or_text is None:
        return False
    if isinstance(receipt_or_text, str):
        return bool(clean_text(receipt_or_text))
    if isinstance(receipt_or_text, dict):
        return bool(clean_text(
            receipt_or_text.get("readback")
            or receipt_or_text.get("read_back")
            or receipt_or_text.get("content")
        ))
    return bool(clean_text(getattr(receipt_or_text, "readback", "")))


def can_close(readback: Any) -> bool:
    """只有留下非空读回内容才允许闭环。"""
    return has_readback(readback)


def is_closed(notification: dict) -> bool:
    return bool(notification.get("closed")) or notification.get("status") == CLOSED


def is_pending(notification: dict) -> bool:
    return not is_closed(notification) and notification.get("status", PENDING) == PENDING


def overdue(notification: dict, at=None) -> bool:
    due = notification.get("due_at_dt") or notification.get("due_at") or notification.get("deadline")
    return is_overdue(due, at)


def should_escalate_on_handover(notification: dict, at=None) -> bool:
    """交班时：仅未回执且已过期限的通知补一次升级。"""
    if is_closed(notification):
        return False
    return overdue(notification, at)


def should_reassign_on_handover(notification: dict) -> bool:
    """交班时：所有未回执通知都换责任人（无论是否到期）。"""
    return not is_closed(notification)


def first_notification_time(notification: dict):
    return parse_dt(notification.get("first_notified_at"))


def deadline_of(notification: dict):
    return parse_dt(notification.get("due_at") or notification.get("deadline"))
