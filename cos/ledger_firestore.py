"""Chief-of-Staff ledger — Firestore backend.

Mirrors cos/ledger_sqlite.py's public API exactly, so cos/ledger.py can swap
between them with LEDGER_BACKEND. Firestore is serverless + durable, so the same
ledger is shared by the 2-hourly pipeline, the 6 AM briefing, and the live MCP
server (and the desktop can point at it too).

Collections: loops, people, memory, seen, events, _counters.
Auth: Application Default Credentials (GCP_PROJECT selects the project).

Filtering/ordering for loops + events is done client-side (single-user scale, a
few hundred docs) to avoid composite-index management. Equality filters that are
index-free are pushed to Firestore.
"""
import datetime
import hashlib
import os
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

VALID_DIRECTIONS = {"i_owe", "owed_to_me"}
VALID_STATUSES = {"open", "waiting", "snoozed", "done", "dropped"}
MANUAL_STATUSES = {"done", "dropped", "snoozed"}

_LOOPS        = "loops"
_PEOPLE       = "people"
_MEMORY       = "memory"
_SEEN         = "seen"
_EVENTS       = "events"
_FEEDBACK     = "feedback"
_COUNTERS     = "_counters"
_SENDER_RULES = "sender_rules"
_GUIDANCE     = "guidance"

_client: Optional[firestore.Client] = None


def _db() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(project=os.environ.get("GCP_PROJECT") or None)
    return _client


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def loop_id(channel: str, source_ref: str, direction: str) -> str:
    raw = f"{channel}|{source_ref}|{direction}".encode("utf-8")
    return f"{channel}-{hashlib.sha1(raw).hexdigest()[:10]}-{direction}"


def init_db() -> None:
    """No-op for Firestore — collections are implicit. Ensures the counter exists."""
    ref = _db().collection(_COUNTERS).document(_LOOPS)
    if not ref.get().exists:
        ref.set({"next_num": 1})


def _loop_doc(snap) -> Optional[dict]:
    if snap is None or not snap.exists:
        return None
    return {**snap.to_dict(), "id": snap.id}


# ── Loops ───────────────────────────────────────────────────────────────────

def upsert_loop(*, direction: str, counterparty: str, summary: str, channel: str,
                source_ref: str, source_link: str = "", counterparty_email: str = "",
                category: str = "", importance: int = 3, confidence: float = 0.0,
                due_at: str = "", status: str = "", last_activity: str = "",
                fyi: bool = False, source_date: str = "",
                urgency: str = "", action_type: str = "", sentiment: str = "",
                escalation_risk: float = 0.0, suggested_assignee: str = "",
                dedup_key: str = "") -> dict:
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"invalid direction: {direction!r}")
    if status and status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")

    db = _db()
    lid = loop_id(channel, source_ref, direction)
    now = now_iso()
    last_activity = last_activity or now
    ref = db.collection(_LOOPS).document(lid)
    counter_ref = db.collection(_COUNTERS).document(_LOOPS)

    @firestore.transactional
    def _txn(txn):
        snap = ref.get(transaction=txn)
        if not snap.exists:
            csnap = counter_ref.get(transaction=txn)
            num = (csnap.to_dict() or {}).get("next_num", 1) if csnap.exists else 1
            txn.set(counter_ref, {"next_num": num + 1}, merge=True)
            txn.set(ref, {
                "num": num, "direction": direction, "counterparty": counterparty,
                "counterparty_email": counterparty_email, "summary": summary,
                "channel": channel, "source_ref": source_ref, "source_link": source_link,
                "category": category, "fyi": bool(fyi), "status": status or "open",
                "importance": importance, "confidence": confidence, "due_at": due_at or None,
                "source_date": source_date or None,
                "urgency": urgency or None, "action_type": action_type or None,
                "sentiment": sentiment or None, "escalation_risk": escalation_risk or None,
                "suggested_assignee": suggested_assignee or None,
                "dedup_key": dedup_key or None,
                "snooze_until": None, "first_seen": now, "last_activity": last_activity,
                "last_reviewed": now, "notes": None,
            })
        else:
            ex = snap.to_dict()
            new_status = (ex["status"] if ex.get("status") in MANUAL_STATUSES
                          else (status or ex.get("status")))
            updates = {
                "counterparty": counterparty,
                "counterparty_email": counterparty_email or ex.get("counterparty_email"),
                "summary": summary, "source_link": source_link or ex.get("source_link"),
                "category": category or ex.get("category"), "fyi": bool(fyi),
                "status": new_status, "importance": importance, "confidence": confidence,
                "due_at": due_at or ex.get("due_at"), "last_activity": last_activity,
                "last_reviewed": now,
                "urgency": urgency or ex.get("urgency"),
                "action_type": action_type or ex.get("action_type"),
                "sentiment": sentiment or ex.get("sentiment"),
                "escalation_risk": escalation_risk or ex.get("escalation_risk"),
                "suggested_assignee": suggested_assignee or ex.get("suggested_assignee"),
            }
            if source_date and not ex.get("source_date"):
                updates["source_date"] = source_date
            if dedup_key and not ex.get("dedup_key"):
                updates["dedup_key"] = dedup_key
            txn.update(ref, updates)

    _txn(db.transaction())
    return _loop_doc(ref.get())


def get_loop(loop_id_: str) -> Optional[dict]:
    return _loop_doc(_db().collection(_LOOPS).document(loop_id_).get())


def get_loop_by_num(num: int) -> Optional[dict]:
    docs = list(_db().collection(_LOOPS)
                .where(filter=FieldFilter("num", "==", int(num))).limit(1).stream())
    return _loop_doc(docs[0]) if docs else None


def resolve_by_num(num: int, status: str, *, reason: str = "") -> Optional[dict]:
    loop = get_loop_by_num(num)
    return resolve_loop(loop["id"], status, reason=reason) if loop else None


def snooze_by_num(num: int, until: str, *, reason: str = "") -> Optional[dict]:
    loop = get_loop_by_num(num)
    return snooze_loop(loop["id"], until, reason=reason) if loop else None


def get_loop_by_dedup_key(key: str) -> Optional[dict]:
    """Return the most-recently-active open loop with this dedup_key, or None."""
    if not key:
        return None
    docs = list(_db().collection(_LOOPS)
                .where(filter=FieldFilter("dedup_key", "==", key))
                .stream())
    candidates = [_loop_doc(d) for d in docs
                  if d.to_dict().get("status") not in ("done", "dropped")]
    if not candidates:
        return None
    return max(candidates, key=lambda l: l.get("last_activity") or "")


def _age_hours(first_seen: Optional[str]) -> Optional[float]:
    if not first_seen:
        return None
    try:
        dt = datetime.datetime.strptime(first_seen, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        return round((datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600, 2)
    except ValueError:
        return None


def _record_feedback(loop: dict, action: str, *, reason: str = "",
                     snooze_until: str = "") -> None:
    if not loop:
        return
    _db().collection(_FEEDBACK).add({
        "ts": now_iso(), "action": action, "loop_id": loop.get("id"),
        "num": loop.get("num"), "direction": loop.get("direction"),
        "channel": loop.get("channel"), "category": loop.get("category"),
        "counterparty": loop.get("counterparty"),
        "counterparty_email": loop.get("counterparty_email"),
        "importance": loop.get("importance"), "due_at": loop.get("due_at"),
        "age_hours": _age_hours(loop.get("first_seen")),
        "snooze_until": snooze_until or None, "reason": reason or None,
    })


def list_feedback(action: str = "", since: str = "", limit: int = 1000) -> list[dict]:
    q = _db().collection(_FEEDBACK)
    if action:
        q = q.where(filter=FieldFilter("action", "==", action))
    if since:
        q = q.where(filter=FieldFilter("ts", ">=", since))
    docs = [s.to_dict() for s in q.stream()]
    docs.sort(key=lambda d: d.get("ts") or "", reverse=True)
    return docs[:limit]


def _order_key(loop: dict):
    # importance desc, then due_at asc (NULLs last), then last_activity asc
    due = loop.get("due_at")
    return (-(loop.get("importance") or 0), due is None, due or "",
            loop.get("last_activity") or "")


def list_loops(*, direction: str = "", channel: str = "", status: str = "",
               overdue_only: bool = False, include_resolved: bool = False,
               deferred_only: bool = False) -> list[dict]:
    q = _db().collection(_LOOPS)
    if direction:
        q = q.where(filter=FieldFilter("direction", "==", direction))
    if channel:
        q = q.where(filter=FieldFilter("channel", "==", channel))
    if status:
        q = q.where(filter=FieldFilter("status", "==", status))

    loops = [_loop_doc(s) for s in q.stream()]
    if not status and not include_resolved:
        loops = [l for l in loops if l["status"] not in ("done", "dropped")]
    if overdue_only:
        now = now_iso()
        loops = [l for l in loops if l.get("due_at") and l["due_at"] < now]
    if deferred_only:
        loops = [l for l in loops if l.get("deferred")]
    else:
        loops = [l for l in loops if not l.get("deferred")]
    loops.sort(key=_order_key)
    return loops


def resolve_loop(loop_id_: str, status: str, *, reason: str = "") -> Optional[dict]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    ref = _db().collection(_LOOPS).document(loop_id_)
    snap = ref.get()
    if not snap.exists:
        return None
    loop = _loop_doc(snap)
    ref.update({"status": status, "last_reviewed": now_iso()})
    if status in ("done", "dropped"):
        _record_feedback(loop, status, reason=reason)
    return _loop_doc(ref.get())


def snooze_loop(loop_id_: str, until: str, *, reason: str = "") -> Optional[dict]:
    ref = _db().collection(_LOOPS).document(loop_id_)
    snap = ref.get()
    if not snap.exists:
        return None
    loop = _loop_doc(snap)
    ref.update({"status": "snoozed", "snooze_until": until, "last_reviewed": now_iso()})
    _record_feedback(loop, "snoozed", reason=reason, snooze_until=until)
    return _loop_doc(ref.get())


def patch_loop(loop_id_: str, *, notes: Optional[str] = None,
               category: Optional[str] = None, fyi: Optional[bool] = None,
               deferred: Optional[bool] = None,
               front_archived: Optional[bool] = None) -> Optional[dict]:
    """Update mutable human-editable fields without touching status or ingestion fields."""
    updates: dict = {"last_reviewed": now_iso()}
    if notes is not None:
        updates["notes"] = notes
    if category is not None:
        updates["category"] = category
    if fyi is not None:
        updates["fyi"] = bool(fyi)
    if deferred is not None:
        updates["deferred"] = bool(deferred)
    if front_archived is not None:
        updates["front_archived"] = bool(front_archived)
    ref = _db().collection(_LOOPS).document(loop_id_)
    if not ref.get().exists:
        return None
    ref.update(updates)
    return _loop_doc(ref.get())


def stats() -> dict:
    loops = [s.to_dict() for s in _db().collection(_LOOPS).stream()]
    active = [l for l in loops if l.get("status") not in ("done", "dropped")]
    now = now_iso()
    by_dir: dict = {}
    for l in active:
        by_dir[l["direction"]] = by_dir.get(l["direction"], 0) + 1
    by_status: dict = {}
    for l in loops:
        by_status[l["status"]] = by_status.get(l["status"], 0) + 1
    overdue = sum(1 for l in active if l.get("due_at") and l["due_at"] < now)
    return {"total": len(loops), "open_by_direction": by_dir,
            "by_status": by_status, "overdue": overdue}


# ── People & memory ──────────────────────────────────────────────────────────

def people_upsert(*, key: str, name: str = "", role: str = "", importance: int = 3,
                  notes: str = "") -> dict:
    ref = _db().collection(_PEOPLE).document(key.lower())
    ref.set({"key": key.lower(), "name": name, "role": role,
             "importance": importance, "notes": notes})
    return ref.get().to_dict()


def list_people() -> list[dict]:
    people = [s.to_dict() for s in _db().collection(_PEOPLE).stream()]
    people.sort(key=lambda p: (-(p.get("importance") or 0), p.get("name") or ""))
    return people


def remember(key: str, value: str) -> dict:
    _db().collection(_MEMORY).document(key).set({"value": value})
    return {"key": key, "value": value}


def get_memory(key: str = "") -> dict:
    col = _db().collection(_MEMORY)
    if key:
        snap = col.document(key).get()
        return {key: snap.to_dict()["value"]} if snap.exists else {}
    return {s.id: s.to_dict().get("value") for s in col.stream()}


# ── Seen gate ────────────────────────────────────────────────────────────────

def _seen_id(channel: str, source_ref: str) -> str:
    return f"{channel}|{source_ref}"


def was_seen(channel: str, source_ref: str, marker: str) -> bool:
    snap = _db().collection(_SEEN).document(_seen_id(channel, source_ref)).get()
    return snap.exists and snap.to_dict().get("marker") == marker


def mark_seen(channel: str, source_ref: str, marker: str) -> None:
    _db().collection(_SEEN).document(_seen_id(channel, source_ref)).set(
        {"channel": channel, "source_ref": source_ref, "marker": marker,
         "seen_at": now_iso()})


# ── Calendar events ──────────────────────────────────────────────────────────

def upsert_event(*, id: str, calendar: str, subject: str = "", start_at: str,
                 end_at: str = "", location: str = "", organizer: str = "",
                 attendees: list[str] | None = None, source_link: str = "",
                 is_all_day: bool = False) -> dict:
    ref = _db().collection(_EVENTS).document(id)
    ref.set({"id": id, "calendar": calendar, "subject": subject, "start_at": start_at,
             "end_at": end_at or None, "location": location or None,
             "organizer": organizer or None, "attendees": attendees or [],
             "source_link": source_link or None, "is_all_day": bool(is_all_day),
             "updated_at": now_iso()})
    return _event(ref.get().to_dict())


def _event(d: Optional[dict]) -> Optional[dict]:
    if d is None:
        return None
    d = dict(d)
    d["attendees"] = d.get("attendees") or []
    d["is_all_day"] = bool(d.get("is_all_day"))
    return d


def list_events_between(start_iso: str, end_iso: str) -> list[dict]:
    evs = [_event(s.to_dict()) for s in _db().collection(_EVENTS).stream()]
    evs = [e for e in evs if start_iso <= e["start_at"] < end_iso]
    evs.sort(key=lambda e: e["start_at"])
    return evs


def list_events_overlapping(start_iso: str, end_iso: str) -> list[dict]:
    evs = [_event(s.to_dict()) for s in _db().collection(_EVENTS).stream()]
    evs = [e for e in evs if e["start_at"] < end_iso and (e.get("end_at") or e["start_at"]) > start_iso]
    evs.sort(key=lambda e: (not e["is_all_day"], e["start_at"]))
    return evs


def delete_events_before(iso: str) -> int:
    n = 0
    for s in _db().collection(_EVENTS).stream():
        if (s.to_dict().get("start_at") or "") < iso:
            s.reference.delete()
            n += 1
    return n


# ── Sender rules (FILTER-1 / PRIORITY-1) ────────────────────────────────────

def upsert_sender_rule(*, email: str, action: str, category: str = "",
                       direction: str = "", importance: int = 0,
                       subject_pattern: str = "", notes: str = "") -> dict:
    email = email.strip().lower()
    _db().collection(_SENDER_RULES).document(email).set({
        "email": email, "action": action, "category": category or None,
        "direction": direction or None, "importance": importance or None,
        "subject_pattern": subject_pattern or None, "notes": notes or None,
        "created_at": now_iso(),
    }, merge=True)
    return _db().collection(_SENDER_RULES).document(email).get().to_dict()


def list_sender_rules() -> list[dict]:
    rules = [s.to_dict() for s in _db().collection(_SENDER_RULES).stream()]
    rules.sort(key=lambda r: r.get("email") or "")
    return rules


def delete_sender_rule(email: str) -> bool:
    email = email.strip().lower()
    ref = _db().collection(_SENDER_RULES).document(email)
    if ref.get().exists:
        ref.delete()
        return True
    return False


def get_sender_rule_for_email(email: str) -> Optional[dict]:
    """Exact match first, then longest-matching @domain.com suffix."""
    email = email.strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    snap = _db().collection(_SENDER_RULES).document(email).get()
    if snap.exists:
        return snap.to_dict()
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        pattern = "@" + ".".join(parts[i:])
        snap = _db().collection(_SENDER_RULES).document(pattern).get()
        if snap.exists:
            return snap.to_dict()
    return None


# ── Guidance (GUIDANCE-1) ────────────────────────────────────────────────────

def upsert_guidance(*, key: str, body: str, scope: str = "all",
                    active: bool = True) -> dict:
    key = key.strip().lower()
    _db().collection(_GUIDANCE).document(key).set({
        "key": key, "body": body.strip(), "scope": scope.strip() or "all",
        "active": bool(active), "created_at": now_iso(),
    }, merge=True)
    return _db().collection(_GUIDANCE).document(key).get().to_dict()


def list_guidance(*, active_only: bool = False) -> list[dict]:
    items = [s.to_dict() for s in _db().collection(_GUIDANCE).stream()]
    if active_only:
        items = [g for g in items if g.get("active")]
    items.sort(key=lambda g: g.get("key") or "")
    return items


def delete_guidance(key: str) -> bool:
    key = key.strip().lower()
    ref = _db().collection(_GUIDANCE).document(key)
    if ref.get().exists:
        ref.delete()
        return True
    return False
