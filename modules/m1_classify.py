"""M1 — Email classification.

Classifies into one of 11 EDOM categories. Tags conversation with AI/{category} if confidence ≥ 0.7.
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

CATEGORIES = [
    "pastoral", "admin", "finance", "event", "clergy", "personnel",
    "press", "parishioner", "vendor", "spam", "other",
]

SYSTEM = """You classify incoming emails for the Episcopal Diocese of Maryland (EDOM).
Choose exactly one category from this list:

- pastoral: pastoral care, spiritual matters, congregational life
- admin: scheduling, operations, internal coordination, IT
- finance: invoices, payroll, expenses, budgets, donations
- event: event planning, RSVPs, logistics for diocesan events
- clergy: clergy deployment, transitions, ordinations, bishop matters
- personnel: HR, staff hiring, performance, employee relations
- press: media inquiries, press relations, external communications
- parishioner: messages from individual parishioners
- vendor: external vendors, suppliers, contractors
- spam: unsolicited marketing, phishing, off-topic
- other: anything not fitting above categories

Respond with JSON only:
{"category": "<one of the above>", "confidence": 0.0-1.0, "reasoning": "<one short sentence>"}
"""


def run(ctx: dict, claude, front) -> dict:
    """Run M1.

    ctx: { conv, messages, transcript, dry_run }
    claude: ClaudeClient
    front: FrontClient
    Returns: { ok, output, cost_usd, writes, error }
    """
    first_msg = (ctx.get("messages") or [{}])[0]
    subject = ctx["conv"].get("subject") or "(no subject)"
    author = first_msg.get("author") or {}
    sender = author.get("email") or author.get("handle") or "unknown"
    body = front.extract_plain_text_body(first_msg)[:2000]

    user_prompt = f"Subject: {subject}\nFrom: {sender}\n\nBody:\n{body}"

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

    if not data or data.get("category") not in CATEGORIES:
        logger.warning(f"M1 invalid response for {ctx['conv'].get('id')}: {res.get('parse_error')} | {res['text'][:120]}")
        return {"ok": False, "output": None, "cost_usd": cost,
                "writes": [], "error": res.get("parse_error") or "invalid category"}

    writes: list[dict] = []
    if not ctx.get("dry_run") and float(data.get("confidence", 0)) >= 0.7:
        tag_name = f"AI/{data['category']}"
        front.add_tag(ctx["conv"]["id"], tag_name)
        writes.append({"type": "tag", "name": tag_name})

    return {"ok": True, "output": data, "cost_usd": cost, "writes": writes, "error": None}
