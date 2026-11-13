"""SQLite 存储：events（事件）+ runs（派工与运行）。

设计原则（P0 最薄，但接口留缝）：
- 事件不可变；去重靠 idem_key 唯一索引。
- 一次派工 = 一行 run，携带规则快照，worker 不需要读 rules.json。
- 所有写入都在一个事务里；SQLite WAL + 每次请求新连接（单进程多线程够用）。
- 之后要加「授权对象 / 订阅 / 结构化上报」，在这两张表上加，不改协议形状。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EVENT_FINAL = {"done", "failed", "recorded"}
RUN_FINAL = {"completed", "failed"}
RUN_ACTIVE = {"leased", "waiting_human"}

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  idem_key      TEXT NOT NULL UNIQUE,
  source        TEXT NOT NULL,
  type          TEXT NOT NULL,
  subject       TEXT NOT NULL DEFAULT '',
  summary       TEXT NOT NULL DEFAULT '',
  link          TEXT,
  payload       TEXT,
  received_at   TEXT NOT NULL,
  status        TEXT NOT NULL,
  rule          TEXT,
  note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at DESC);

CREATE TABLE IF NOT EXISTS runs (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id         INTEGER NOT NULL REFERENCES events(id),
  rule             TEXT NOT NULL,
  agent            TEXT NOT NULL,
  group_name       TEXT NOT NULL,
  parallel_limit   INTEGER NOT NULL DEFAULT 1,
  subject          TEXT NOT NULL DEFAULT '',
  cwd              TEXT NOT NULL,
  skill            TEXT,
  ask              TEXT NOT NULL DEFAULT '',
  sandbox          TEXT NOT NULL DEFAULT 'read-only',
  runtime          TEXT NOT NULL DEFAULT 'codex',
  model            TEXT,
  tell             TEXT NOT NULL DEFAULT 'needed',
  status           TEXT NOT NULL,
  attempts         INTEGER NOT NULL DEFAULT 0,
  next_attempt_at  TEXT,
  lease_expires_at TEXT,
  leased_by        TEXT,
  created_at       TEXT NOT NULL,
  started_at       TEXT,
  ended_at         TEXT,
  result_status    TEXT,
  summary          TEXT,
  session_ref      TEXT,
  artifacts        TEXT,
  report           TEXT,
  error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_group ON runs(group_name, status);
CREATE INDEX IF NOT EXISTS idx_runs_event ON runs(event_id);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Store:
    def __init__(
        self,
        db_path: Path,
        max_attempts: int = 3,
        retry_base_seconds: int = 15,
        retry_max_seconds: int = 600,
    ) -> None:
        self.db_path = Path(db_path)
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds

    # ---------- 基础设施 ----------

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
            if "runtime" not in cols:
                conn.execute(
                    "ALTER TABLE runs ADD COLUMN runtime TEXT NOT NULL DEFAULT 'codex'"
                )
            conn.commit()
        finally:
            conn.close()

    # ---------- 事件 ----------

    def add_event(
        self,
        idem_key: str,
        source: str,
        event_type: str,
        subject: str,
        summary: str,
        link: Optional[str],
        payload: Optional[Dict[str, Any]],
        status: str,
        rule: Optional[str],
    ) -> Tuple[int, bool]:
        """写一条事件。返回 (event_id, created)。created=False 表示命中既有去重键。"""
        now = iso(utc_now())
        conn = self.connect()
        try:
            existing = conn.execute(
                "SELECT id, status, rule FROM events WHERE idem_key = ?", (idem_key,)
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            cur = conn.execute(
                """INSERT INTO events
                   (idem_key, source, type, subject, summary, link, payload,
                    received_at, status, rule)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    idem_key,
                    source,
                    event_type,
                    subject,
                    summary,
                    link,
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                    now,
                    status,
                    rule,
                ),
            )
            conn.commit()
            return int(cur.lastrowid), True
        except sqlite3.IntegrityError:
            conn.rollback()
            row = conn.execute(
                "SELECT id FROM events WHERE idem_key = ?", (idem_key,)
            ).fetchone()
            return (int(row["id"]) if row else -1), False
        finally:
            conn.close()

    def set_event_status(self, event_id: int, status: str, note: Optional[str] = None) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE events SET status = ?, note = COALESCE(?, note) WHERE id = ?",
                (status, note, event_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            return _event_dict(row) if row else None
        finally:
            conn.close()

    # ---------- 派工与运行 ----------

    def create_run(self, event_id: int, rule: Any) -> int:
        conn = self.connect()
        try:
            cur = conn.execute(
                """INSERT INTO runs
                   (event_id, rule, agent, group_name, parallel_limit, subject, cwd, skill,
                    ask, sandbox, runtime, model, tell, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
                (
                    event_id,
                    rule.name,
                    rule.agent,
                    rule.group,
                    rule.parallel,
                    "",
                    rule.cwd,
                    rule.skill,
                    rule.ask,
                    rule.sandbox,
                    getattr(rule, "runtime", "codex"),
                    rule.model,
                    rule.tell,
                    iso(utc_now()),
                ),
            )
            run_id = int(cur.lastrowid)
            subject = conn.execute(
                "SELECT subject FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if subject:
                conn.execute(
                    "UPDATE runs SET subject = ? WHERE id = ?",
                    (subject["subject"] or "", run_id),
                )
            conn.commit()
            return run_id
        finally:
            conn.close()

    def lease(self, agent: str, ttl_seconds: int) -> Optional[Dict[str, Any]]:
        """领一个活。返回 run+event 的快照，没有活返回 None。"""
        now_dt = utc_now()
        now = iso(now_dt)
        expires = iso(now_dt + timedelta(seconds=ttl_seconds))
        conn = self.connect()
        try:
            row = conn.execute(
                """SELECT r.*, e.source AS e_source, e.type AS e_type,
                          e.summary AS e_summary, e.link AS e_link,
                          e.payload AS e_payload, e.received_at AS e_received_at
                   FROM runs r JOIN events e ON e.id = r.event_id
                   WHERE r.status = 'queued'
                     AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= ?)
                     AND r.agent = ?
                     AND (SELECT COUNT(*) FROM runs r2
                          WHERE r2.group_name = r.group_name AND r2.status = 'leased'
                         ) < r.parallel_limit
                     AND NOT EXISTS (
                          SELECT 1 FROM runs r3
                          WHERE r3.group_name = r.group_name
                            AND r3.subject = r.subject
                            AND r3.status IN ('leased', 'waiting_human')
                            AND r3.id <> r.id)
                   ORDER BY r.id
                   LIMIT 1""",
                (now, agent),
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                """UPDATE runs
                   SET status = 'leased', attempts = attempts + 1,
                       lease_expires_at = ?, leased_by = ?, started_at = COALESCE(started_at, ?)
                   WHERE id = ? AND status = 'queued'""",
                (expires, agent, now, row["id"]),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return None
            conn.execute("UPDATE events SET status = 'leased' WHERE id = ?", (row["event_id"],))
            conn.commit()
            leased = dict(row)
            leased["status"] = "leased"
            leased["attempts"] = int(leased["attempts"]) + 1
            leased["lease_expires_at"] = expires
            return leased
        finally:
            conn.close()

    def conclude(
        self,
        run_id: int,
        status: str,
        summary: str = "",
        session_ref: Optional[str] = None,
        artifacts: Optional[List[str]] = None,
        report: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """回写结论。幂等：已终结的 run 原样返回。"""
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("run %s 不存在" % run_id)
            if row["status"] in RUN_FINAL:
                event = conn.execute(
                    "SELECT status FROM events WHERE id = ?", (row["event_id"],)
                ).fetchone()
                return {
                    "run_id": run_id,
                    "status": row["status"],
                    "event_status": event["status"] if event else None,
                    "retried": False,
                    "idempotent": True,
                }

            now = iso(utc_now())
            payload = json.dumps(report, ensure_ascii=False) if report is not None else None
            art = json.dumps(artifacts, ensure_ascii=False) if artifacts is not None else None

            if status == "completed":
                conn.execute(
                    """UPDATE runs SET status='completed', ended_at=?, result_status=?,
                            summary=?, session_ref=COALESCE(?, session_ref),
                            artifacts=COALESCE(?, artifacts), report=COALESCE(?, report),
                            error=NULL, lease_expires_at=NULL
                       WHERE id=?""",
                    (now, "completed", summary, session_ref, art, payload, run_id),
                )
                conn.execute(
                    "UPDATE events SET status='done' WHERE id=?", (row["event_id"],)
                )
                conn.commit()
                return {
                    "run_id": run_id,
                    "status": "completed",
                    "event_status": "done",
                    "retried": False,
                    "idempotent": False,
                }

            if status == "need_decision":
                conn.execute(
                    """UPDATE runs SET status='waiting_human', ended_at=?, result_status=?,
                            summary=?, session_ref=COALESCE(?, session_ref),
                            artifacts=COALESCE(?, artifacts), report=COALESCE(?, report),
                            error=NULL, lease_expires_at=NULL
                       WHERE id=?""",
                    (now, "waiting_human", summary, session_ref, art, payload, run_id),
                )
                conn.execute(
                    "UPDATE events SET status='waiting_human' WHERE id=?", (row["event_id"],)
                )
                conn.commit()
                return {
                    "run_id": run_id,
                    "status": "waiting_human",
                    "event_status": "waiting_human",
                    "retried": False,
                    "idempotent": False,
                }

            if status != "failed":
                raise ValueError("未知的结论状态：%s" % status)

            retried = self._retry_or_fail(
                conn, row, error or summary or "unknown failure", now
            )
            conn.commit()
            run_status = conn.execute(
                "SELECT status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()["status"]
            event_status = conn.execute(
                "SELECT status FROM events WHERE id = ?", (row["event_id"],)
            ).fetchone()["status"]
            return {
                "run_id": run_id,
                "status": run_status,
                "event_status": event_status,
                "retried": retried,
                "idempotent": False,
            }
        finally:
            conn.close()

    def _retry_or_fail(
        self, conn: sqlite3.Connection, row: sqlite3.Row, error: str, now: str
    ) -> bool:
        attempts = int(row["attempts"])
        if attempts < self.max_attempts:
            backoff = min(
                self.retry_base_seconds * (2 ** (attempts - 1)), self.retry_max_seconds
            )
            next_at = iso(utc_now() + timedelta(seconds=backoff))
            conn.execute(
                """UPDATE runs SET status='queued', next_attempt_at=?, lease_expires_at=NULL,
                        leased_by=NULL, ended_at=NULL, error=?, summary=? WHERE id=?""",
                (next_at, error, error, row["id"]),
            )
            conn.execute(
                "UPDATE events SET status='queued', note=? WHERE id=?",
                ("第 %d 次失败，%ds 后重试：%s" % (attempts, backoff, error[:200]), row["event_id"]),
            )
            return True
        conn.execute(
            """UPDATE runs SET status='failed', ended_at=?, result_status='failed',
                    error=?, summary=?, lease_expires_at=NULL WHERE id=?""",
            (now, error, error, row["id"]),
        )
        conn.execute(
            "UPDATE events SET status='failed', note=? WHERE id=?",
            ("连续失败 %d 次：%s" % (attempts, error[:200]), row["event_id"]),
        )
        return False

    def sweep_expired_leases(self) -> int:
        """租约过期（worker 掉线/卡死）→ 重派或失败。"""
        now = iso(utc_now())
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT * FROM runs
                   WHERE status='leased' AND lease_expires_at IS NOT NULL
                     AND lease_expires_at <= ?""",
                (now,),
            ).fetchall()
            for row in rows:
                self._retry_or_fail(conn, row, "租约过期：worker 未回写结论", now)
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    # ---------- 读 ----------

    def recent_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT r.*, e.source AS e_source, e.type AS e_type,
                          e.summary AS e_summary, e.link AS e_link
                   FROM runs r JOIN events e ON e.id = r.event_id
                   ORDER BY r.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [_run_dict(r) for r in rows]
        finally:
            conn.close()

    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute(
                """SELECT r.*, e.source AS e_source, e.type AS e_type,
                          e.summary AS e_summary, e.link AS e_link
                   FROM runs r JOIN events e ON e.id = r.event_id
                   WHERE r.id = ?""",
                (run_id,),
            ).fetchone()
            return _run_dict(row) if row else None
        finally:
            conn.close()

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [_event_dict(r) for r in rows]
        finally:
            conn.close()

    def inbox(self) -> List[Dict[str, Any]]:
        """待我决定的事。P0 只读展示，决策动作在 P1。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT r.*, e.source AS e_source, e.type AS e_type,
                          e.summary AS e_summary, e.link AS e_link
                   FROM runs r JOIN events e ON e.id = r.event_id
                   WHERE r.status = 'waiting_human'
                   ORDER BY r.id DESC"""
            ).fetchall()
            return [_run_dict(r) for r in rows]
        finally:
            conn.close()

    def expired_lease_count(self) -> int:
        now = iso(utc_now())
        conn = self.connect()
        try:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM runs
                   WHERE status='leased' AND lease_expires_at IS NOT NULL
                     AND lease_expires_at <= ?""",
                (now,),
            ).fetchone()
            return int(row["n"])
        finally:
            conn.close()

    def counts(self) -> Dict[str, int]:
        conn = self.connect()
        try:
            events = {
                r["status"]: int(r["n"])
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
                )
            }
            runs = {
                r["status"]: int(r["n"])
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM runs GROUP BY status"
                )
            }
            return {"events": events, "runs": runs}
        finally:
            conn.close()


def _event_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    if data.get("payload"):
        try:
            data["payload"] = json.loads(data["payload"])
        except ValueError:
            pass
    return data


def _run_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    for key in ("report", "artifacts"):
        if data.get(key):
            try:
                data[key] = json.loads(data[key])
            except ValueError:
                pass
    return data
