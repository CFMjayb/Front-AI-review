"""M7 — Routing suggestion. Writes [AI/M7] comment only — never auto-assigns."""
import logging

logger = logging.getLogger(__name__)

COMMENT_PREFIX = "[AI/M7]"
ROUTES = ["bishop", "canon-to-ordinary", "finance-office", "communications",
          "keep-with-jay", "vendor-coordinator"]

SYSTEM = """You suggest routing for EDOM emails based on content.
Possible routes:
- bishop: matters requiring the Bishop's direct attention
- canon-to-ordinary: clergy personnel, deployment, formation
- finance-office: invoices, payroll, financial questions
- communications: press, public-facing comms, social media
- vendor-coordinator: vendor management, contracts
- keep-with-jay: anything Jay should handle himself

Respond with JSON only:
{
  "suggestedAssignee": "<one of the routes above>",
  "confidence": 0.0-1.0,
  "reasoning": "<one short sentence>"
}

NEVER auto-assign. This is a suggestion only — Jay reviews it."""


def run(ctx: dict, claude, front) -> dict:
    m1_out = (ctx.get("m1") or {}).get("output") or {}
    m6_out = (ctx.get("m6") or {}).get("output") or {}
    subject = ctx["conv"].get("subject") or "(no subject)"

    user_prompt = (
        f"Classification: {m1_out.get('category', 'unknown')}\n"
        f"Summary: {m6_out.get('tldr', '(none)')}\n"
        f"Subject: {subject}"
    )

    res = claude.call(
        system=SYSTEM,
        user=user_prompt,
        model=claude.fast_model,
        max_tokens=200,
        json_mode=True,
        cached_system=True,
    )

    cost = res["cost_usd"]
    data = res["json"]

    if not data or data.get("suggestedAssignee") not in ROUTES:
        logger.warning(f"M7 invalid response for {ctx['conv'].get('id')}: {res.get('parse_error')} | {res['text'][:120]}")
        return {"ok": False, "output": None, "cost_usd": cost,
                "writes": [], "error": res.get("parse_error") or "invalid route"}

    writes: list[dict] = []
    if not ctx.get("dry_run"):
        cid = ctx["conv"]["id"]
        if not front.has_comment_with_prefix(cid, COMMENT_PREFIX):
            lines = [
                f"{COMMENT_PREFIX} Routing suggestion",
                "",
                f"Suggested: {data['suggestedAssignee']}",
                f"Confidence: {float(data.get('confidence', 0)):.2f}",
            ]
            if data.get("reasoning"):
                lines.append(f"Reasoning: {data['reasoning']}")
            lines += ["", "(Suggestion only — not auto-assigned.)"]
            front.add_comment(cid, "\n".join(lines))
            writes.append({"type": "comment", "prefix": COMMENT_PREFIX})

    return {"ok": True, "output": data, "cost_usd": cost, "writes": writes, "error": None}
