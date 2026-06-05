"""Channel-agnostic open-loop extraction core.

Front, Outlook, and Teams all normalize their threads into the same shape and
run through this one set of direction rules, so a loop means the same thing no
matter where it came from. Channel adapters (cos/front_extract.py,
cos/ms_ingest.py) do the normalization; the logic lives here.

Normalized message:  {inbound, ts_epoch, sender_name, sender_email, recipients, text}
Normalized thread:   {channel, source_ref, subject, source_link, messages: [...]}
"""
import hashlib
import logging
import os
import re
import time
from typing import Optional

from cos import ledger

logger = logging.getLogger(__name__)

NOISE_CATEGORIES = {"spam"}
URGENCY_IMPORTANCE = {"urgent": 5, "high": 4, "normal": 3, "low": 2}


def _derive_action_type(analysis: dict, *, fyi: bool = False) -> str:
    if fyi:
        return "FYI"
    if analysis.get("requires_payment"):
        return "Pay"
    if analysis.get("requires_approval"):
        return "Approve"
    if analysis.get("requires_reply"):
        return "Reply"
    if analysis.get("action_items"):
        return "Decide"
    return "Review"

# ── Dedup (DEDUP-1) ──────────────────────────────────────────────────────────
_RE_AMOUNT   = re.compile(r'\$[\d,]+(?:\.\d+)?')
_RE_DATE     = re.compile(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b')
_RE_REF      = re.compile(r'\b[A-Z]{2,6}\d{6,}\b|\bINV[-#]?\d+\b|\breminder\s+#?\d+\b',
                           re.IGNORECASE)
_RE_ORDINAL  = re.compile(r'\b\d+(st|nd|rd|th)\b', re.IGNORECASE)
_RE_SPACES   = re.compile(r'\s+')

def _normalize_subject(text: str) -> str:
    s = (text or "").lower()
    s = _RE_AMOUNT.sub(" ", s)
    s = _RE_DATE.sub(" ", s)
    s = _RE_REF.sub(" ", s)
    s = _RE_ORDINAL.sub(" ", s)
    return _RE_SPACES.sub(" ", s).strip()

def _dedup_key_for(channel: str, counterparty_email: str, summary: str) -> str:
    normalized = _normalize_subject(summary)
    raw = f"{channel}|{counterparty_email.lower()}|{normalized}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


# ── Sender rules (FILTER-1 / PRIORITY-1) ────────────────────────────────────
# Hard-coded fallback when Firestore is unavailable during startup / tests.
_FALLBACK_FYI_DOMAINS = frozenset({"hq.bill.com", "bill.com"})

def _get_sender_rule(sender_email: str) -> Optional[dict]:
    """Look up a sender rule from the ledger. Returns None if no rule found."""
    try:
        return ledger.get_sender_rule_for_email(sender_email)
    except Exception:
        return None

def _sender_forces_fyi_fallback(sender_email: str) -> bool:
    """Fallback: check against hard-coded domains when ledger lookup fails."""
    domain = sender_email.lower().split("@")[-1] if "@" in sender_email else ""
    return any(domain == d or domain.endswith("." + d) for d in _FALLBACK_FYI_DOMAINS)



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


def is_fyi(analysis: dict) -> bool:
    """Informational/notification, not a real action: a newsletter, a 'cc'd' courtesy
    note, a routine receipt. Goes to the brief's FYI section and auto-clears in 24h.

    True when the AI flags it 'FYI', OR when nothing is actually required of Jay
    (no reply/approval/payment) and it's low urgency."""
    summary = (analysis.get("action_summary") or "").strip().lower()
    if summary.startswith("fyi"):
        return True
    hard = (analysis.get("requires_reply") or analysis.get("requires_approval")
            or analysis.get("requires_payment"))
    return (not hard) and analysis.get("urgency") == "low"


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

    # Original email send date = earliest message in thread
    messages = thread.get("messages") or []
    first_msg = min(messages, key=lambda m: m.get("ts_epoch") or 0) if messages else None
    source_date = iso(first_msg.get("ts_epoch")) if first_msg else ""

    fyi_flag = is_fyi(analysis)
    summary_text = _summary_from(analysis)
    common = dict(
        summary=summary_text, channel=channel, source_ref=source_ref,
        source_link=thread.get("source_link", ""), category=analysis.get("category") or "",
        importance=importance, confidence=confidence, fyi=fyi_flag,
        due_at=analysis.get("deadline") or "", last_activity=last_iso,
        source_date=source_date,
        urgency=analysis.get("urgency") or "normal",
        action_type=_derive_action_type(analysis, fyi=fyi_flag),
        sentiment=analysis.get("sentiment") or "",
        escalation_risk=float(analysis.get("escalation_risk") or 0.0),
        suggested_assignee=analysis.get("suggested_assignee") or "",
    )

    if inbound:
        sender_email = last.get("sender_email") or ""
        counterparty = last.get("sender_name") or sender_email or "unknown"
        counterparty_email = sender_email

        # ── Sender rule check (FILTER-1 / PRIORITY-1) ────────────────────────
        rule = _get_sender_rule(sender_email)
        if rule is None and _sender_forces_fyi_fallback(sender_email):
            # Fallback: legacy hard-coded FYI domains until Firestore rule is seeded
            rule = {"action": "fyi"}

        if rule:
            action = rule.get("action", "")
            if action == "exclude":
                logger.info(f"sender-rule exclude: {source_ref} ({sender_email})")
                return None
            if action == "subscribe":
                if not dry_run:
                    ledger.upsert_loop(direction="owed_to_me", counterparty=counterparty,
                                       counterparty_email=counterparty_email, status="open",
                                       **{**common, "fyi": True, "action_type": "FYI"})
                logger.info(f"sender-rule subscribe: {source_ref} ({sender_email})")
                return None
            if action in ("fyi", "force-category"):
                override_cat = rule.get("category") or common.get("category") or ""
                override_imp = int(rule.get("importance") or 0) or importance
                fyi_common = {**common, "fyi": True, "action_type": "FYI",
                              "category": override_cat, "importance": override_imp}
                if not dry_run:
                    return ledger.upsert_loop(direction="owed_to_me", counterparty=counterparty,
                                              counterparty_email=counterparty_email,
                                              status="open", **fyi_common)
                logger.info(f"[dry-run] sender-rule {action}: {source_ref} ({sender_email})")
                return {"dry_run": True, "direction": "owed_to_me", "fyi": True,
                        "counterparty": counterparty, **common}
            # PRIORITY-1: importance override (action not exclude/fyi/subscribe)
            if rule.get("importance"):
                common["importance"] = int(rule["importance"])
        # ─────────────────────────────────────────────────────────────────────

        needs = (analysis.get("requires_reply") or analysis.get("requires_approval")
                 or analysis.get("requires_payment") or bool(analysis.get("action_items")))
        if not needs:
            return None
        direction, status = "i_owe", "open"
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

    # ── Dedup check (DEDUP-1) ────────────────────────────────────────────────
    dk = _dedup_key_for(channel, counterparty_email, summary_text)
    existing_dup = ledger.get_loop_by_dedup_key(dk) if not dry_run else None
    if existing_dup and existing_dup.get("source_ref") != source_ref:
        # Same sender + normalized subject — update last_activity only, no new loop
        logger.info(f"dedup: {source_ref} matches loop #{existing_dup.get('num')} "
                    f"({existing_dup.get('source_ref')}) — updating activity only")
        ledger.upsert_loop(direction=existing_dup["direction"],
                           counterparty=existing_dup["counterparty"],
                           summary=existing_dup["summary"],
                           channel=existing_dup["channel"],
                           source_ref=existing_dup["source_ref"],
                           fyi=bool(existing_dup.get("fyi")),
                           last_activity=last_iso, dedup_key=dk)
        return existing_dup
    common["dedup_key"] = dk
    # ─────────────────────────────────────────────────────────────────────────

    if dry_run:
        logger.info(f"[dry-run] would upsert {direction} {channel} loop for "
                    f"{source_ref} ({counterparty}): {common['summary']}")
        return {"dry_run": True, "direction": direction, "counterparty": counterparty,
                "status": status, **common}

    return ledger.upsert_loop(direction=direction, counterparty=counterparty,
                              counterparty_email=counterparty_email, status=status,
                              **common)


def expire_fyi_loops(*, max_age_hours: float = 24.0, dry_run: bool = False) -> int:
    """Auto-clear FYI/notification loops not acted on within max_age_hours. Acting on
    one (reply/archive) already resolves it via reconcile; this drops the rest so the
    FYI section stays a rolling 24h window, not an ever-growing pile."""
    import calendar
    expired = 0
    for loop in ledger.list_loops():  # open/waiting/snoozed only (done/dropped hidden)
        if not loop.get("fyi") or loop.get("status") != "open":
            continue
        fs = loop.get("first_seen")
        try:
            age_h = (time.time() - calendar.timegm(
                time.strptime(fs, "%Y-%m-%dT%H:%M:%SZ"))) / 3600 if fs else 0.0
        except ValueError:
            age_h = 0.0
        if age_h >= max_age_hours:
            if not dry_run:
                ledger.resolve_loop(loop["id"], "dropped", reason="fyi auto-expire 24h")
            expired += 1
    if expired:
        logger.info(f"FYI auto-expire: cleared {expired} loop(s) older than {max_age_hours}h")
    return expired


def reconcile(loops: list[dict], fetch_messages, *, is_done=None,
              dry_run: bool = False) -> dict:
    """Claude-free pass over existing loops. fetch_messages(source_ref) returns the
    thread's normalized messages. Resolves loops once the other side has the ball.

    is_done(source_ref) -> bool is an optional channel-specific "handled" signal
    (e.g. the Front conversation was archived). When it returns True the loop is
    resolved to done immediately — this is how action-only loops (approve in the
    bank portal, pay in BILL) get cleared: you archive the thread after acting.
    """
    active = [l for l in loops if l["status"] not in ("done", "dropped")]
    resolved = updated = errored = 0

    for loop in active:
        ref = loop["source_ref"]

        # Channel-specific "handled" signal (e.g. archived in Front) wins — and lets
        # us skip the message fetch entirely for already-cleared threads.
        if is_done is not None:
            try:
                if is_done(ref):
                    if not dry_run:
                        ledger.resolve_loop(loop["id"], "done")
                    resolved += 1
                    continue
            except Exception as exc:
                logger.debug(f"reconcile: is_done failed for {ref}: {exc}")

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
