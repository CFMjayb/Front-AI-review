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
  num           INTEGER,            -- stable human-facing catalog number (#42)
  direction     TEXT NOT NULL,
  counterparty  TEXT NOT NULL,
  counterparty_email TEXT,
  summary       TEXT NOT NULL,
  channel       TEXT NOT NULL,
  source_ref    TEXT NOT NULL,
  source_link   TEXT,
  category      TEXT,
  fyi           INTEGER DEFAULT 0,   -- informational/notification; separate brief section, auto-clears 24h
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

-- Sender rules — pre-classify known senders before Claude is invoked.
-- email: exact address (lower) or @domain.com pattern; exact takes precedence.
-- action: exclude | fyi | force-category | subscribe
CREATE TABLE IF NOT EXISTS sender_rules (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  email           TEXT NOT NULL UNIQUE,
  action          TEXT NOT NULL,
  category        TEXT,
  direction       TEXT,
  importance      INTEGER,
  subject_pattern TEXT,
  notes           TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sender_rules_email ON sender_rules(email);

-- Persistent AI guidance — standing instructions injected into the analysis prompt.
-- scope: all | category:X | sender:domain.com
CREATE TABLE IF NOT EXISTS guidance (
  key        TEXT PRIMARY KEY,
  body       TEXT NOT NULL,
  scope      TEXT NOT NULL DEFAULT 'all',
  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

-- Resolution feedback log — one row per resolve/snooze/drop. The learning
-- substrate: how Jay triages (what he drops as noise, what he acts on fast vs
-- defers) feeds noise-filtering + importance ranking over time.
CREATE TABLE IF NOT EXISTS feedback (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                 TEXT NOT NULL,
  action             TEXT NOT NULL,   -- done | dropped | snoozed
  loop_id            TEXT,
  num                INTEGER,
  direction          TEXT,
  channel            TEXT,
  category           TEXT,
  counterparty       TEXT,
  counterparty_email TEXT,
  importance         INTEGER,
  due_at             TEXT,
  age_hours          REAL,            -- first_seen -> resolution
  snooze_until       TEXT,
  reason             TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_action ON feedback(action);
"""


def _db_path() -> Path:
    return Path(os.environ.get("COS_DB_PATH") or DEFAULT_DB_PATH)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def loop_id(channel: str, source_ref: str, direction: str) -> str:
    raw = f"{channel}|{source_ref}|{direction}".encode("utf-8")
    return f"{channel}-{hashlib.sha1(raw).hexdigest()[:10]}-{direction}"


def _migrate(conn) -> None:
    """Idempotent schema migrations for existing databases."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(loops)").fetchall()]
    if "num" not in cols:
        conn.execute("ALTER TABLE loops ADD COLUMN num INTEGER")
        # Backfill stable numbers in catalog order (oldest first).
        rows = conn.execute("SELECT id FROM loops ORDER BY first_seen ASC, rowid ASC").fetchall()
        for i, r in enumerate(rows, 1):
            conn.execute("UPDATE loops SET num=? WHERE id=?", (i, r[0]))
    if "fyi" not in cols:
        conn.execute("ALTER TABLE loops ADD COLUMN fyi INTEGER DEFAULT 0")
        conn.execute("UPDATE loops SET fyi=1 WHERE summary LIKE 'FYI%'")
    if "deferred" not in cols:
        conn.execute("ALTER TABLE loops ADD COLUMN deferred INTEGER DEFAULT 0")
    if "source_date" not in cols:
        conn.execute("ALTER TABLE loops ADD COLUMN source_date TEXT")
    for col in ("urgency", "action_type", "sentiment", "suggested_assignee"):
        if col not in cols:
            conn.execute(f"ALTER TABLE loops ADD COLUMN {col} TEXT")
    if "escalation_risk" not in cols:
        conn.execute("ALTER TABLE loops ADD COLUMN escalation_risk REAL DEFAULT 0.0")
    if "dedup_key" not in cols:
        conn.execute("ALTER TABLE loops ADD COLUMN dedup_key TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loops_dedup ON loops(dedup_key)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_loops_num ON loops(num)")


def _next_num(conn) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(num), 0) + 1 FROM loops").fetchone()[0])


@contextmanager
def _connect():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
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
                due_at: str = "", status: str = "", last_activity: str = "",
                fyi: bool = False, source_date: str = "",
                urgency: str = "", action_type: str = "", sentiment: str = "",
                escalation_risk: float = 0.0, suggested_assignee: str = "",
                dedup_key: str = "") -> dict:
    """Insert a loop or merge into the existing one. Returns the stored row.

    Merge rules: first_seen, source_date, and a manually-set status are preserved;
    machine fields (summary, last_activity, confidence, links) refresh.
    """
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"invalid direction: {direction!r}")
    if status and status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")

    lid = loop_id(channel, source_ref, direction)
    now = now_iso()
    last_activity = last_activity or now

    with _connect() as conn:
        existing_row = conn.execute("SELECT * FROM loops WHERE id = ?", (lid,)).fetchone()
        existing = dict(existing_row) if existing_row else None

        if existing is None:
            conn.execute(
                """INSERT INTO loops (id, num, direction, counterparty, counterparty_email,
                       summary, channel, source_ref, source_link, category, fyi, status,
                       importance, confidence, due_at, source_date, first_seen, last_activity,
                       last_reviewed, urgency, action_type, sentiment, escalation_risk,
                       suggested_assignee, dedup_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (lid, _next_num(conn), direction, counterparty, counterparty_email, summary,
                 channel, source_ref, source_link, category, 1 if fyi else 0,
                 status or "open", importance, confidence, due_at or None,
                 source_date or None, now, last_activity, now,
                 urgency or None, action_type or None, sentiment or None,
                 escalation_risk or None, suggested_assignee or None,
                 dedup_key or None),
            )
        else:
            if existing["status"] in MANUAL_STATUSES:
                new_status = existing["status"]
            else:
                new_status = status or existing["status"]
            conn.execute(
                """UPDATE loops SET counterparty=?, counterparty_email=?, summary=?,
                       source_link=?, category=?, fyi=?, status=?, importance=?, confidence=?,
                       due_at=?, last_activity=?, last_reviewed=?,
                       source_date=COALESCE(source_date, ?),
                       urgency=?, action_type=?, sentiment=?,
                       escalation_risk=?, suggested_assignee=?,
                       dedup_key=COALESCE(dedup_key, ?)
                   WHERE id=?""",
                (counterparty, counterparty_email or existing["counterparty_email"],
                 summary, source_link or existing["source_link"],
                 category or existing["category"], 1 if fyi else 0, new_status, importance,
                 confidence, due_at or existing["due_at"], last_activity, now,
                 source_date or None,
                 urgency or existing.get("urgency"), action_type or existing.get("action_type"),
                 sentiment or existing.get("sentiment"),
                 escalation_risk or existing.get("escalation_risk"),
                 suggested_assignee or existing.get("suggested_assignee"),
                 dedup_key or None, lid),
            )

        row = conn.execute("SELECT * FROM loops WHERE id = ?", (lid,)).fetchone()
    return _row_to_dict(row)


def get_loop_by_dedup_key(key: str) -> Optional[dict]:
    """Return the most-recently-active open loop with this dedup_key, or None."""
    if not key:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM loops WHERE dedup_key=? AND status NOT IN ('done','dropped') "
            "ORDER BY last_activity DESC LIMIT 1", (key,)).fetchone()
    return _row_to_dict(row)


def get_loop(loop_id_: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM loops WHERE id = ?", (loop_id_,)).fetchone()
    return _row_to_dict(row)


def get_loop_by_num(num: int) -> Optional[dict]:
    """Look up a loop by its stable catalog number (#num)."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM loops WHERE num = ?", (int(num),)).fetchone()
    return _row_to_dict(row)


def resolve_by_num(num: int, status: str, *, reason: str = "") -> Optional[dict]:
    """Resolve a loop by catalog number. Returns None if the number is unknown."""
    loop = get_loop_by_num(num)
    return resolve_loop(loop["id"], status, reason=reason) if loop else None


def snooze_by_num(num: int, until: str, *, reason: str = "") -> Optional[dict]:
    """Snooze a loop by catalog number. Returns None if the number is unknown."""
    loop = get_loop_by_num(num)
    return snooze_loop(loop["id"], until, reason=reason) if loop else None


def list_loops(*, direction: str = "", channel: str = "", status: str = "",
               overdue_only: bool = False, include_resolved: bool = False,
               deferred_only: bool = False) -> list[dict]:
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
    if deferred_only:
        clauses.append("deferred = 1")
    else:
        clauses.append("(deferred IS NULL OR deferred = 0)")

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT * FROM loops" + where +
        " ORDER BY importance DESC, "
        "CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC, last_activity ASC"
    )
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _age_hours(first_seen: Optional[str]) -> Optional[float]:
    if not first_seen:
        return None
    try:
        dt = datetime.datetime.strptime(first_seen, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        return round((datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600, 2)
    except ValueError:
        return None


def _record_feedback(conn, loop: dict, action: str, *, reason: str = "",
                     snooze_until: str = "") -> None:
    if not loop:
        return
    conn.execute(
        """INSERT INTO feedback (ts, action, loop_id, num, direction, channel, category,
               counterparty, counterparty_email, importance, due_at, age_hours,
               snooze_until, reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now_iso(), action, loop.get("id"), loop.get("num"), loop.get("direction"),
         loop.get("channel"), loop.get("category"), loop.get("counterparty"),
         loop.get("counterparty_email"), loop.get("importance"), loop.get("due_at"),
         _age_hours(loop.get("first_seen")), snooze_until or None, reason or None))


def list_feedback(action: str = "", since: str = "", limit: int = 1000) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if action:
        clauses.append("action = ?"); params.append(action)
    if since:
        clauses.append("ts >= ?"); params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM feedback{where} ORDER BY ts DESC LIMIT ?",
            params + [limit]).fetchall()
    return [dict(r) for r in rows]


def resolve_loop(loop_id_: str, status: str, *, reason: str = "") -> Optional[dict]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    with _connect() as conn:
        conn.execute("UPDATE loops SET status=?, last_reviewed=? WHERE id=?",
                     (status, now_iso(), loop_id_))
        row = conn.execute("SELECT * FROM loops WHERE id = ?", (loop_id_,)).fetchone()
        if row is not None and status in ("done", "dropped"):
            _record_feedback(conn, dict(row), status, reason=reason)
    return _row_to_dict(row)


def snooze_loop(loop_id_: str, until: str, *, reason: str = "") -> Optional[dict]:
    with _connect() as conn:
        conn.execute("UPDATE loops SET status='snoozed', snooze_until=?, last_reviewed=? "
                     "WHERE id=?", (until, now_iso(), loop_id_))
        row = conn.execute("SELECT * FROM loops WHERE id = ?", (loop_id_,)).fetchone()
        if row is not None:
            _record_feedback(conn, dict(row), "snoozed", reason=reason, snooze_until=until)
    return _row_to_dict(row)


def patch_loop(loop_id_: str, *, notes: Optional[str] = None,
               category: Optional[str] = None, fyi: Optional[bool] = None,
               deferred: Optional[bool] = None) -> Optional[dict]:
    """Update mutable human-editable fields without touching status or ingestion fields."""
    sets, vals = [], []
    if notes is not None:
        sets.append("notes=?"); vals.append(notes)
    if category is not None:
        sets.append("category=?"); vals.append(category)
    if fyi is not None:
        sets.append("fyi=?"); vals.append(int(fyi))
    if deferred is not None:
        sets.append("deferred=?"); vals.append(int(deferred))
    if not sets:
        return get_loop(loop_id_)
    vals.append(now_iso()); vals.append(loop_id_)
    with _connect() as conn:
        conn.execute(f"UPDATE loops SET {', '.join(sets)}, last_reviewed=? WHERE id=?", vals)
        row = conn.execute("SELECT * FROM loops WHERE id=?", (loop_id_,)).fetchone()
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


def list_events_overlapping(start_iso: str, end_iso: str) -> list[dict]:
    """Events that overlap the [start, end) window — so a multi-day all-day event
    appears on every day it spans. All-day banners sort first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE start_at < ? AND COALESCE(end_at, start_at) > ? "
            "ORDER BY is_all_day DESC, start_at",
            (end_iso, start_iso)).fetchall()
    return [_event_row(r) for r in rows]


# ── Sender rules (FILTER-1 / PRIORITY-1) ────────────────────────────────────

def upsert_sender_rule(*, email: str, action: str, category: str = "",
                       direction: str = "", importance: int = 0,
                       subject_pattern: str = "", notes: str = "") -> dict:
    """email is an exact address (lower) or @domain.com pattern."""
    email = email.strip().lower()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO sender_rules (email, action, category, direction, importance,
                   subject_pattern, notes, created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(email) DO UPDATE SET
                   action=excluded.action, category=excluded.category,
                   direction=excluded.direction, importance=excluded.importance,
                   subject_pattern=excluded.subject_pattern, notes=excluded.notes""",
            (email, action, category or None, direction or None,
             importance or None, subject_pattern or None, notes or None, now_iso()))
        row = conn.execute("SELECT * FROM sender_rules WHERE email=?", (email,)).fetchone()
    return _row_to_dict(row)


def list_sender_rules() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM sender_rules ORDER BY email").fetchall()
    return [dict(r) for r in rows]


def delete_sender_rule(email: str) -> bool:
    email = email.strip().lower()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM sender_rules WHERE email=?", (email,))
    return cur.rowcount > 0


def get_sender_rule_for_email(email: str) -> Optional[dict]:
    """Exact match first, then longest-matching @domain.com suffix."""
    email = email.strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    with _connect() as conn:
        # Exact match
        row = conn.execute("SELECT * FROM sender_rules WHERE email=?", (email,)).fetchone()
        if row:
            return _row_to_dict(row)
        # Domain-pattern match — try @sub.domain.com, then @domain.com, etc.
        parts = domain.split(".")
        for i in range(len(parts) - 1):
            pattern = "@" + ".".join(parts[i:])
            row = conn.execute("SELECT * FROM sender_rules WHERE email=?", (pattern,)).fetchone()
            if row:
                return _row_to_dict(row)
    return None


# ── Guidance (GUIDANCE-1) ────────────────────────────────────────────────────

def upsert_guidance(*, key: str, body: str, scope: str = "all",
                    active: bool = True) -> dict:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO guidance (key, body, scope, active, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   body=excluded.body, scope=excluded.scope, active=excluded.active""",
            (key.strip().lower(), body.strip(), scope.strip() or "all",
             1 if active else 0, now_iso()))
        row = conn.execute("SELECT * FROM guidance WHERE key=?",
                           (key.strip().lower(),)).fetchone()
    return _row_to_dict(row)


def list_guidance(*, active_only: bool = False) -> list[dict]:
    with _connect() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM guidance WHERE active=1 ORDER BY key").fetchall()
        else:
            rows = conn.execute("SELECT * FROM guidance ORDER BY key").fetchall()
    return [dict(r) for r in rows]


def delete_guidance(key: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM guidance WHERE key=?", (key.strip().lower(),))
    return cur.rowcount > 0


def delete_events_before(iso: str) -> int:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM events WHERE start_at < ?", (iso,))
        return cur.rowcount
