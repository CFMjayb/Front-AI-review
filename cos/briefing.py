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

from cos import ledger

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


def gather() -> dict:
    """Pull the day's sections from the ledger (resolved loops already excluded)."""
    on_you = [l for l in ledger.list_loops(direction="i_owe") if not _is_active_snooze(l)]
    waiting = [l for l in ledger.list_loops(direction="owed_to_me") if not _is_active_snooze(l)]
    overdue = [l for l in ledger.list_loops(overdue_only=True) if not _is_active_snooze(l)]
    new_recent = [l for l in (on_you + waiting) if _hours_ago(l.get("first_seen")) <= 24]
    return {"on_you": on_you, "waiting": waiting, "overdue": overdue,
            "new": new_recent, "stats": ledger.stats()}


def _loop_line(loop: dict) -> str:
    bits = [f"**{loop['counterparty']}** — {loop['summary']}"]
    if loop.get("due_at"):
        bits.append(f" _(due {loop['due_at'][:10]})_")
    if loop.get("source_link"):
        bits.append(f" → [open]({loop['source_link']})")
    return "".join(bits)


def render(sections: dict, *, date: str = "", headline: str = "", closing: str = "",
           filtered_count: int | None = None) -> tuple[str, str]:
    """Return (subject, markdown_body)."""
    date = date or datetime.date.today().isoformat()
    on_you, waiting = sections["on_you"], sections["waiting"]
    subject = (f"☀️ Your day — {date} · {len(on_you)} on you · "
               f"{len(waiting)} waiting")

    lines = [f"# Your day — {date}", ""]
    if headline:
        lines += [headline, ""]

    lines.append(f"## 🔴 On you — you owe a reply ({len(on_you)})")
    lines += [f"{i}. {_loop_line(l)}" for i, l in enumerate(on_you, 1)] or ["_Nothing on you. Enjoy it._"]
    lines.append("")

    lines.append(f"## ⏳ Waiting on others — quiet 36 h+ ({len(waiting)})")
    lines += [f"- {_loop_line(l)}" for l in waiting] or ["_Nothing outstanding._"]
    lines.append("")

    if sections["new"]:
        lines.append(f"## 🆕 New since yesterday ({len(sections['new'])})")
        lines += [f"- {_loop_line(l)}" for l in sections["new"]]
        lines.append("")

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


def _deliver(subject: str, body: str, filepath: Path) -> str:
    """Pluggable delivery. Default 'file' just persists. 'email' transports are wired
    once a sending channel is chosen (Front / Outlook / SMTP)."""
    mode = os.environ.get("BRIEFING_DELIVERY", "file").lower()
    if mode == "file":
        return "file"
    logger.warning("BRIEFING_DELIVERY=%s requested but no email transport is "
                   "configured yet — brief saved to %s only.", mode, filepath)
    return "file"


def run_briefing(*, claude=None, filtered_count: int | None = None,
                 deliver: bool = True) -> dict:
    sections = gather()
    headline, closing = _narrate(sections, claude)
    date = datetime.date.today().isoformat()
    subject, body = render(sections, date=date, headline=headline, closing=closing,
                           filtered_count=filtered_count)

    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    filepath = BRIEF_DIR / f"{date}.md"
    filepath.write_text(f"**Subject:** {subject}\n\n{body}\n", encoding="utf-8")

    transport = _deliver(subject, body, filepath) if deliver else "skipped"
    logger.info(f"Briefing written to {filepath} (delivery: {transport})")
    return {"file": str(filepath), "subject": subject, "transport": transport,
            "counts": {k: len(v) for k, v in sections.items() if isinstance(v, list)}}
