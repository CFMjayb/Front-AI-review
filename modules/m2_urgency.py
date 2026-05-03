"""M2 — Urgency classification. Tags urgency/{level} if confidence ≥ 0.75."""
import logging

logger = logging.getLogger(__name__)

LEVELS = ["urgent", "high", "normal", "low"]

SYSTEM = """You assign urgency levels to EDOM emails. Use this rubric:

- urgent: Same-day action required. Critical financial issue, system error, time-sensitive request, escalating parishioner, IRS/legal deadlines.
- high: 1-2 days. Important but not immediately critical. Large transactions, approvals needed, compliance items.
- normal: This week. Standard processing. Routine invoices, regular communications, administrative tasks.
- low: Can defer. Non-urgent, archive candidates, FYI items.

Respond with JSON only:
{"urgency": "urgent|high|normal|low", "confidence": 0.0-1.0, "signals": ["<short phrase>", ...]}"""


def run(ctx: dict, claude, front) -> dict:
    m1_out = (ctx.get("m1") or {}).get("output") or {}
    subject = ctx["conv"].get("subject") or "(no subject)"
    user_prompt = f"Subject: {subject}\nClassification: {m1_out.get('category', 'unknown')}\n\nTranscript:\n{ctx['transcript']}"

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

    if not data or data.get("urgency") not in LEVELS:
        logger.warning(f"M2 invalid response for {ctx['conv'].get('id')}: {res.get('parse_error')} | {res['text'][:120]}")
        return {"ok": False, "output": None, "cost_usd": cost,
                "writes": [], "error": res.get("parse_error") or "invalid urgency"}

    writes: list[dict] = []
    if not ctx.get("dry_run") and float(data.get("confidence", 0)) >= 0.75:
        tag_name = f"urgency/{data['urgency']}"
        front.add_tag(ctx["conv"]["id"], tag_name)
        writes.append({"type": "tag", "name": tag_name})

    return {"ok": True, "output": data, "cost_usd": cost, "writes": writes, "error": None}
