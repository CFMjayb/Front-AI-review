"""Plaud.ai meeting-notes action extractor.

When the pipeline detects an email from plaud.ai (a meeting-notes AI), it routes
here instead of the standard analyze → loop flow. We use Claude to pull discrete
action items from the meeting summary and create one CoS loop per item. The email
thread itself does NOT become a loop.

Detection: sender domain ends with plaud.ai (covers noreply@plaud.ai, etc.).
"""
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_PLAUD_DOMAIN = "plaud.ai"

_EXTRACT_PROMPT = """\
You are reading a meeting summary email sent by Plaud.ai. Your job is to extract
every discrete action item from the notes.

Rules:
- Each action item must be a single, concrete task someone needs to do.
- If no assignee is mentioned, assume the assignee is "Jay".
- Keep the action text short (under 120 characters) — it will be a CoS loop summary.
- Include a one-sentence context so the action makes sense without re-reading the notes.
- Ignore informational statements that require no action.
- Return a JSON array. If there are no action items, return an empty array [].

Format each item as:
{"action": "...", "assignee": "...", "context": "..."}

Meeting notes follow:
---
{body}
---

Return only the JSON array, no other text."""


def is_plaud_email(conv: dict, messages: list[dict]) -> bool:
    """True when the conversation originates from a Plaud.ai meeting summary."""
    for m in messages or []:
        author = m.get("author") or {}
        email = (author.get("email") or author.get("handle") or "").lower()
        if email.endswith("@" + _PLAUD_DOMAIN) or email.endswith("." + _PLAUD_DOMAIN):
            return True
        # Also check recipients[role='from'] for inbound messages
        for r in (m.get("recipients") or []):
            if r.get("role") == "from":
                handle = (r.get("handle") or "").lower()
                if handle.endswith("@" + _PLAUD_DOMAIN):
                    return True
    return False


def _get_body(messages: list[dict], front_client) -> str:
    """Pull plain-text body from the latest inbound message."""
    inbound = [m for m in messages if m.get("is_inbound")]
    if not inbound:
        return ""
    latest = max(inbound, key=lambda m: m.get("created_at") or 0)
    return front_client.extract_plain_text_body(latest) or ""


def extract_action_items(conv: dict, messages: list[dict],
                         claude, front_client) -> list[dict]:
    """Return a list of action-item dicts extracted from the meeting notes."""
    body = _get_body(messages, front_client)
    if not body.strip():
        logger.warning(f"Plaud extract: no body in {conv.get('id')}")
        return []

    prompt = _EXTRACT_PROMPT.format(body=body[:8000])  # cap at 8k chars

    try:
        import json
        response = claude.complete(prompt, max_tokens=1024)
        text = (response or "").strip()
        # Strip markdown fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        items = json.loads(text)
        if not isinstance(items, list):
            raise ValueError("response is not a list")
        logger.info(f"Plaud extract: {len(items)} action item(s) from {conv.get('id')}")
        return items
    except Exception as exc:
        logger.error(f"Plaud extract: parse failed for {conv.get('id')}: {exc}")
        return []


def create_loops(conv: dict, messages: list[dict], action_items: list[dict],
                 ledger, front_source_link, *, dry_run: bool = False) -> list[dict]:
    """Upsert one CoS loop per action item. Returns the list of created loops."""
    import time
    conv_id = conv.get("id", "")
    source_link = front_source_link(conv_id)
    # Use the conversation creation date as the source_date
    source_date_epoch = conv.get("created_at") or 0
    source_date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(source_date_epoch)) if source_date_epoch else ""
    subject = conv.get("subject") or "Meeting notes"

    loops = []
    for i, item in enumerate(action_items):
        action = (item.get("action") or "").strip()
        assignee = (item.get("assignee") or "Jay").strip()
        context = (item.get("context") or "").strip()
        if not action:
            continue

        summary = action[:160]
        notes = f"From: {subject}\nAssignee: {assignee}\nContext: {context}" if context else \
                f"From: {subject}\nAssignee: {assignee}"

        # Use a sub-ref so each action item gets its own stable loop ID
        source_ref = f"{conv_id}::action::{i}"

        if dry_run:
            logger.info(f"[dry-run] would create loop: {summary}")
            loops.append({"dry_run": True, "summary": summary, "source_ref": source_ref})
            continue

        loop = ledger.upsert_loop(
            direction="i_owe",
            counterparty=assignee if assignee.lower() != "jay" else "self",
            summary=summary,
            channel="front",
            source_ref=source_ref,
            source_link=source_link,
            category="meeting-action",
            importance=3,
            source_date=source_date,
        )
        if loop:
            # Store the context note
            existing_notes = (loop.get("notes") or "").strip()
            if not existing_notes:
                ledger.patch_loop(loop["id"], notes=notes)
            loops.append(loop)

    return loops
