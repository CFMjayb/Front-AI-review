"""M4 — daily Chief-of-Staff briefing, assembled from the open-loop ledger.

Builds the 6 AM email (see docs/chief-of-staff/examples/daily-brief-email.md) from
SQLite: what's on you, what you're waiting on, what's overdue, and what's new.

The section body is fully deterministic (testable without any network). An optional
Claude "narrator" adds the warm headline + closing line, mirroring digest.py.

Delivery transport (which address actually sends the email) is a separate, pluggable
step — see _deliver(). Until one is configured the brief is written to
data/briefings/<date>.md and logged.
"""
import datetime
import json
import logging
import os
import time
from pathlib import Path

from cos import ledger, mailboxes

logger = logging.getLogger(__name__)

BRIEF_DIR = Path(__file__).resolve().parent.parent / "data" / "briefings"

NARRATOR_SYSTEM = """You write the opening and closing of Jay Bentzen's daily
chief-of-staff briefing (Episcopal Diocese of Maryland). Tone: warm, direct, honest.
Given the day's loop counts and the single most pressing item, return JSON only:
{"headline": "<2-3 sentence synthesis of the day>",
 "closing": "<one sentence; name the thing to do first>"}"""


def _now_iso() -> str:
    return ledger.now_iso()


def _is_active_snooze(loop: dict) -> bool:
    su = loop.get("snooze_until")
    return loop.get("status") == "snoozed" and bool(su) and su > _now_iso()


def _hours_ago(iso_ts: str) -> float:
    if not iso_ts:
        return 0.0
    try:
        dt = datetime.datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600
    except ValueError:
        return 0.0


_BRIEF_URGENCY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def _brief_sort(loops: list[dict]) -> list[dict]:
    """Sort for briefing: urgency tier → oldest first within tier."""
    return sorted(loops, key=lambda l: (
        _BRIEF_URGENCY_ORDER.get((l.get("urgency") or "normal").lower(), 4),
        l.get("first_seen") or "",
    ))


def gather(*, mailbox: str = "", include_calendar: bool = True) -> dict:
    """Pull the day's sections from the ledger (resolved loops already excluded).

    mailbox="" is every loop, unsplit — the shape the subject line and the
    run_briefing counts are built from. Pass a mailbox key for one mailbox's
    block; the calendar is shared across mailboxes rather than repeated, so
    per-mailbox calls pass include_calendar=False.
    """
    from cos import calendars
    i_owe  = [l for l in ledger.list_loops(direction="i_owe", mailbox=mailbox)
              if not _is_active_snooze(l)]
    on_you = _brief_sort([l for l in i_owe if not l.get("fyi")])
    fyi    = [l for l in i_owe if l.get("fyi")]
    waiting = _brief_sort(
        [l for l in ledger.list_loops(direction="owed_to_me", mailbox=mailbox)
         if not _is_active_snooze(l)])
    overdue = [l for l in ledger.list_loops(overdue_only=True, mailbox=mailbox)
               if not _is_active_snooze(l) and not l.get("fyi")]
    new_recent = [l for l in (on_you + waiting)
                  if l["channel"] != "calendar" and _hours_ago(l.get("first_seen")) <= 24]
    out = {"on_you": on_you, "fyi": fyi, "waiting": waiting, "overdue": overdue,
           "new": new_recent, "events": [], "conflicts": [], "attached": {}}
    if include_calendar:
        events = calendars.events_for_day()
        out.update({"events": events,
                    "conflicts": sorted(calendars.detect_conflicts(events)),
                    "attached": calendars.attach_loops(events),
                    "stats": ledger.stats()})
    return out


def gather_by_mailbox() -> list[tuple[str, dict]]:
    """One (mailbox_key, sections) pair per mailbox that has anything to show.

    Registered mailboxes always appear, so the email keeps a stable shape day to
    day. The Unattributed bucket appears only when it is non-empty.
    """
    out: list[tuple[str, dict]] = []
    for mb in mailboxes.mailboxes(include_unassigned=True):
        sections = gather(mailbox=mb["key"], include_calendar=False)
        has_any = any(sections[k] for k in ("on_you", "waiting", "new", "fyi"))
        if mb["key"] == mailboxes.UNASSIGNED and not has_any:
            continue
        out.append((mb["key"], sections))
    return out


_URGENCY_EMOJI = {"urgent": "🔴", "high": "🟠", "normal": "🟡", "low": "⚪"}


def _loop_line(loop: dict) -> str:
    num     = loop.get("num")
    urgency = (loop.get("urgency") or "normal").lower()
    action  = loop.get("action_type") or ""
    tag     = f"**#{num}** · " if num else ""
    urg_em  = _URGENCY_EMOJI.get(urgency, "")
    act_tag = f"`{action}` " if action and action != "Review" else ""
    bits    = [f"{urg_em} {act_tag}{tag}**{loop['counterparty']}** — {loop['summary']}"]
    if loop.get("due_at"):
        bits.append(f" _(due {loop['due_at'][:10]})_")
    if loop.get("sentiment") in ("concerned", "frustrated", "angry"):
        bits.append(f" ⚠️ _{loop['sentiment']}_")
    if loop.get("source_link"):
        bits.append(f" → [open]({loop['source_link']})")
    return "".join(bits)


def _event_line(ev: dict, *, conflict: bool = False) -> str:
    from cos import calendars
    if ev.get("is_all_day"):
        when = "all day"
    else:
        start = calendars.local_hhmm(ev.get("start_at", ""))
        end = calendars.local_hhmm(ev.get("end_at") or "")
        when = f"{start}–{end}" if end else (start or "—")
    line = f"- **{when}** {ev.get('subject', '(no title)')}"
    if ev.get("location"):
        line += f" _({ev['location']})_"
    if conflict:
        line += " ⚠️ overlaps"
    return line


def render(sections: dict, *, date: str = "", headline: str = "", closing: str = "",
           filtered_count: int | None = None) -> tuple[str, str]:
    """Return (subject, markdown_body)."""
    date = date or datetime.date.today().isoformat()
    on_you, waiting = sections["on_you"], sections["waiting"]
    events = sections.get("events", [])
    subject = (f"☀️ Your day — {date} · {len(on_you)} on you · "
               f"{len(waiting)} waiting · {len(events)} meetings")

    lines = [f"# Your day — {date}", ""]
    if headline:
        lines += [headline, ""]

    lines.append(f"## 🔴 On you ({len(on_you)})")
    lines += [f"- {_loop_line(l)}" for l in on_you] or ["_Nothing on you. Enjoy it._"]
    lines.append("")

    lines.append(f"## ⏳ Waiting on others — quiet 36 h+ ({len(waiting)})")
    lines += [f"- {_loop_line(l)}" for l in waiting] or ["_Nothing outstanding._"]
    lines.append("")

    if events:
        conflicts = set(sections.get("conflicts", []))
        attached = sections.get("attached", {})
        lines.append(f"## 📅 Today ({len(events)})")
        for ev in events:
            lines.append(_event_line(ev, conflict=ev["id"] in conflicts))
            for loop in attached.get(ev["id"], []):
                lines.append(f"    - ↳ prep: you owe **{loop['counterparty']}** — {loop['summary']}")
        if conflicts:
            lines.append(f"- ⚠️ {len(conflicts)} meeting(s) overlap — check your schedule.")
        lines.append("")

    if sections["new"]:
        lines.append(f"## 🆕 New since yesterday ({len(sections['new'])})")
        lines += [f"- {_loop_line(l)}" for l in sections["new"]]
        lines.append("")

    fyi = sections.get("fyi", [])
    if fyi:
        lines.append(f"## 📋 FYI — auto-clears in 24h ({len(fyi)})")
        lines.append("_Notifications / cc's / newsletters. Act on any you care about; "
                     "the rest clear automatically._")
        lines += [f"- {_loop_line(l)}" for l in fyi]
        lines.append("")

    if filtered_count is not None:
        lines += [f"## 🗑️ Filtered", "",
                  f"**{filtered_count}** marketing / spam / unsolicited set aside.", ""]

    if closing:
        lines += ["---", "", closing, "", "— Your chief of staff"]
    else:
        lines += ["— Your chief of staff"]

    return subject, "\n".join(lines)


def _mailbox_block(key: str, sections: dict) -> list[str]:
    """One mailbox's section of the email. Headings are one level deeper than the
    mailbox heading itself so the mailbox stays the visual unit."""
    mb = mailboxes.by_key(key) or {}
    label = mb.get("label") or mailboxes.UNASSIGNED_LABEL
    address = mb.get("address") or ""
    on_you, waiting = sections["on_you"], sections["waiting"]
    fyi, new = sections.get("fyi", []), sections.get("new", [])

    head = f"## 📬 {label}"
    if address:
        head += f" — {address}"
    head += f" · {len(on_you)} on you · {len(waiting)} waiting"
    lines = [head, ""]

    lines.append(f"### 🔴 On you ({len(on_you)})")
    lines += [f"- {_loop_line(l)}" for l in on_you] or ["_Nothing on you here._"]
    lines.append("")

    lines.append(f"### ⏳ Waiting on others — quiet 36 h+ ({len(waiting)})")
    lines += [f"- {_loop_line(l)}" for l in waiting] or ["_Nothing outstanding._"]
    lines.append("")

    if new:
        lines.append(f"### 🆕 New since yesterday ({len(new)})")
        lines += [f"- {_loop_line(l)}" for l in new]
        lines.append("")

    if fyi:
        lines.append(f"### 📋 FYI — auto-clears in 24h ({len(fyi)})")
        lines += [f"- {_loop_line(l)}" for l in fyi]
        lines.append("")

    return lines


def render_all(per_mailbox: list[tuple[str, dict]], shared: dict, *,
               date: str = "", headline: str = "", closing: str = "",
               filtered_count: int | None = None) -> tuple[str, str]:
    """Render the morning email with one section per mailbox.

    per_mailbox comes from gather_by_mailbox(); shared carries the calendar and
    the all-mailbox totals used in the subject line.
    """
    date = date or datetime.date.today().isoformat()
    on_you_total = len(shared["on_you"])
    waiting_total = len(shared["waiting"])
    events = shared.get("events", [])
    subject = (f"☀️ Your day — {date} · {on_you_total} on you · "
               f"{waiting_total} waiting · {len(events)} meetings")

    lines = [f"# Your day — {date}", ""]
    if headline:
        lines += [headline, ""]

    # One-line index so the mailbox split is visible before scrolling.
    if len(per_mailbox) > 1:
        bits = [f"**{mailboxes.label_for(k)}** {len(sec['on_you'])}/{len(sec['waiting'])}"
                for k, sec in per_mailbox]
        lines += ["_On you / waiting by mailbox:_ " + "  ·  ".join(bits), ""]

    # Calendar is shared — the day has one schedule, not one per mailbox.
    if events:
        conflicts = set(shared.get("conflicts", []))
        attached = shared.get("attached", {})
        lines.append(f"## 📅 Today ({len(events)})")
        for ev in events:
            lines.append(_event_line(ev, conflict=ev["id"] in conflicts))
            for loop in attached.get(ev["id"], []):
                lines.append(f"    - ↳ prep: you owe **{loop['counterparty']}** — {loop['summary']}")
        if conflicts:
            lines.append(f"- ⚠️ {len(conflicts)} meeting(s) overlap — check your schedule.")
        lines.append("")

    for key, sections in per_mailbox:
        lines += _mailbox_block(key, sections)

    if filtered_count is not None:
        lines += [f"## 🗑️ Filtered", "",
                  f"**{filtered_count}** marketing / spam / unsolicited set aside.", ""]

    if closing:
        lines += ["---", "", closing, "", "— Your chief of staff"]
    else:
        lines += ["— Your chief of staff"]

    return subject, "\n".join(lines)


def _narrate(sections: dict, claude) -> tuple[str, str]:
    """Optional warm headline + closing via Claude. Falls back to templated text."""
    on_you, waiting = sections["on_you"], sections["waiting"]
    top = on_you[0]["summary"] if on_you else (waiting[0]["summary"] if waiting else "")
    if claude is None:
        n = len(on_you)
        headline = (f"{n} thing{'s' if n != 1 else ''} on you today"
                    + (f", most pressing: {top}." if top else "."))
        return headline, ("Start with the top item." if top else "Clear day — enjoy it.")
    try:
        payload = {"on_you": len(on_you), "waiting": len(waiting),
                   "overdue": len(sections["overdue"]), "most_pressing": top}
        res = claude.call(system=NARRATOR_SYSTEM, user=json.dumps(payload),
                          model=claude.default_model, max_tokens=300, json_mode=True,
                          cached_system=True)
        data = res.get("json") or {}
        return data.get("headline", ""), data.get("closing", "")
    except Exception as exc:
        logger.warning(f"briefing narrator failed, using template: {exc}")
        return _narrate(sections, None)


_XLSM_MIME = "application/vnd.ms-excel.sheet.macroEnabled.12"
_TEMPLATE_BUCKET = os.environ.get("COS_TRIAGE_BUCKET", "cfm-cos-triage-uploads")


def _build_triage_attachment() -> list[dict] | None:
    """Attach the same static, macro-enabled Triage workbooks staff use for
    manual review — one per registered mailbox — fetched from a fixed GCS
    location, not regenerated here.

    These are deliberately NOT rebuilt per-send: Cloud Run has no Excel to
    run the COM automation that builds a .xlsm's VBA project and buttons, so
    each workbook is built once locally (create_triage_workbook.py) and
    uploaded to GCS. A Workbook_Open event bakes in its own auto-refresh, so
    the same static file still shows that day's real data the instant it's
    opened — see modControls.AutoRefreshOnOpen. Rebuild and re-upload a
    mailbox's template if the mailbox registry changes (a new key, a
    relabeled one) or if the baked-in MCP API key rotates; nothing here
    detects either condition on its own.

    Failure here must never block the briefing email itself; it just goes
    out with fewer attachments, or none. One mailbox's template missing from
    the bucket does not cost the others theirs.
    """
    try:
        import base64
        from google.cloud import storage
        from cos import mailboxes
    except Exception as exc:
        logger.warning("Triage workbook attachment setup failed, sending "
                       "briefing without attachments: %s", exc)
        return None

    try:
        bucket = storage.Client().bucket(_TEMPLATE_BUCKET)
    except Exception as exc:
        logger.warning("Could not reach GCS bucket %s for triage workbook "
                       "templates: %s", _TEMPLATE_BUCKET, exc)
        return None

    attachments: list[dict] = []
    for mb in mailboxes.mailboxes(include_unassigned=False):
        name = f"CoS Triage Workbook - {mailboxes.slug(mb['key'])}.xlsm"
        blob_name = f"templates/{name}"
        try:
            data = bucket.blob(blob_name).download_as_bytes()
            attachments.append({
                "name": name,
                "content_type": _XLSM_MIME,
                "content_base64": base64.b64encode(data).decode("ascii"),
            })
        except Exception as exc:
            logger.warning("Could not attach %s workbook template (%s): %s",
                           mb["key"], blob_name, exc)
    return attachments or None


def _deliver(subject: str, body: str, filepath: Path) -> str:
    """Deliver via the reusable sender layer. Default 'file' only persists; set
    BRIEFING_DELIVERY=email to send (transport chosen by SENDER_TRANSPORT)."""
    mode = os.environ.get("BRIEFING_DELIVERY", "file").lower()
    if mode == "file":
        return "file"
    try:
        from cos import sender
        attachments = _build_triage_attachment()
        result = sender.send(subject=subject, body_md=body, attachments=attachments)
        return result.get("transport", "email")
    except Exception as exc:
        logger.error("Briefing delivery failed (%s) — brief saved to %s only.",
                     exc, filepath)
        return "file"


def run_briefing(*, claude=None, filtered_count: int | None = None,
                 deliver: bool = True) -> dict:
    # Refresh calendar-derived loops from the cached events before assembling.
    from cos import calendars, extract
    try:
        calendars.expire_past_calendar_loops()
        calendars.sync_prep_loops(calendars.events_for_day())
    except Exception as exc:
        logger.warning(f"calendar prep sync failed: {exc}")
    try:
        extract.expire_fyi_loops()  # drop FYI items not acted on within 24h
    except Exception as exc:
        logger.warning(f"FYI auto-expire failed: {exc}")

    # `sections` stays the all-mailbox view: it drives the narrator, the subject
    # line totals, and the returned counts. The per-mailbox split is layered on
    # top rather than replacing it.
    sections = gather()
    headline, closing = _narrate(sections, claude)
    date = calendars.local_today().isoformat()

    per_mailbox = gather_by_mailbox()
    if len(per_mailbox) > 1:
        subject, body = render_all(per_mailbox, sections, date=date, headline=headline,
                                   closing=closing, filtered_count=filtered_count)
    else:
        # One mailbox (or none registered) — a split adds nothing but a heading.
        subject, body = render(sections, date=date, headline=headline, closing=closing,
                               filtered_count=filtered_count)

    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    filepath = BRIEF_DIR / f"{date}.md"
    filepath.write_text(f"**Subject:** {subject}\n\n{body}\n", encoding="utf-8")

    transport = _deliver(subject, body, filepath) if deliver else "skipped"
    logger.info(f"Briefing written to {filepath} (delivery: {transport})")
    return {"file": str(filepath), "subject": subject, "transport": transport,
            "counts": {k: len(v) for k, v in sections.items() if isinstance(v, list)},
            "by_mailbox": {k: {"on_you": len(sec["on_you"]),
                               "waiting": len(sec["waiting"]),
                               "fyi": len(sec.get("fyi", []))}
                           for k, sec in per_mailbox}}


def main() -> None:
    """Entrypoint for the scheduled Cloud Run briefing job (`python -m cos.briefing`)."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    claude = None
    try:
        from auth import get_anthropic_api_key
        from claude_client import ClaudeClient
        claude = ClaudeClient(
            api_key=get_anthropic_api_key(),
            default_model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7"),
            fast_model=os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5"))
    except Exception as exc:  # narrator is optional; brief still renders without it
        logger.warning(f"briefing narrator unavailable: {exc}")
    result = run_briefing(claude=claude)
    logger.info(f"Briefing complete: delivery={result.get('transport')} "
                f"counts={result.get('counts')}")


if __name__ == "__main__":
    main()
