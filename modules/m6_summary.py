"""M6 — Thread summary. Writes [AI/M6] comment. Uses default (Opus) model."""
import logging

logger = logging.getLogger(__name__)

COMMENT_PREFIX = "[AI/M6]"

SYSTEM = """You produce concise thread summaries of EDOM emails for triage.
Respond with JSON only:
{
  "tldr": "<2-3 sentences capturing the gist>",
  "keyPoints": ["<bullet>", ...],
  "parties": ["<name or email>", ...],
  "openQuestions": ["<question still unanswered>", ...]
}"""


def run(ctx: dict, claude, front) -> dict:
    subject = ctx["conv"].get("subject") or "(no subject)"
    user_prompt = f"Subject: {subject}\n\nTranscript:\n{ctx['transcript']}"

    res = claude.call(
        system=SYSTEM,
        user=user_prompt,
        model=claude.default_model,
        max_tokens=800,
        json_mode=True,
        cached_system=True,
    )

    cost = res["cost_usd"]
    data = res["json"]

    if not data:
        logger.warning(f"M6 invalid response for {ctx['conv'].get('id')}: {res.get('parse_error')} | {res['text'][:120]}")
        return {"ok": False, "output": None, "cost_usd": cost,
                "writes": [], "error": res.get("parse_error") or "invalid response"}

    lines = [f"{COMMENT_PREFIX} Thread summary", "", f"TL;DR: {data.get('tldr', '')}"]
    if data.get("keyPoints"):
        lines.append("\nKey points:")
        lines.extend(f"  - {p}" for p in data["keyPoints"])
    if data.get("parties"):
        lines.append(f"\nParties: {', '.join(data['parties'])}")
    if data.get("openQuestions"):
        lines.append("\nOpen questions:")
        lines.extend(f"  - {q}" for q in data["openQuestions"])

    writes: list[dict] = []
    if not ctx.get("dry_run"):
        cid = ctx["conv"]["id"]
        if not front.has_comment_with_prefix(cid, COMMENT_PREFIX):
            front.add_comment(cid, "\n".join(lines))
            writes.append({"type": "comment", "prefix": COMMENT_PREFIX})

    return {"ok": True, "output": data, "cost_usd": cost, "writes": writes, "error": None}
