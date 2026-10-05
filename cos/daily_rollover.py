"""Daily-email rollover: when a new daily email arrives, archive the earlier ones.

Jay (2026-10-05): "I would like the daily email to archive the prior day when a new
one is issued." The nightly notifications@cfmins.org mail ("Your day", "CFM Daily
Processing") is only useful until the next one arrives; left alone each day's copy
sits in the inbox until someone clears it by hand.

Driven by the sender rule, so nothing is hardcoded here. A rule may carry

    supersede_series: [<regex>, ...]

(a list of regexes matched against the subject, case-insensitive; each regex is one
"series"). For every series we look at the sender's conversations, find the newest
one (whatever its status), and archive every OLDER one that is still open. The
newest is never touched, and a series with a single conversation does nothing.

Safe by construction:
  - a conversation is archived only if every message in it is an inbound message
    from the sender, with no draft. If anyone replied, forwarded or is drafting,
    it is left open (that is a conversation a person acted on);
  - it only ever archives, never deletes; every archive is logged with its id so
    it can be reopened (PATCH status=open);
  - capped per run (DAILY_ROLLOVER_LIMIT, default 25); disable with
    DAILY_ROLLOVER_ENABLED=false.

Only runs on the Firestore backend (that is where sender_rules live).
"""
import logging
import os
import re
from typing import Any, Optional

from cos import front_archive
from cos.front_archive import OPEN_STATUSES

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 25
SEARCH_PAGES = 2          # 50 per page: ~50 days of two daily emails


def _matches(pattern: str, subject: str) -> bool:
    try:
        return bool(re.search(pattern, subject or "", re.I))
    except re.error:
        logger.warning("daily rollover: invalid supersede_series regex %r", pattern)
        return False


def plan_series(convs: list[dict], pattern: str) -> tuple[Optional[dict], list[dict]]:
    """(newest, older_open) for one series. older_open is newest-first."""
    series = [c for c in convs if _matches(pattern, c.get("subject"))]
    if not series:
        return None, []
    series.sort(key=lambda c: (c.get("created_at") or 0, c.get("id") or ""), reverse=True)
    newest, rest = series[0], series[1:]
    return newest, [c for c in rest if (c.get("status") or "") in OPEN_STATUSES]


def _only_sender_inbound(messages: list[dict]) -> bool:
    """True when nobody but the sender has touched the conversation."""
    if not messages:
        return False                       # could not read it: leave it alone
    return all(m.get("is_inbound") and not m.get("is_draft") for m in messages)


def run_rule(front: Any, rule: dict, *, dry_run: bool = False,
             budget: int = DEFAULT_LIMIT) -> dict:
    """Apply one sender rule's supersede_series. Returns counts."""
    counts = {"checked": 0, "archived": 0, "kept_acted_on": 0, "errors": 0}
    sender = (rule.get("email") or "").strip().lower()
    patterns = [p for p in (rule.get("supersede_series") or []) if p]
    if not sender or "@" not in sender or sender.startswith("@") or not patterns:
        return counts
    convs = front_archive.call_with_429_retry(
        front.search_conversations, f"from:{sender}", max_pages=SEARCH_PAGES)
    for pattern in patterns:
        newest, older = plan_series(convs, pattern)
        for conv in older:
            if counts["archived"] >= budget:
                return counts
            cid = conv["id"]
            counts["checked"] += 1
            try:
                msgs = front_archive.call_with_429_retry(front.get_conversation_messages, cid)
                if not _only_sender_inbound(msgs):
                    counts["kept_acted_on"] += 1
                    logger.info(f"Daily rollover: {cid} left open (someone replied/drafted)")
                    continue
                if dry_run:
                    logger.info(f"[dry-run] daily rollover: would archive {cid} "
                                f"({conv.get('subject')!r}), superseded by {newest['id']}")
                else:
                    front_archive.call_with_429_retry(front.set_status, cid, "archived")
                    logger.info(f"Daily rollover: archived {cid} ({conv.get('subject')!r}), "
                                f"superseded by {newest['id']}")
                counts["archived"] += 1
            except Exception as exc:
                counts["errors"] += 1
                logger.warning(f"Daily rollover WARNING: {cid}: {exc}")
    return counts


def run(front: Any, *, dry_run: bool = False, limit: Optional[int] = None) -> dict:
    """Pipeline entry point. No-op unless the Firestore ledger is in use."""
    if os.environ.get("DAILY_ROLLOVER_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return {"disabled": True}
    if os.environ.get("LEDGER_BACKEND", "sqlite").lower() != "firestore":
        return {"skipped": "non-firestore ledger"}
    from cos import ledger_firestore as lf
    rules = [r for r in lf.list_sender_rules() if r.get("supersede_series")]
    total = {"checked": 0, "archived": 0, "kept_acted_on": 0, "errors": 0}
    cap = limit or int(os.environ.get("DAILY_ROLLOVER_LIMIT", DEFAULT_LIMIT))
    for rule in rules:
        left = cap - total["archived"]
        if left <= 0:
            break
        try:
            got = run_rule(front, rule, dry_run=dry_run, budget=left)
        except Exception as exc:
            total["errors"] += 1
            logger.warning(f"Daily rollover WARNING: rule {rule.get('email')}: {exc}")
            continue
        for k in total:
            total[k] += got[k]
    if total["checked"] or total["errors"]:
        logger.info(f"Daily rollover: {total}")
    return total
