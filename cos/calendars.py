"""Calendar integration — events cache, conflicts, meeting-prep loops.

Calendars reach this environment as the agent-bound outlook_calendar_search MCP
tool, so an agent sweep pulls your own + named shared calendars and feeds the raw
events to ingest_events(). The autonomous 6 AM briefing then reads the cached
events (no MCP needed) and uses them three ways:

  1. the 📅 Today section of the briefing,
  2. meeting-prep loops (channel 'calendar') for meetings you owe prep on / run,
  3. conflict detection (overlapping meetings).

Configure which calendars via COS_CALENDARS (comma-separated owner emails / names);
your own calendar ('self') is always included. See docs/chief-of-staff/INGESTION.md.
"""
import datetime
import hashlib
import logging
import os
import re
import time

from cos import extract, ledger

logger = logging.getLogger(__name__)


def configured_calendars() -> list[str]:
    extra = [c.strip() for c in os.environ.get("COS_CALENDARS", "").split(",") if c.strip()]
    return ["self"] + extra


# ── Normalization (Microsoft Graph / MCP event shapes) ───────────────────────

def _email(obj) -> str:
    if not obj:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        ea = obj.get("emailAddress") or obj
        return (ea.get("address") or ea.get("email") or obj.get("address") or "").strip()
    return ""


def _emails(items) -> list[str]:
    return [e for e in (_email(x) for x in (items or [])) if e]


def _parse_dt(val) -> str:
    if isinstance(val, dict):
        val = val.get("dateTime") or val.get("date") or ""
    return _norm_iso(val or "")


def _norm_iso(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if len(s) == 10:  # date only → all-day start
        return s + "T00:00:00Z"
    if s.endswith("Z") or re.search(r"[+-]\d\d:?\d\d$", s):
        return s
    return s + "Z"


def _to_epoch(iso: str) -> float:
    if not iso:
        return 0.0
    s = re.sub(r"[+-]\d\d:?\d\d$", "", iso.replace("Z", ""))
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


def event_from_outlook(raw: dict, calendar_label: str = "self") -> dict:
    subject = raw.get("subject") or raw.get("title") or "(no title)"
    start = _parse_dt(raw.get("start"))
    ev_id = raw.get("id") or raw.get("iCalUId") or (
        "ev-" + hashlib.sha1(f"{calendar_label}|{subject}|{start}".encode()).hexdigest()[:12])
    location = raw.get("location")
    if isinstance(location, dict):
        location = location.get("displayName") or ""
    return {
        "id": ev_id, "calendar": calendar_label, "subject": subject,
        "start_at": start, "end_at": _parse_dt(raw.get("end")),
        "location": location or "", "organizer": _email(raw.get("organizer")),
        "attendees": _emails(raw.get("attendees")),
        "source_link": raw.get("webLink") or raw.get("source_link") or "",
        "is_all_day": bool(raw.get("isAllDay") or raw.get("is_all_day")),
    }


def ingest_events(raw_events: list[dict], calendar_label: str = "self") -> int:
    n = 0
    for raw in raw_events or []:
        ledger.upsert_event(**event_from_outlook(raw, calendar_label))
        n += 1
    logger.info(f"Calendar ingest: {n} events from {calendar_label}")
    return n


# ── Reads / derived views ────────────────────────────────────────────────────

def events_for_day(day: datetime.date | None = None) -> list[dict]:
    day = day or datetime.date.today()
    start = f"{day.isoformat()}T00:00:00Z"
    end = f"{(day + datetime.timedelta(days=1)).isoformat()}T00:00:00Z"
    return ledger.list_events_between(start, end)


def detect_conflicts(events: list[dict]) -> set[str]:
    """Return the set of event ids that overlap another timed event."""
    timed = sorted((e for e in events if not e["is_all_day"] and e.get("end_at")),
                   key=lambda e: e["start_at"])
    conflicting: set[str] = set()
    for i in range(len(timed)):
        for j in range(i + 1, len(timed)):
            if timed[j]["start_at"] < timed[i]["end_at"]:
                conflicting.add(timed[i]["id"])
                conflicting.add(timed[j]["id"])
    return conflicting


def attach_loops(events: list[dict]) -> dict[str, list[dict]]:
    """Map event id → open i_owe loops whose counterparty is an attendee."""
    by_email: dict[str, list[dict]] = {}
    for loop in ledger.list_loops(direction="i_owe"):
        email = (loop.get("counterparty_email") or "").lower()
        if email and loop["channel"] != "calendar":
            by_email.setdefault(email, []).append(loop)
    out: dict[str, list[dict]] = {}
    for ev in events:
        related = [l for a in ev["attendees"] for l in by_email.get((a or "").lower(), [])]
        if related:
            out[ev["id"]] = related
    return out


# ── Meeting-prep loops ───────────────────────────────────────────────────────

def sync_prep_loops(events: list[dict], *, owner_emails: set[str] | None = None,
                    dry_run: bool = False) -> list[dict]:
    """Create a prep loop for upcoming meetings you owe an attendee something, or
    that you organize. Idempotent on event id; expires after the meeting."""
    owners = owner_emails if owner_emails is not None else extract.owner_emails()
    now = time.time()
    by_email: dict[str, list[dict]] = {}
    for loop in ledger.list_loops(direction="i_owe"):
        email = (loop.get("counterparty_email") or "").lower()
        if email and loop["channel"] != "calendar":
            by_email.setdefault(email, []).append(loop)

    created: list[dict] = []
    for ev in events:
        if ev["is_all_day"]:
            continue
        start_e = _to_epoch(ev["start_at"])
        if start_e and start_e < now:
            continue
        attendees = [(a or "").lower() for a in ev["attendees"]]
        owe = [l for a in attendees for l in by_email.get(a, [])]
        organizer_is_me = (ev.get("organizer") or "").lower() in owners and len(attendees) >= 2
        if not (owe or organizer_is_me):
            continue

        summary = f"Prep for: {ev['subject']}"
        if owe:
            who = ", ".join(sorted({l["counterparty"] for l in owe}))
            summary += f" — you owe {who}"
        importance = 4 if start_e and (start_e - now) <= 3 * 3600 else 3

        if dry_run:
            created.append({"dry_run": True, "summary": summary, "source_ref": ev["id"]})
            continue
        created.append(ledger.upsert_loop(
            direction="i_owe", counterparty=ev.get("organizer") or "meeting",
            counterparty_email=ev.get("organizer") or "", summary=summary,
            channel="calendar", source_ref=ev["id"], source_link=ev.get("source_link") or "",
            due_at=ev["start_at"], importance=importance, status="open"))
    return created


def expire_past_calendar_loops(now_epoch: float | None = None) -> int:
    """Resolve calendar prep loops once their meeting has started/passed."""
    now = now_epoch or time.time()
    n = 0
    for loop in ledger.list_loops(channel="calendar"):
        if loop["status"] in ("done", "dropped"):
            continue
        due = _to_epoch(loop.get("due_at") or "")
        if due and due < now:
            ledger.resolve_loop(loop["id"], "done")
            n += 1
    return n
