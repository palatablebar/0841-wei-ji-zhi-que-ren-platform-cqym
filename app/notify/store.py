"""危急值通知闭环的 sqlite 持久化。

设计要点：
- critical_key（通常等于 specimen_id，业务主键）唯一，重跑/重复通知走
  “存在即返回”，绝不删除重建，因此回执与升级记录不会被误删。
- 回执、升级、交班均为追加记录。
- NotifyStoreMixin 可直接挂到已有的 app.review.store.Store 上复用同一 DB。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS critical_notifications(
  id TEXT PRIMARY KEY,
  critical_key TEXT UNIQUE NOT NULL,
  run_id TEXT,
  specimen_id TEXT,
  patient_id TEXT,
  analyte TEXT,
  value TEXT,
  unit TEXT,
  receiver TEXT,
  owner TEXT,
  first_notified_at TEXT,
  first_notified_at_dt TEXT,
  due_at TEXT,
  due_at_dt TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING',
  notified INTEGER NOT NULL DEFAULT 1,
  readback TEXT,
  receipt_by TEXT,
  receipt_at TEXT,
  receipt_at_dt TEXT,
  receipt_json TEXT,
  closed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  extra TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS critical_receipts(
  id TEXT PRIMARY KEY,
  notification_id TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 1,
  by TEXT,
  readback TEXT,
  at TEXT,
  at_dt TEXT,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS critical_escalations(
  id TEXT PRIMARY KEY,
  notification_id TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 1,
  at TEXT,
  at_dt TEXT,
  reason TEXT,
  kind TEXT,
  level INTEGER NOT NULL DEFAULT 1,
  from_owner TEXT,
  to_owner TEXT,
  event_id TEXT,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS critical_handovers(
  id TEXT PRIMARY KEY,
  event_id TEXT,
  notification_id TEXT,
  at TEXT,
  at_dt TEXT,
  from_owner TEXT,
  to_owner TEXT,
  scope TEXT,
  run_id TEXT,
  escalated INTEGER NOT NULL DEFAULT 0,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_esc_notif ON critical_escalations(notification_id);
CREATE INDEX IF NOT EXISTS idx_rcp_notif ON critical_receipts(notification_id);
CREATE INDEX IF NOT EXISTS idx_hov_notif ON critical_handovers(notification_id);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class NotifyStoreMixin:
    """依赖 self.connect() / self.path，可与 review.Store 共存。"""

    def init_notify(self):
        with self.connect() as db:
            db.executescript(SCHEMA)

    # ---------- 通知主记录 ----------
    def notify_upsert(self, data: dict) -> tuple[dict, bool]:
        """按 critical_key 幂等创建；已存在则只补全空的登记字段。"""
        self.init_notify()
        now_iso = _utcnow_iso()
        key = data["critical_key"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM critical_notifications WHERE critical_key=?", (key,)
            ).fetchone()
            if row is None:
                nid = data.get("id") or key
                db.execute(
                    """INSERT INTO critical_notifications(
                       id,critical_key,run_id,specimen_id,patient_id,analyte,value,unit,
                       receiver,owner,first_notified_at,first_notified_at_dt,
                       due_at,due_at_dt,status,notified,created_at,updated_at,extra)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (nid, key, data.get("run_id"), data.get("specimen_id"),
                     data.get("patient_id"), data.get("analyte"),
                     None if data.get("value") is None else str(data.get("value")),
                     data.get("unit"), data.get("receiver"), data.get("owner") or data.get("receiver"),
                     data.get("first_display"), data.get("first_dt"),
                     data.get("due_display"), data.get("due_dt"),
                     data.get("status", "PENDING"), 1 if data.get("notified", True) else 0,
                     now_iso, now_iso, json.dumps(data.get("extra") or {}, ensure_ascii=False)),
                )
                created = True
            else:
                created = False
                existing_notified = bool(row["notified"])
                incoming_notified = bool(data.get("notified", True))
                if not existing_notified and incoming_notified:
                    # 自动草稿被首次显式通知“激活”：以显式登记的医师/时间/期限为准
                    db.execute(
                        """UPDATE critical_notifications SET
                             receiver=?, owner=?, first_notified_at=?, first_notified_at_dt=?,
                             due_at=?, due_at_dt=?, notified=1, updated_at=?
                           WHERE critical_key=?""",
                        (data.get("receiver"), data.get("owner") or data.get("receiver"),
                         data.get("first_display"), data.get("first_dt"),
                         data.get("due_display"), data.get("due_dt"), now_iso, key),
                    )
                else:
                    # 已正式登记：仅补全空的接收医师/责任人，绝不覆盖首次通知时间与已有内容。
                    db.execute(
                        """UPDATE critical_notifications SET
                             receiver=COALESCE(NULLIF(receiver,''), ?),
                             owner=COALESCE(NULLIF(owner,''), ?),
                             run_id=COALESCE(run_id, ?),
                             updated_at=?
                           WHERE critical_key=?""",
                        (data.get("receiver"), data.get("owner") or data.get("receiver"),
                         data.get("run_id"), now_iso, key),
                    )
        return self.notify_get(key), created  # type: ignore[return-value]

    def notify_get(self, ident) -> dict | None:
        self.init_notify()
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM critical_notifications
                   WHERE id=? OR critical_key=? OR specimen_id=? LIMIT 1""",
                (str(ident), str(ident), str(ident)),
            ).fetchone()
            if row is None:
                return None
            return self._load_detail(db, dict(row))

    def notify_list(self, status: str | None = None, run_id: str | None = None,
                    pending_only: bool = False) -> list[dict]:
        self.init_notify()
        sql = "SELECT * FROM critical_notifications WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status=?"
            args.append(status.upper())
        if pending_only:
            sql += " AND status!='CLOSED'"
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        sql += " ORDER BY rowid ASC"
        with self.connect() as db:
            rows = db.execute(sql, args).fetchall()
            return [self._load_detail(db, dict(row)) for row in rows]

    def _load_detail(self, db, row: dict) -> dict:
        nid = row["id"]
        row["receipts"] = [dict(r) for r in db.execute(
            "SELECT * FROM critical_receipts WHERE notification_id=? ORDER BY seq ASC, rowid ASC",
            (nid,)).fetchall()]
        row["escalations"] = [dict(r) for r in db.execute(
            "SELECT * FROM critical_escalations WHERE notification_id=? ORDER BY seq ASC, rowid ASC",
            (nid,)).fetchall()]
        row["handovers"] = [dict(r) for r in db.execute(
            "SELECT * FROM critical_handovers WHERE notification_id=? ORDER BY rowid ASC",
            (nid,)).fetchall()]
        return row

    # ---------- 交班换人（只换责任人，不动首次通知时间/期限） ----------
    def notify_transfer(self, nid: str, new_owner: str) -> None:
        self.init_notify()
        with self.connect() as db:
            db.execute(
                "UPDATE critical_notifications SET owner=?, updated_at=? WHERE id=?",
                (new_owner, _utcnow_iso(), nid),
            )

    # ---------- 回执 ----------
    def notify_add_receipt(self, nid: str, receipt: dict) -> dict:
        self.init_notify()
        now_iso = _utcnow_iso()
        rid = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            seq = db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM critical_receipts WHERE notification_id=?",
                (nid,)).fetchone()["s"]
            db.execute(
                """INSERT INTO critical_receipts
                   (id,notification_id,seq,by,readback,at,at_dt,payload,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (rid, nid, seq, receipt.get("by"), receipt.get("readback"),
                 receipt.get("at_display"), receipt.get("at_dt"),
                 json.dumps(receipt.get("payload") or {}, ensure_ascii=False), now_iso),
            )
            db.execute(
                """UPDATE critical_notifications SET
                     status='CLOSED', readback=?, receipt_by=?, receipt_at=?,
                     receipt_at_dt=?, receipt_json=?, closed_at=?, updated_at=?
                   WHERE id=?""",
                (receipt.get("readback"), receipt.get("by"), receipt.get("at_display"),
                 receipt.get("at_dt"), json.dumps(receipt.get("payload") or {}, ensure_ascii=False),
                 receipt.get("at_display") or now_iso, now_iso, nid),
            )
        return self.notify_get(nid)  # type: ignore[return-value]

    # ---------- 升级 ----------
    def notify_add_escalation(self, nid: str, esc: dict) -> dict:
        self.init_notify()
        now_iso = _utcnow_iso()
        eid = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            seq = db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM critical_escalations WHERE notification_id=?",
                (nid,)).fetchone()["s"]
            db.execute(
                """INSERT INTO critical_escalations
                   (id,notification_id,seq,at,at_dt,reason,kind,level,
                    from_owner,to_owner,event_id,payload,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (eid, nid, seq, esc.get("at_display"), esc.get("at_dt"),
                 esc.get("reason", "OVERDUE"), esc.get("kind", "HANDOVER"),
                 int(esc.get("level") or 1), esc.get("from_owner"), esc.get("to_owner"),
                 esc.get("event_id"), json.dumps(esc.get("payload") or {}, ensure_ascii=False),
                 now_iso),
            )
        return self.notify_get(nid)  # type: ignore[return-value]

    # ---------- 交班 ----------
    def notify_add_handover(self, rec: dict) -> None:
        self.init_notify()
        now_iso = _utcnow_iso()
        hid = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute(
                """INSERT INTO critical_handovers
                   (id,event_id,notification_id,at,at_dt,from_owner,to_owner,
                    scope,run_id,escalated,payload,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (hid, rec.get("event_id"), rec.get("notification_id"),
                 rec.get("at_display"), rec.get("at_dt"), rec.get("from_owner"),
                 rec.get("to_owner"), rec.get("scope", "all"), rec.get("run_id"),
                 1 if rec.get("escalated") else 0,
                 json.dumps(rec.get("payload") or {}, ensure_ascii=False), now_iso),
            )

    def notify_list_handover_events(self, event_id: str | None = None) -> list[dict]:
        self.init_notify()
        with self.connect() as db:
            if event_id:
                rows = db.execute(
                    "SELECT * FROM critical_handovers WHERE event_id=? ORDER BY rowid ASC",
                    (event_id,)).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM critical_handovers WHERE notification_id IS NULL ORDER BY rowid DESC"
                ).fetchall()
            return [dict(r) for r in rows]


class NotifyStore(NotifyStoreMixin):
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_notify()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection


# 常见命名别名
NotificationStore = NotifyStore
CriticalNotificationStore = NotifyStore
CriticalStore = NotifyStore
TaskStore = NotifyStore

# 与 app.review.store.Store 命名习惯保持一致
Store = NotifyStore
