"""M8 — Draft reply. Fires only when M3.requiresReply and urgency in {urgent, high}.
Draft saved for Jay's review — never auto-sent."""
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

VOICE_NOTES = """Jay's reply style:
- Warm and direct.
- Brief paragraphs (2-4 sentences).
- Signs off "—Jay" for internal, "Best, Jay Bentzen" for external.
- Asks one specific clarifying question rather than several.
- Confirms next concrete step (date, action)."""

SYSTEM = f"""You draft email replies on behalf of Jay Bentzen at the Episcopal Diocese of Maryland.
{VOICE_NOTES}

Respond with JSON only:
{{
  "to": ["<email>", ...],
  "subject": "Re: <original subject>",
  "body": "<full plain-text reply>",
  "rationale": "<one sentence on why this draft is appropriate>"
}}

Drafts are SAVED for Jay's review — never auto-sent."""


def _pick_recipient(conv: dict, messages: list) -> list[str]:
    for m in reversed(messages or []):
        if m.get("is_inbound"):
            author = m.get("author") or {}
            email = author.get("email") or author.get("handle")
            if email:
                return [email]
    return []


def _pick_channel_id(messages: list) -> Optional[str]:
    for m in (messages or []):
        url = ((m.get("_links") or {}).get("related") or {}).get("channel", "")
        match = re.search(r"/channels/([^/?#]+)", url)
        if match:
            return match.group(1)
    return None


def run(ctx: dict, claude, front) -> dict:
    analyze_out = (ctx.get("analyze") or {}).get("output") or {}
    subject = ctx["conv"].get("subject") or "(no subject)"

    user_prompt = (
        f"Subject: {subject}\n\n"
        f"Thread summary:\n{analyze_out.get('tldr', '')}\n\n"
        f"Action context: {analyze_out.get('action_summary', '')}\n\n"
        f"Full transcript:\n{ctx['transcript']}"
    )

    res = claude.call(
        system=SYSTEM,
        user=user_prompt,
        model=claude.default_model,
        max_tokens=1200,
        json_mode=True,
        cached_system=True,
    )

    cost = res["cost_usd"]
    data = res["json"]

    if not data or not data.get("body"):
        logger.warning(f"M8 invalid response for {ctx['conv'].get('id')}: {res.get('parse_error')} | {res['text'][:120]}")
        return {"ok": False, "output": None, "cost_usd": cost,
                "writes": [], "error": res.get("parse_error") or "invalid draft"}

    writes: list[dict] = []
    if not ctx.get("dry_run"):
        cid = ctx["conv"]["id"]
        messages = ctx.get("messages") or []
        to = data.get("to") or _pick_recipient(ctx["conv"], messages)
        if not to:
            logger.warning(f"M8: could not determine recipient for {cid}; skipping draft")
            return {"ok": False, "output": data, "cost_usd": cost,
                    "writes": [], "error": "no recipient"}

        channel_id = _pick_channel_id(messages)
        body_with_label = f"[AI/M8 draft]\n\n{data['body']}"
        try:
            front.create_draft(
                cid,
                channel_id=channel_id,
                subject=data.get("subject", f"Re: {subject}"),
                body=body_with_label,
                to=to,
            )
            writes.append({"type": "draft", "to": to})
        except Exception as exc:
            logger.warning(f"M8 draft creation failed for {cid}: {exc}")
            return {"ok": False, "output": data, "cost_usd": cost,
                    "writes": [], "error": str(exc)}

    return {"ok": True, "output": data, "cost_usd": cost, "writes": writes, "error": None}
