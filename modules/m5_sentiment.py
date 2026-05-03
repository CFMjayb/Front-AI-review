"""M5 — Sender sentiment. Tags sentiment/{value} if escalation risk ≥ 0.5. Writes [AI/M5] comment."""
import logging

logger = logging.getLogger(__name__)

COMMENT_PREFIX = "[AI/M5]"
VALUES = ["positive", "neutral", "concerned", "frustrated", "angry"]

SYSTEM = """You assess sender sentiment in EDOM email threads.
Respond with JSON only:
{
  "sentiment": "positive|neutral|concerned|frustrated|angry",
  "escalationRisk": 0.0-1.0,
  "signals": ["<phrase>", ...],
  "explanation": "<one sentence>"
}

Escalation risk = probability the situation escalates if not addressed soon."""


def run(ctx: dict, claude, front) -> dict:
    m6_out = (ctx.get("m6") or {}).get("output") or {}
    messages = ctx.get("messages") or []
    last_two_text = "\n\n---\n\n".join(
        front.extract_plain_text_body(m) for m in messages[-2:]
    )[:4000]

    user_prompt = f"Summary:\n{m6_out.get('tldr', '(none)')}\n\nLast messages:\n{last_two_text}"

    res = claude.call(
        system=SYSTEM,
        user=user_prompt,
        model=claude.fast_model,
        max_tokens=250,
        json_mode=True,
        cached_system=True,
    )

    cost = res["cost_usd"]
    data = res["json"]

    if not data or data.get("sentiment") not in VALUES:
        logger.warning(f"M5 invalid response for {ctx['conv'].get('id')}: {res.get('parse_error')} | {res['text'][:120]}")
        return {"ok": False, "output": None, "cost_usd": cost,
                "writes": [], "error": res.get("parse_error") or "invalid sentiment"}

    writes: list[dict] = []
    if not ctx.get("dry_run"):
        cid = ctx["conv"]["id"]
        if float(data.get("escalationRisk", 0)) >= 0.5:
            tag_name = f"sentiment/{data['sentiment']}"
            front.add_tag(cid, tag_name)
            writes.append({"type": "tag", "name": tag_name})

        if not front.has_comment_with_prefix(cid, COMMENT_PREFIX):
            lines = [
                f"{COMMENT_PREFIX} Sentiment",
                "",
                f"Sentiment: {data['sentiment']}",
                f"Escalation risk: {float(data.get('escalationRisk', 0)):.2f}",
            ]
            if data.get("explanation"):
                lines.append(f"Explanation: {data['explanation']}")
            if data.get("signals"):
                lines.append(f"Signals: {'; '.join(data['signals'])}")
            front.add_comment(cid, "\n".join(lines))
            writes.append({"type": "comment", "prefix": COMMENT_PREFIX})

    return {"ok": True, "output": data, "cost_usd": cost, "writes": writes, "error": None}
