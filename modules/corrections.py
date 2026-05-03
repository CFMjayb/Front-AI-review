"""Corrections scanner — detects when Jay changes an AI-assigned category tag.

Jay just changes the AI/<category> tag in Front to the correct one.
The scanner compares the tag against the original category in the
[AI/analysis] comment, and grabs any existing comments for context.
No special format or extra work required from Jay.
"""
import logging
import re
from typing import Optional

from modules.analyze import CATEGORIES

logger = logging.getLogger(__name__)

_CATEGORY_SET = set(CATEGORIES)
_ORIGINAL_CAT_RE = re.compile(r"Category:\s*(\w+)", re.IGNORECASE)


def _current_ai_category(conv: dict) -> Optional[str]:
    cats = [
        t["name"][3:]
        for t in (conv.get("tags") or [])
        if t.get("name", "").startswith("AI/") and t["name"][3:] in _CATEGORY_SET
    ]
    return cats[0] if len(cats) == 1 else None


def detect_corrections(skipped_conversations: list[dict], front) -> list[dict]:
    """
    For each already-processed conversation, compare the current AI/<category>
    tag against the original in the [AI/analysis] comment. If they differ,
    record the correction and include any comments on the conversation as context.
    """
    corrections = []

    for conv in skipped_conversations:
        human_cat = _current_ai_category(conv)
        if not human_cat:
            continue

        cid = conv.get("id", "")
        try:
            comments = front.get_conversation_comments(cid)
        except Exception:
            continue

        # Find original AI category from the analysis comment
        ai_cat = None
        for c in (comments or []):
            body = c.get("body") or ""
            if body.startswith("[AI/analysis]"):
                m = _ORIGINAL_CAT_RE.search(body)
                if m:
                    ai_cat = m.group(1).lower()
                    break

        if not ai_cat or ai_cat == human_cat:
            continue

        # Collect non-AI comments as context for why the change was made
        context_comments = [
            c.get("body", "").strip()
            for c in (comments or [])
            if not (c.get("body") or "").startswith("[AI/")
            and (c.get("body") or "").strip()
        ]

        corrections.append({
            "conversation_id": cid,
            "subject": (conv.get("subject") or "")[:80],
            "ai_category": ai_cat,
            "human_category": human_cat,
            "context": context_comments[-1] if context_comments else "",
        })

    return corrections


def log_corrections(corrections: list[dict]) -> None:
    for c in corrections:
        ctx = f' context="{c["context"][:100]}"' if c.get("context") else ""
        logger.info(
            f"CORRECTION: conv={c['conversation_id']} "
            f"ai={c['ai_category']} → human={c['human_category']}"
            f"{ctx} subject=\"{c['subject']}\""
        )
    if corrections:
        logger.info(f"Corrections this run: {len(corrections)}")


def apply_corrections(corrections: list[dict]) -> None:
    """Append new corrections to the analyze-examples secret for use in future runs."""
    if not corrections:
        return
    try:
        from auth import read_examples, write_examples
        existing = read_examples()
        seen_ids = {e.get("conversation_id") for e in existing}
        new_ones = [c for c in corrections if c.get("conversation_id") not in seen_ids]
        if new_ones:
            write_examples(existing + new_ones)
            logger.info(f"Added {len(new_ones)} correction(s) to analyze prompt examples")
    except Exception as exc:
        logger.warning(f"apply_corrections failed: {exc}")


def format_digest_section(corrections: list[dict]) -> str:
    if not corrections:
        return ""
    lines = [
        "## Category Corrections This Week",
        "",
        "To improve accuracy, ask Claude Code: \"add this week's corrections as examples\".",
        "",
    ]
    for c in corrections:
        ctx = f"\n  *Context: {c['context'][:120]}*" if c.get("context") else ""
        lines.append(
            f"- **{c['conversation_id']}** — "
            f"AI: `{c['ai_category']}` → `{c['human_category']}`  \n"
            f"  Subject: *{c['subject']}*{ctx}"
        )
    return "\n".join(lines)
