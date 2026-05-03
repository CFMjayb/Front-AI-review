"""M3 — Action item extraction. Writes [AI/M3] comment."""
import logging

logger = logging.getLogger(__name__)

COMMENT_PREFIX = "[AI/M3]"

SYSTEM = """You analyze EDOM email threads and identify what action is required.
Respond with JSON only:
{
  "requiresReply": <bool>,
  "requiresApproval": <bool>,
  "requiresPayment": <bool>,
  "deadline": "<ISO 8601 date or null>",
  "actionItems": ["<short imperative phrase>", ...],
  "summary": "<one sentence describing the action needed, or 'FYI only' if none>"
}"""


def run(ctx: dict, claude, front) -> dict:
    subject = ctx["conv"].get("subject") or "(no subject)"
    user_prompt = f"Subject: {subject}\n\nTranscript:\n{ctx['transcript']}"

    res = claude.call(
        system=SYSTEM,
        user=user_prompt,
        model=claude.fast_model,
        max_tokens=500,
        json_mode=True,
        cached_system=True,
    )

    cost = res["cost_usd"]
    data = res["json"]

    if not data:
        logger.warning(f"M3 invalid response for {ctx['conv'].get('id')}: {res.get('parse_error')} | {res['text'][:120]}")
        return {"ok": False, "output": None, "cost_usd": cost,
                "writes": [], "error": res.get("parse_error") or "invalid response"}

    lines = [
        f"{COMMENT_PREFIX} Action analysis",
        "",
        f"Summary: {data.get('summary', '')}",
        f"Requires reply: {data.get('requiresReply', False)}",
        f"Requires approval: {data.get('requiresApproval', False)}",
        f"Requires payment: {data.get('requiresPayment', False)}",
    ]
    if data.get("deadline"):
        lines.append(f"Deadline: {data['deadline']}")
    items = data.get("actionItems") or []
    if items:
        lines.append("Action items:")
        lines.extend(f"  - {a}" for a in items)

    writes: list[dict] = []
    if not ctx.get("dry_run"):
        cid = ctx["conv"]["id"]
        if not front.has_comment_with_prefix(cid, COMMENT_PREFIX):
            front.add_comment(cid, "\n".join(lines))
            writes.append({"type": "comment", "prefix": COMMENT_PREFIX})

    return {"ok": True, "output": data, "cost_usd": cost, "writes": writes, "error": None}
