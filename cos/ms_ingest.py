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
import datetime
import html as _html
import logging
import os
import re
import time
from collections import defaultdict

from cos import extract, ledger
from modules import analyze

logger = logging.getLogger(__name__)


def _owner_emails() -> set[str]:
    return extract.owner_emails()


def _owner_names() -> set[str]:
    """Display names that are "Jay" — Teams identifies senders by name, not email."""
    raw = os.environ.get("COS_OWNER_NAMES", "")
    return {v.strip().lower() for v in raw.split(",") if v.strip()}


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _parse_iso(s: str) -> float:
    if not s:
        return 0.0
    s = re.sub(r"\.\d+", "", s).replace("Z", "")
    s = re.sub(r"[+-]\d\d:?\d\d$", "", s)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


def threads_from_teams_search(items: list[dict], *, owner_names: set[str] | None = None,
                              owner_emails: set[str] | None = None) -> list[dict]:
    """Build normalized threads directly from raw chat_message_search output.

    Real shape: each item has chatId, from.displayName (email often null),
    createdDateTime (ISO), and summary (HTML). Groups by chat; direction is set by
    matching the sender's name/email against the owner identity.
    """
    owners_e = owner_emails if owner_emails is not None else _owner_emails()
    owners_n = owner_names if owner_names is not None else _owner_names()

    chats: dict[str, list[dict]] = defaultdict(list)
    links: dict[str, str] = {}
    for it in items:
        cid = it.get("chatId")
        if cid:
            chats[cid].append(it)
            links.setdefault(cid, it.get("chatUri") or it.get("webUrl") or "")

    threads: list[dict] = []
    for cid, msgs in chats.items():
        parsed = [{
            "name": ((m.get("from") or {}).get("displayName") or "").strip(),
            "email": ((m.get("from") or {}).get("email") or "").lower(),
            "ts": _parse_iso(m.get("createdDateTime")),
            "text": _strip_html(m.get("summary") or m.get("body")),
        } for m in msgs]
        participants = {p["name"] for p in parsed if p["name"]}

        norm = []
        for p in parsed:
            is_owner = (p["email"] and p["email"] in owners_e) or (p["name"].lower() in owners_n)
            norm.append(extract.make_message(
                inbound=not is_owner, ts_epoch=p["ts"], sender_name=p["name"],
                sender_email=p["email"], recipients=sorted(participants - {p["name"]}),
                text=p["text"]))
        threads.append(extract.build_thread(
            channel="teams", source_ref=cid, subject="Teams chat",
            source_link=links.get(cid, ""), messages=norm))
    return threads


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
