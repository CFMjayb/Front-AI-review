"""M5 — Teams ingestion (agent-driven).

Outlook email is routed into Front (connect the M365 mailbox as a Front inbox), so
it flows through the existing pipeline + cos/front_extract.py with no code here.
Teams chat has no clean Front equivalent, so an agent run fetches it via the
chat_message_search MCP tool, hands the results here as plain dicts, and this
module:

  1. normalizes them into the shared thread shape (cos/extract.py),
  2. gates on the ledger's `seen` table so each thread state is analyzed once,
  3. runs the write-free analyzer, and
  4. upserts loops with channel 'teams'.

ingest() is channel-agnostic, so the same path serves any future agent-fetched
channel. See docs/chief-of-staff/INGESTION.md for the playbook. Pure functions —
fully testable with fake payloads and a fake Claude client.
"""
import logging
import os
import time

from cos import extract, ledger
from modules import analyze

logger = logging.getLogger(__name__)


def _owner_emails() -> set[str]:
    return extract.owner_emails()


def _normalize_ms_messages(messages: list[dict], owners: set[str]) -> list[dict]:
    norm: list[dict] = []
    for m in messages or []:
        sender_email = (m.get("sender_email") or "").lower()
        # Direction: a message from anyone other than Jay is inbound.
        inbound = sender_email not in owners if sender_email else not m.get("from_me")
        norm.append(extract.make_message(
            inbound=inbound, ts_epoch=m.get("ts_epoch") or 0,
            sender_name=m.get("sender_name", ""), sender_email=sender_email,
            recipients=[r.lower() for r in (m.get("recipients") or [])],
            text=m.get("text", ""),
        ))
    return norm


def thread_from_teams(*, chat_id: str, messages: list[dict], subject: str = "",
                      web_link: str = "", owner_emails: set[str] | None = None) -> dict:
    owners = owner_emails or _owner_emails()
    return extract.build_thread(
        channel="teams", source_ref=chat_id, subject=subject or "Teams chat",
        source_link=web_link, messages=_normalize_ms_messages(messages, owners))


def _transcript(thread: dict) -> str:
    parts = []
    for m in sorted(thread["messages"], key=lambda x: x.get("ts_epoch") or 0):
        who = m.get("sender_name") or m.get("sender_email") or "unknown"
        date = (time.strftime("%Y-%m-%d %H:%M", time.gmtime(m["ts_epoch"]))
                if m.get("ts_epoch") else "unknown")
        direction = "inbound" if m.get("inbound") else "outbound"
        parts.append(f"From: {who}\nDate: {date}\nDirection: {direction}\n\n"
                     f"{m.get('text', '')}\n---")
    return "\n".join(parts)


def ingest(threads: list[dict], claude, *, dry_run: bool = False) -> dict:
    """Analyze (once per state) and upsert loops for normalized MS threads."""
    created = skipped = analyzed = errored = 0
    cost = 0.0

    for thread in threads:
        channel, ref = thread["channel"], thread["source_ref"]
        last = extract.last_message(thread)
        if last is None:
            continue
        marker = extract.iso(last.get("ts_epoch"))

        if ledger.was_seen(channel, ref, marker):
            skipped += 1
            continue

        first = min(thread["messages"], key=lambda x: x.get("ts_epoch") or 0)
        sender = first.get("sender_name") or first.get("sender_email") or "unknown"
        try:
            result = analyze.analyze_transcript(
                claude, subject=thread.get("subject", ""), sender=sender,
                transcript=_transcript(thread))
        except Exception as exc:
            logger.warning(f"MS analyze failed for {channel}:{ref}: {exc}")
            errored += 1
            continue

        analyzed += 1
        cost += result.get("cost_usd", 0)
        if result["ok"]:
            loop = extract.loop_from_thread(thread, result["output"], dry_run=dry_run)
            if loop and not loop.get("dry_run"):
                created += 1
        if not dry_run:
            ledger.mark_seen(channel, ref, marker)

    logger.info(f"MS ingest: {analyzed} analyzed / {created} loops / {skipped} skipped "
                f"/ {errored} errored / ${cost:.4f}")
    return {"analyzed": analyzed, "created": created, "skipped": skipped,
            "errored": errored, "cost_usd": cost}
