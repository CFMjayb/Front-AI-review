"""Chief-of-Staff open-loop ledger — SQLite source of truth.

All persistence for loops / people / memory lives here. Surfaces (the CoS MCP
server, the daily briefing) and ingestion call these functions; nothing else
in the system touches SQL or knows the schema. Per the design, SQLite is the
source of truth and the Obsidian vault is rendered from it (M2).

A loop is one open commitment:
  - direction 'i_owe'      → the counterparty is waiting on Jay
  - direction 'owed_to_me' → Jay is waiting on the counterparty

Idempotency: id = hash(channel, source_ref, direction). Re-sweeping the same
thread UPDATEs the row rather than duplicating it, and never clobbers a status
Jay has set manually (done / dropped / snoozed).
"""
import datetime
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cos.db"

VALID_DIRECTIONS = {"i_owe", "owed_to_me"}
VALID_STATUSES = {"open", "waiting", "snoozed", "done", "dropped"}

# Statuses that reflect a human decision — ingestion upserts must not overwrite them.
MANUAL_STATUSES = {"done", "dropped", "snoozed"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS loops (
  id            TEXT PRIMARY KEY,
  direction     TEXT NOT NULL,
  counterparty  TEXT NOT NULL,
  counterparty_email TEXT,
  summary       TEXT NOT NULL,
  channel       TEXT NOT NULL,
  source_ref    TEXT NOT NULL,
  source_link   TEXT,
  category      TEXT,
  status        TEXT NOT NULL DEFAULT 'open',
  importance    INTEGER DEFAULT 3,
  confidence    REAL,
  due_at        TEXT,
  snooze_until  TEXT,
  first_seen    TEXT NOT NULL,
  last_activity TEXT,
  last_reviewed TEXT,
  notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_loops_status    ON loops(status);
CREATE INDEX IF NOT EXISTS idx_loops_direction ON loops(direction);

CREATE TABLE IF NOT EXISTS people (
  key        TEXT PRIMARY KEY,
  name       TEXT,
  role       TEXT,
  importance INTEGER DEFAULT 3,
  notes      TEXT
);

CREATE TABLE IF NOT EXISTS memory (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Cost gate for channels without a Front-style processed tag (Outlook/Teams):
-- one analysis per thread state. marker = the thread's last_activity timestamp.
CREATE TABLE IF NOT EXISTS seen (
  channel    TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  marker     TEXT NOT NULL,
  seen_at    TEXT NOT NULL,
  PRIMARY KEY (channel, source_ref)
);

-- Calendar events cache. Agent sweeps fill this; the autonomous briefing reads it.
CREATE TABLE IF NOT EXISTS events (
  id          TEXT PRIMARY KEY,   -- provider event id (or hash fallback)
  calendar    TEXT NOT NULL,      -- which calendar (owner email / label)
  subject     TEXT,
  start_at    TEXT NOT NULL,      -- ISO; all-day → date T00:00:00Z
  end_at      TEXT,
  location    TEXT,
  organizer   TEXT,               -- organizer email
  attendees   TEXT,               -- JSON list of emails
  source_link TEXT,
  is_all_day  INTEGER DEFAULT 0,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);
"""


def _db_path() -> Path:
    return Path(os.environ.get("COS_DB_PATH") or DEFAULT_DB_PATH)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def loop_id(channel: str, source_ref: str, direction: str) -> str:
    raw = f"{channel}|{source_ref}|{direction}".encode("utf-8")
    return f"{channel}-{hashlib.sha1(raw).hexdigest()[:10]}-{direction}"


@contextmanager
def _connect():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create the database file and schema if absent (also done lazily on connect)."""
    with _connect():
        pass


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


# ── Loops ───────────────────────────────────────────────────────────────────

def upsert_loop(*, direction: str, counterparty: str, summary: str, channel: str,
                source_ref: str, source_link: str = "", counterparty_email: str = "",
                category: str = "", importance: int = 3, confidence: float = 0.0,
                due_at: str = "", status: str = "", last_activity: str = "") -> dict:
    """Insert a loop or merge into the existing one. Returns the stored row.

    Merge rules: first_seen and a manually-set status (done/dropped/snoozed) are
    preserved; machine fields (summary, last_activity, confidence, links) refresh.
    """
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"invalid direction: {direction!r}")
    if status and status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")

    lid = loop_id(channel, source_ref, direction)
    now = now_iso()
    last_activity = last_activity or now

    with _connect() as conn:
        existing = conn.execute("SELECT * FROM loops WHERE id = ?", (lid,)).fetchone()

        if existing is None:
            conn.execute(
                """INSERT INTO loops (id, direction, counterparty, counterparty_email,
                       summary, channel, source_ref, source_link, category, status,
                       importance, confidence, due_at, first_seen, last_activity,
                       last_reviewed)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (lid, direction, counterparty, counterparty_email, summary, channel,
                 source_ref, source_link, category, status or "open", importance,
                 confidence, due_at or None, now, last_activity, now),
            )
        else:
            # Preserve a human-set status; otherwise accept the caller's or keep current.
            if existing["status"] in MANUAL_STATUSES:
                new_status = existing["status"]
            else:
                new_status = status or existing["status"]
            conn.execute(
                """UPDATE loops SET counterparty=?, counterparty_email=?, summary=?,
                       source_link=?, category=?, status=?, importance=?, confidence=?,
                       due_at=?, last_activity=?, last_reviewed=?
                   WHERE id=?""",
                (counterparty, counterparty_email or existing["counterparty_email"],
                 summary, source_link or existing["source_link"],
                 category or existing["category"], new_status, importance, confidence,
                 due_at or existing["due_at"], last_activity, now, lid),
            )

        row = conn.execute("SELECT * FROM loops WHERE id = ?", (lid,)).fetchone()
    return _row_to_dict(row)


def get_loop(loop_id_: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM loops WHERE id = ?", (loop_id_,)).fetchone()
    return _row_to_dict(row)


def list_loops(*, direction: str = "", channel: str = "", status: str = "",
               overdue_only: bool = False, include_resolved: bool = False) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if direction:
        clauses.append("direction = ?"); params.append(direction)
    if channel:
        clauses.append("channel = ?"); params.append(channel)
    if status:
        clauses.append("status = ?"); params.append(status)
    elif not include_resolved:
        clauses.append("status NOT IN ('done','dropped')")
    if overdue_only:
        clauses.append("due_at IS NOT NULL AND due_at < ?"); params.append(now_iso())

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT * FROM loops" + where +
        " ORDER BY importance DESC, "
        "CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC, last_activity ASC"
    )
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def resolve_loop(loop_id_: str, status: str) -> Optional[dict]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    with _connect() as conn:
        conn.execute("UPDATE loops SET status=?, last_reviewed=? WHERE id=?",
                     (status, now_iso(), loop_id_))
        row = conn.execute("SELECT * FROM loops WHERE id = ?", (loop_id_,)).fetchone()
    return _row_to_dict(row)


def snooze_loop(loop_id_: str, until: str) -> Optional[dict]:
    with _connect() as conn:
        conn.execute("UPDATE loops SET status='snoozed', snooze_until=?, last_reviewed=? "
                     "WHERE id=?", (until, now_iso(), loop_id_))
        row = conn.execute("SELECT * FROM loops WHERE id = ?", (loop_id_,)).fetchone()
    return _row_to_dict(row)


def stats() -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM loops").fetchone()[0]
        by_dir = {r[0]: r[1] for r in conn.execute(
            "SELECT direction, COUNT(*) FROM loops WHERE status NOT IN ('done','dropped') "
            "GROUP BY direction").fetchall()}
        by_status = {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM loops GROUP BY status").fetchall()}
        overdue = conn.execute(
            "SELECT COUNT(*) FROM loops WHERE status NOT IN ('done','dropped') "
            "AND due_at IS NOT NULL AND due_at < ?", (now_iso(),)).fetchone()[0]
    return {"total": total, "open_by_direction": by_dir, "by_status": by_status,
            "overdue": overdue}


# ── People & memory ──────────────────────────────────────────────────────────

def people_upsert(*, key: str, name: str = "", role: str = "", importance: int = 3,
                  notes: str = "") -> dict:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO people (key, name, role, importance, notes)
               VALUES (?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   name=excluded.name, role=excluded.role,
                   importance=excluded.importance, notes=excluded.notes""",
            (key.lower(), name, role, importance, notes),
        )
        row = conn.execute("SELECT * FROM people WHERE key=?", (key.lower(),)).fetchone()
    return _row_to_dict(row)


def list_people() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM people ORDER BY importance DESC, name").fetchall()
    return [dict(r) for r in rows]


def remember(key: str, value: str) -> dict:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO memory (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    return {"key": key, "value": value}


def get_memory(key: str = "") -> dict:
    with _connect() as conn:
        if key:
            row = conn.execute("SELECT value FROM memory WHERE key=?", (key,)).fetchone()
            return {key: row[0]} if row else {}
        rows = conn.execute("SELECT key, value FROM memory").fetchall()
    return {r[0]: r[1] for r in rows}


# ── Seen gate (one analysis per thread state) ────────────────────────────────

def was_seen(channel: str, source_ref: str, marker: str) -> bool:
    """True if this thread was already analyzed at this exact state (marker)."""
    with _connect() as conn:
        row = conn.execute("SELECT marker FROM seen WHERE channel=? AND source_ref=?",
                           (channel, source_ref)).fetchone()
    return row is not None and row[0] == marker


def mark_seen(channel: str, source_ref: str, marker: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO seen (channel, source_ref, marker, seen_at) VALUES (?,?,?,?) "
            "ON CONFLICT(channel, source_ref) DO UPDATE SET marker=excluded.marker, "
            "seen_at=excluded.seen_at",
            (channel, source_ref, marker, now_iso()))


# ── Calendar events cache ────────────────────────────────────────────────────

import json as _json


def upsert_event(*, id: str, calendar: str, subject: str = "", start_at: str,
                 end_at: str = "", location: str = "", organizer: str = "",
                 attendees: list[str] | None = None, source_link: str = "",
                 is_all_day: bool = False) -> dict:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO events (id, calendar, subject, start_at, end_at, location,
                   organizer, attendees, source_link, is_all_day, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   calendar=excluded.calendar, subject=excluded.subject,
                   start_at=excluded.start_at, end_at=excluded.end_at,
                   location=excluded.location, organizer=excluded.organizer,
                   attendees=excluded.attendees, source_link=excluded.source_link,
                   is_all_day=excluded.is_all_day, updated_at=excluded.updated_at""",
            (id, calendar, subject, start_at, end_at or None, location or None,
             organizer or None, _json.dumps(attendees or []), source_link or None,
             1 if is_all_day else 0, now_iso()))
        row = conn.execute("SELECT * FROM events WHERE id=?", (id,)).fetchone()
    return _event_row(row)


def _event_row(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    d["attendees"] = _json.loads(d.get("attendees") or "[]")
    d["is_all_day"] = bool(d.get("is_all_day"))
    return d


def list_events_between(start_iso: str, end_iso: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE start_at >= ? AND start_at < ? ORDER BY start_at",
            (start_iso, end_iso)).fetchall()
    return [_event_row(r) for r in rows]


def delete_events_before(iso: str) -> int:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM events WHERE start_at < ?", (iso,))
        return cur.rowcount
