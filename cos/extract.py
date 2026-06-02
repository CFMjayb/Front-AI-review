"""Channel-agnostic open-loop extraction core.

Front, Outlook, and Teams all normalize their threads into the same shape and
run through this one set of direction rules, so a loop means the same thing no
matter where it came from. Channel adapters (cos/front_extract.py,
cos/ms_ingest.py) do the normalization; the logic lives here.

Normalized message:  {inbound, ts_epoch, sender_name, sender_email, recipients, text}
Normalized thread:   {channel, source_ref, subject, source_link, messages: [...]}
"""
import logging
import os
import time

from cos import ledger

logger = logging.getLogger(__name__)

NOISE_CATEGORIES = {"spam"}
URGENCY_IMPORTANCE = {"urgent": 5, "high": 4, "normal": 3, "low": 2}


def quiet_threshold_hours() -> float:
    return float(os.environ.get("QUIET_THRESHOLD_HOURS", "36"))


def cos_enabled() -> bool:
    return os.environ.get("COS_ENABLED", "true").lower() == "true"


def owner_emails() -> set[str]:
    """Addresses that are "Jay" — drives inbound/outbound and meeting ownership."""
    raw = os.environ.get("COS_OWNER_EMAILS", "") or os.environ.get("SENDER_TO", "")
    return {v.strip().lower() for v in raw.split(",") if v.strip()}


def iso(epoch_s) -> str:
    if not epoch_s:
        return ledger.now_iso()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_s))


def make_message(*, inbound: bool, ts_epoch: float = 0, sender_name: str = "",
                 sender_email: str = "", recipients: list[str] | None = None,
                 text: str = "") -> dict:
    return {"inbound": bool(inbound), "ts_epoch": ts_epoch or 0,
            "sender_name": sender_name, "sender_email": sender_email,
            "recipients": recipients or [], "text": text}


def build_thread(*, channel: str, source_ref: str, subject: str = "",
                 source_link: str = "", messages: list[dict]) -> dict:
    return {"channel": channel, "source_ref": source_ref, "subject": subject,
            "source_link": source_link, "messages": messages}


def last_message(thread: dict) -> dict | None:
    messages = thread.get("messages") or []
    return max(messages, key=lambda m: m.get("ts_epoch") or 0) if messages else None


def _trim(text: str, limit: int = 160) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _summary_from(analysis: dict) -> str:
    summary = analysis.get("action_summary")
    if not summary or summary == "FYI only":
        questions = analysis.get("open_questions") or []
        summary = questions[0] if questions else analysis.get("tldr")
    return _trim(summary or "(no summary)")


def loop_from_thread(thread: dict, analysis: dict, *, dry_run: bool = False) -> dict | None:
    """Decide whether a normalized thread is an open loop and upsert it."""
    if not cos_enabled() or not analysis:
        return None
    if analysis.get("category") in NOISE_CATEGORIES:
        return None

    last = last_message(thread)
    if last is None:
        return None

    channel = thread["channel"]
    source_ref = thread["source_ref"]
    inbound = bool(last.get("inbound"))
    last_iso = iso(last.get("ts_epoch"))
    importance = URGENCY_IMPORTANCE.get(analysis.get("urgency"), 3)
    confidence = float(analysis.get("urgency_confidence")
                       or analysis.get("category_confidence") or 0.5)
    common = dict(
        summary=_summary_from(analysis), channel=channel, source_ref=source_ref,
        source_link=thread.get("source_link", ""), category=analysis.get("category") or "",
        importance=importance, confidence=confidence,
        due_at=analysis.get("deadline") or "", last_activity=last_iso,
    )

    if inbound:
        needs = (analysis.get("requires_reply") or analysis.get("requires_approval")
                 or analysis.get("requires_payment") or bool(analysis.get("action_items")))
        if not needs:
            return None
        direction, status = "i_owe", "open"
        counterparty = last.get("sender_name") or last.get("sender_email") or "unknown"
        counterparty_email = last.get("sender_email") or ""
    else:
        has_ask = bool(analysis.get("open_questions")) or analysis.get("requires_reply")
        if not has_ask:
            return None
        quiet_h = (time.time() - (last.get("ts_epoch") or 0)) / 3600
        if quiet_h < quiet_threshold_hours():
            return None  # still fresh; a later sweep promotes it past the threshold
        recipients = last.get("recipients") or []
        counterparty = recipients[0] if recipients else "unknown"
        counterparty_email = counterparty if "@" in (counterparty or "") else ""
        direction, status = "owed_to_me", "waiting"

    if dry_run:
        logger.info(f"[dry-run] would upsert {direction} {channel} loop for "
                    f"{source_ref} ({counterparty}): {common['summary']}")
        return {"dry_run": True, "direction": direction, "counterparty": counterparty,
                "status": status, **common}

    return ledger.upsert_loop(direction=direction, counterparty=counterparty,
                              counterparty_email=counterparty_email, status=status,
                              **common)


def reconcile(loops: list[dict], fetch_messages, *, dry_run: bool = False) -> dict:
    """Claude-free pass over existing loops. fetch_messages(source_ref) returns the
    thread's normalized messages. Resolves loops once the other side has the ball."""
    active = [l for l in loops if l["status"] not in ("done", "dropped")]
    resolved = updated = errored = 0

    for loop in active:
        ref = loop["source_ref"]
        try:
            messages = fetch_messages(ref)
        except Exception as exc:
            logger.debug(f"reconcile: fetch failed for {ref}: {exc}")
            errored += 1
            continue

        last = last_message({"messages": messages})
        if last is None:
            continue
        inbound = bool(last.get("inbound"))
        last_iso = iso(last.get("ts_epoch"))

        if (loop["direction"] == "i_owe" and not inbound) or \
           (loop["direction"] == "owed_to_me" and inbound):
            if not dry_run:
                ledger.resolve_loop(loop["id"], "done")
            resolved += 1
        elif last_iso != loop["last_activity"]:
            if not dry_run:
                ledger.upsert_loop(direction=loop["direction"], counterparty=loop["counterparty"],
                                   summary=loop["summary"], channel=loop["channel"],
                                   source_ref=ref, last_activity=last_iso)
            updated += 1

    logger.info(f"Reconcile: {len(active)} open / {resolved} resolved / "
                f"{updated} refreshed / {errored} errored")
    return {"reconciled": len(active), "resolved": resolved, "updated": updated,
            "errored": errored}
