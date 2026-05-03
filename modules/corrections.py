"""Corrections scanner — collects Jay's category corrections for prompt improvement.

WORKFLOW
--------
1. AI categorizes an email and applies AI/<category> tag.
2. Jay disagrees and writes a Front comment:

       [AI/correction] clergy — scheduling request for a specific priest, not pastoral care

   Format:  [AI/correction] <correct-category> — <reason why>
   The reason is optional but strongly recommended — it becomes the few-shot example.

3. Each pipeline run scans already-processed conversations for these comments
   and logs a CORRECTION: line to stdout (captured by Cloud Logging).

4. The weekly digest surfaces a summary.  Jay reviews and tells Claude Code
   "add these corrections as examples" — Claude Code reads them via MCP and
   updates the few-shot section in modules/analyze.py.

DETECTION (tag-change fallback)
--------------------------------
If no [AI/correction] comment is found but Jay changed the AI/<category> tag,
the scanner can still detect the correction (without a reason).  This is a
weaker signal — prefer writing the comment.
"""
import logging
import re
import time
from typing import Optional

from modules.analyze import CATEGORIES

logger = logging.getLogger(__name__)

_CATEGORY_SET = set(CATEGORIES)
CORRECTION_PREFIX = "[AI/correction]"
_ANALYSIS_PREFIX = "[AI/analysis]"
_CORRECTION_RE = re.compile(
    r"\[AI/correction\]\s*(\w+)(?:\s*[—\-]+\s*(.+))?", re.IGNORECASE
)
_ORIGINAL_CAT_RE = re.compile(r"Category:\s*(\w+)", re.IGNORECASE)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _current_ai_category(conv: dict) -> Optional[str]:
    cats = [
        t["name"][3:]
        for t in (conv.get("tags") or [])
        if t.get("name", "").startswith("AI/") and t["name"][3:] in _CATEGORY_SET
    ]
    return cats[0] if len(cats) == 1 else None


def _parse_analysis_comment(comments: list[dict]) -> Optional[str]:
    for c in (comments or []):
        body = c.get("body") or ""
        if body.startswith(_ANALYSIS_PREFIX):
            m = _ORIGINAL_CAT_RE.search(body)
            if m:
                return m.group(1).lower()
    return None


def _parse_correction_comment(comments: list[dict]) -> Optional[dict]:
    """Return the first [AI/correction] comment as {category, reason, body}."""
    for c in (comments or []):
        body = c.get("body") or ""
        m = _CORRECTION_RE.match(body.strip())
        if m:
            cat = m.group(1).lower()
            reason = (m.group(2) or "").strip()
            if cat in _CATEGORY_SET:
                return {"category": cat, "reason": reason, "body": body}
    return None


# ── Main scanner ─────────────────────────────────────────────────────────────

def detect_corrections(skipped_conversations: list[dict], front) -> list[dict]:
    """
    Scan already-processed conversations for category corrections.

    Checks for [AI/correction] comments first; falls back to tag-change detection.
    Returns a list of correction dicts.
    """
    corrections: list[dict] = []
    now = int(time.time())

    for conv in skipped_conversations:
        cid = conv.get("id", "")
        subject = (conv.get("subject") or "")[:80]

        try:
            comments = front.get_conversation_comments(cid)
        except Exception as exc:
            logger.debug(f"corrections: comment fetch failed for {cid}: {exc}")
            continue

        # Primary: explicit [AI/correction] comment
        correction_comment = _parse_correction_comment(comments)
        if correction_comment:
            ai_cat = _parse_analysis_comment(comments) or "unknown"
            corrections.append({
                "conversation_id": cid,
                "subject": subject,
                "ai_category": ai_cat,
                "human_category": correction_comment["category"],
                "reason": correction_comment["reason"],
                "source": "comment",
                "detected_at_s": now,
            })
            continue

        # Fallback: tag-change detection (no reason captured)
        human_cat = _current_ai_category(conv)
        if not human_cat:
            continue
        ai_cat = _parse_analysis_comment(comments)
        if ai_cat and ai_cat != human_cat:
            corrections.append({
                "conversation_id": cid,
                "subject": subject,
                "ai_category": ai_cat,
                "human_category": human_cat,
                "reason": "",
                "source": "tag_change",
                "detected_at_s": now,
            })

    return corrections


# ── Output helpers ───────────────────────────────────────────────────────────

def log_corrections(corrections: list[dict]) -> None:
    """Emit CORRECTION: log lines to stdout (captured by Cloud Logging)."""
    for c in corrections:
        reason_part = f' reason="{c["reason"]}"' if c.get("reason") else ""
        logger.info(
            f"CORRECTION: conv={c['conversation_id']} "
            f"ai={c['ai_category']} → human={c['human_category']}"
            f"{reason_part} subject=\"{c['subject']}\""
        )
    if corrections:
        logger.info(
            f"Corrections this run: {len(corrections)} "
            f"({sum(1 for c in corrections if c['source'] == 'comment')} with reason, "
            f"{sum(1 for c in corrections if c['source'] == 'tag_change')} tag-change only)"
        )


def format_digest_section(corrections: list[dict]) -> str:
    """Format corrections for the weekly digest."""
    if not corrections:
        return ""
    lines = [
        "## Category Corrections",
        "",
        "Jay corrected these AI-assigned categories this week.",
        "To improve future accuracy, add them as few-shot examples in",
        "`modules/analyze.py` (ask Claude Code: \"add this week's corrections as examples\").",
        "",
    ]
    for c in corrections:
        reason = f" — {c['reason']}" if c.get("reason") else " *(no reason — add an [AI/correction] comment for better examples)*"
        lines.append(
            f"- **{c['conversation_id']}** — "
            f"AI: `{c['ai_category']}` → `{c['human_category']}`{reason}  \n"
            f"  Subject: *{c['subject']}*"
        )
    return "\n".join(lines)
