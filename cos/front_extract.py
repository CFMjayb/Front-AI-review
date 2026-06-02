"""M3 — Front loop extraction.

Turns the analysis the pipeline already paid for into open-loop ledger entries,
and cheaply reconciles existing loops as threads move. No extra Claude calls.

Direction logic (see docs/chief-of-staff/DESIGN.md §4):
  - last message INBOUND  + needs reply/approval/payment/action → i_owe (open)
  - last message OUTBOUND + had an ask + quiet ≥ threshold       → owed_to_me (waiting)

Reconcile (Claude-free) over loops already in the ledger:
  - i_owe whose thread now ends with an outbound (Jay replied)  → done
  - owed_to_me whose thread got a new inbound (they replied)    → done
  - otherwise refresh last_activity
"""
import logging
import os
import time

from cos import ledger

logger = logging.getLogger(__name__)

NOISE_CATEGORIES = {"spam"}
URGENCY_IMPORTANCE = {"urgent": 5, "high": 4, "normal": 3, "low": 2}


def _quiet_threshold_hours() -> float:
    return float(os.environ.get("QUIET_THRESHOLD_HOURS", "36"))


def front_source_link(conversation_id: str) -> str:
    return f"https://app.frontapp.com/open/{conversation_id}"


def _iso(epoch_s) -> str:
    if not epoch_s:
        return ledger.now_iso()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_s))


def _last_message(messages: list[dict]) -> dict | None:
    if not messages:
        return None
    return max(messages, key=lambda m: m.get("created_at") or 0)


def _author_name(author: dict) -> str:
    name = f"{author.get('first_name') or ''} {author.get('last_name') or ''}".strip()
    return name or author.get("email") or author.get("handle") or "unknown"


def _trim(text: str, limit: int = 160) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _summary_from(analysis: dict) -> str:
    summary = analysis.get("action_summary")
    if not summary or summary == "FYI only":
        questions = analysis.get("open_questions") or []
        summary = questions[0] if questions else analysis.get("tldr")
    return _trim(summary or "(no summary)")


def extract_from_analysis(conv: dict, messages: list[dict], analysis: dict,
                          *, dry_run: bool = False) -> dict | None:
    """Decide whether a thread is an open loop and upsert it. Returns the loop
    (or a dry-run preview), or None when the thread isn't a loop."""
    if os.environ.get("COS_ENABLED", "true").lower() != "true":
        return None
    if not analysis:
        return None
    if analysis.get("category") in NOISE_CATEGORIES:
        return None

    last = _last_message(messages)
    if last is None:
        return None

    conv_id = conv.get("id")
    inbound = bool(last.get("is_inbound"))
    last_iso = _iso(last.get("created_at"))
    importance = URGENCY_IMPORTANCE.get(analysis.get("urgency"), 3)
    confidence = float(analysis.get("urgency_confidence")
                       or analysis.get("category_confidence") or 0.5)
    common = dict(
        summary=_summary_from(analysis), channel="front", source_ref=conv_id,
        source_link=front_source_link(conv_id), category=analysis.get("category") or "",
        importance=importance, confidence=confidence,
        due_at=analysis.get("deadline") or "", last_activity=last_iso,
    )

    if inbound:
        needs = (analysis.get("requires_reply") or analysis.get("requires_approval")
                 or analysis.get("requires_payment") or bool(analysis.get("action_items")))
        if not needs:
            return None
        author = last.get("author") or {}
        direction, status = "i_owe", "open"
        counterparty = _author_name(author)
        counterparty_email = author.get("email") or ""
    else:
        has_ask = bool(analysis.get("open_questions")) or analysis.get("requires_reply")
        if not has_ask:
            return None
        quiet_h = (time.time() - (last.get("created_at") or 0)) / 3600
        if quiet_h < _quiet_threshold_hours():
            return None  # still fresh; a later sweep promotes it past the threshold
        recipients = last.get("to") or []
        counterparty = recipients[0].get("handle") if recipients else "unknown"
        counterparty_email = counterparty if "@" in (counterparty or "") else ""
        direction, status = "owed_to_me", "waiting"

    if dry_run:
        logger.info(f"[dry-run] would upsert {direction} loop for {conv_id} "
                    f"({counterparty}): {common['summary']}")
        return {"dry_run": True, "direction": direction, "counterparty": counterparty,
                "status": status, **common}

    return ledger.upsert_loop(direction=direction, counterparty=counterparty,
                              counterparty_email=counterparty_email, status=status,
                              **common)


def reconcile_open_front_loops(front, *, dry_run: bool = False) -> dict:
    """Cheap, Claude-free pass over Front loops already in the ledger.
    Bounded by the number of open loops, not the inbox size."""
    loops = [l for l in ledger.list_loops(channel="front")
             if l["status"] not in ("done", "dropped")]
    resolved = updated = errored = 0

    for loop in loops:
        conv_id = loop["source_ref"]
        try:
            messages = front.get_conversation_messages(conv_id)
        except Exception as exc:
            logger.debug(f"reconcile: messages fetch failed for {conv_id}: {exc}")
            errored += 1
            continue

        last = _last_message(messages)
        if last is None:
            continue
        inbound = bool(last.get("is_inbound"))
        last_iso = _iso(last.get("created_at"))

        # i_owe + Jay replied (outbound last), or owed_to_me + they replied (inbound last)
        if (loop["direction"] == "i_owe" and not inbound) or \
           (loop["direction"] == "owed_to_me" and inbound):
            if not dry_run:
                ledger.resolve_loop(loop["id"], "done")
            resolved += 1
        elif last_iso != loop["last_activity"]:
            if not dry_run:
                ledger.upsert_loop(direction=loop["direction"], counterparty=loop["counterparty"],
                                   summary=loop["summary"], channel="front",
                                   source_ref=conv_id, last_activity=last_iso)
            updated += 1

    logger.info(f"Loop reconcile: {len(loops)} open / {resolved} resolved / "
                f"{updated} refreshed / {errored} errored")
    return {"reconciled": len(loops), "resolved": resolved, "updated": updated,
            "errored": errored}
