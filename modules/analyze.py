"""Consolidated analysis — replaces M1-M7 with a single Claude call.

Returns category, urgency, action items, summary, sentiment, and routing in one JSON.
Model: ANTHROPIC_MODEL_ANALYZE env var (defaults to claude-sonnet-4-6).
"""
import logging
import os

logger = logging.getLogger(__name__)

CATEGORIES = ["pastoral", "admin", "finance", "event", "clergy", "personnel",
              "press", "parishioner", "vendor", "spam", "other"]

URGENCY_LEVELS = ["urgent", "high", "normal", "low"]

SENTIMENT_VALUES = ["positive", "neutral", "concerned", "frustrated", "angry"]

ROUTES = ["bishop", "canon-to-ordinary", "finance-office", "communications",
          "keep-with-jay", "vendor-coordinator"]

COMMENT_PREFIX = "[AI/analysis]"

SYSTEM = """You are an AI assistant for Jay Bentzen at the Episcopal Diocese of Maryland (EDOM).
Analyze the email thread and return ALL of the following in one JSON response:

CATEGORIES (pick one):
pastoral, admin, finance, event, clergy, personnel, press, parishioner, vendor, spam, other

URGENCY (pick one):
- urgent: Same-day action required. Critical financial issue, legal deadline, escalating situation.
- high: 1-2 days. Important but not immediately critical. Large transactions, approvals needed.
- normal: This week. Standard processing. Routine communications, administrative tasks.
- low: Can defer. Non-urgent, FYI items, archive candidates.

SENTIMENT (pick one): positive, neutral, concerned, frustrated, angry
Escalation risk = probability situation escalates if not addressed soon.

ROUTING (pick one):
- bishop: matters requiring the Bishop's direct attention
- canon-to-ordinary: clergy personnel, deployment, formation
- finance-office: invoices, payroll, financial questions
- communications: press, public-facing comms, social media
- vendor-coordinator: vendor management, contracts
- keep-with-jay: anything Jay should handle himself

Respond with JSON only:
{
  "category": "<one of the categories>",
  "category_confidence": 0.0-1.0,
  "urgency": "<urgent|high|normal|low>",
  "urgency_confidence": 0.0-1.0,
  "urgency_signals": ["<short phrase>"],
  "requires_reply": <bool>,
  "requires_approval": <bool>,
  "requires_payment": <bool>,
  "deadline": "<ISO 8601 date or null>",
  "action_items": ["<short imperative phrase>"],
  "action_summary": "<one sentence or 'FYI only'>",
  "tldr": "<2-3 sentences capturing the gist>",
  "key_points": ["<bullet>"],
  "parties": ["<name or email>"],
  "open_questions": ["<unanswered question>"],
  "sentiment": "<one of the sentiment values>",
  "escalation_risk": 0.0-1.0,
  "sentiment_signals": ["<phrase>"],
  "sentiment_explanation": "<one sentence>",
  "suggested_assignee": "<one of the routes>",
  "routing_confidence": 0.0-1.0,
  "routing_reasoning": "<one sentence>"
}"""


def _load_correction_examples() -> str:
    """Append human-correction examples from Secret Manager to the system prompt."""
    try:
        from auth import read_examples
        examples = read_examples()
        if not examples:
            return ""
        lines = ["\n## Correction Examples (human feedback — use these to refine judgments)\n"]
        for e in examples:
            ctx_part = f'\n  Comment context: "{e["context"][:120]}"' if e.get("context") else ""
            lines.append(
                f'- AI said "{e["ai_category"]}", corrected to "{e["human_category"]}": '
                f'subject "{e["subject"]}"{ctx_part}'
            )
        return "\n".join(lines)
    except Exception:
        return ""


SYSTEM = SYSTEM + _load_correction_examples()

# Guidance cache — populated once per process (per Cloud Run job execution).
_guidance_cache: list[dict] | None = None

def _load_guidance_text() -> str:
    """Return active guidance records as a prompt section, cached per process."""
    global _guidance_cache
    if _guidance_cache is None:
        try:
            from cos import ledger
            _guidance_cache = ledger.list_guidance(active_only=True)
        except Exception:
            _guidance_cache = []
    if not _guidance_cache:
        return ""
    lines = ["\n\n## Standing Instructions (always apply when classifying)\n"]
    for g in _guidance_cache:
        scope = g.get("scope") or "all"
        prefix = f"[{scope}] " if scope != "all" else ""
        lines.append(f"- {prefix}{g['body']}")
    return "\n".join(lines)


def _build_system() -> str:
    return SYSTEM + _load_guidance_text()


def _get_model(claude) -> str:
    return os.environ.get("ANTHROPIC_MODEL_ANALYZE", "claude-sonnet-4-6")


def analyze_transcript(claude, *, subject: str, sender: str, transcript: str) -> dict:
    """Write-free analysis of any thread transcript (used by non-Front channels).

    Same SYSTEM prompt and JSON contract as run(), but performs no Front writes —
    returns {ok, output, cost_usd} for the caller to act on.
    """
    user_prompt = f"Subject: {subject}\nFrom: {sender}\n\nTranscript:\n{transcript}"
    res = claude.call(
        system=_build_system(), user=user_prompt, model=_get_model(claude),
        max_tokens=1200, json_mode=True, cached_system=True,
    )
    data = res.get("json")
    if not data or data.get("category") not in CATEGORIES:
        return {"ok": False, "output": None, "cost_usd": res["cost_usd"],
                "error": res.get("parse_error") or "invalid response"}
    return {"ok": True, "output": data, "cost_usd": res["cost_usd"], "error": None}


def run(ctx: dict, claude, front) -> dict:
    first_msg = (ctx.get("messages") or [{}])[0]
    subject = ctx["conv"].get("subject") or "(no subject)"
    author = first_msg.get("author") or {}
    sender = author.get("email") or author.get("handle") or "unknown"

    user_prompt = f"Subject: {subject}\nFrom: {sender}\n\nTranscript:\n{ctx['transcript']}"

    res = claude.call(
        system=_build_system(),
        user=user_prompt,
        model=_get_model(claude),
        max_tokens=1200,
        json_mode=True,
        cached_system=True,
    )

    cost = res["cost_usd"]
    data = res["json"]

    if not data or data.get("category") not in CATEGORIES:
        logger.warning(f"analyze invalid response for {ctx['conv'].get('id')}: {res.get('parse_error')} | {res['text'][:120]}")
        return {"ok": False, "output": None, "cost_usd": cost,
                "writes": [], "error": res.get("parse_error") or "invalid response"}

    writes: list[dict] = []
    dry_run = ctx.get("dry_run", False)
    cid = ctx["conv"]["id"]

    if not dry_run:
        # Category tag
        if float(data.get("category_confidence", 0)) >= 0.7:
            tag = f"AI/{data['category']}"
            front.add_tag(cid, tag)
            writes.append({"type": "tag", "name": tag})

        # Urgency tag
        if float(data.get("urgency_confidence", 0)) >= 0.75 and data.get("urgency") in URGENCY_LEVELS:
            tag = f"urgency/{data['urgency']}"
            front.add_tag(cid, tag)
            writes.append({"type": "tag", "name": tag})

        # Sentiment tag (only when escalation risk is high)
        if float(data.get("escalation_risk", 0)) >= 0.5 and data.get("sentiment") in SENTIMENT_VALUES:
            tag = f"sentiment/{data['sentiment']}"
            front.add_tag(cid, tag)
            writes.append({"type": "tag", "name": tag})

        # Single analysis comment
        if not front.has_comment_with_prefix(cid, COMMENT_PREFIX):
            comment = _build_comment(data)
            front.add_comment(cid, comment)
            writes.append({"type": "comment", "prefix": COMMENT_PREFIX})

    return {"ok": True, "output": data, "cost_usd": cost, "writes": writes, "error": None}


def _build_comment(data: dict) -> str:
    lines = [
        COMMENT_PREFIX,
        "",
        f"Category: {data.get('category', '?')}  |  Urgency: {data.get('urgency', '?')}  |  Sentiment: {data.get('sentiment', '?')}",
        "",
        f"Summary: {data.get('tldr', '')}",
    ]

    if data.get("action_summary") and data["action_summary"] != "FYI only":
        lines += ["", f"Action: {data['action_summary']}"]

    if data.get("action_items"):
        lines.append("Items:")
        lines.extend(f"  - {a}" for a in data["action_items"])

    if data.get("deadline"):
        lines.append(f"Deadline: {data['deadline']}")

    if data.get("open_questions"):
        lines += ["", "Open questions:"]
        lines.extend(f"  - {q}" for q in data["open_questions"])

    if float(data.get("escalation_risk", 0)) >= 0.5:
        lines += ["", f"⚠ Escalation risk: {float(data['escalation_risk']):.0%} — {data.get('sentiment_explanation', '')}"]

    lines += ["", f"Routing: {data.get('suggested_assignee', '?')} — {data.get('routing_reasoning', '')}"]
    lines += ["", "(Routing is a suggestion only — not auto-assigned.)"]

    return "\n".join(lines)
