"""EDOM pipeline orchestrator.

Cost-control rule: every conversation gets exactly ONE AI review. After all modules complete,
the pipeline tags the conversation with AI/processed. Subsequent runs check that tag
FIRST and skip already-processed conversations before any Claude call.
"""
import logging
import os
import time
from typing import Optional

from auth import get_anthropic_api_key, get_front_api_token
from claude_client import ClaudeClient
from cos import front_extract
from front_client import FrontClient, PROCESSED_TAG
from modules import analyze, m4_cluster, m8_draft, plaud_extract, prefilter

logger = logging.getLogger(__name__)

# Front has no literal "open" status — an open conversation is assigned OR
# unassigned (as opposed to archived / deleted / trashed / spam).
_OPEN_STATUSES = {"open", "assigned", "unassigned"}

REQUIRED_TAGS: list[tuple[str, Optional[str]]] = [
    (PROCESSED_TAG, "blue"),
    *((f"AI/{c}", None) for c in analyze.CATEGORIES),
    *((f"urgency/{l}", None) for l in analyze.URGENCY_LEVELS),
    *((f"sentiment/{v}", None) for v in analyze.SENTIMENT_VALUES),
]


def _build_clients() -> tuple[FrontClient, ClaudeClient]:
    front = FrontClient(get_front_api_token())
    claude = ClaudeClient(
        api_key=get_anthropic_api_key(),
        default_model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7"),
        fast_model=os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5"),
    )
    return front, claude


def _ensure_required_tags(front: FrontClient) -> None:
    for name, highlight in REQUIRED_TAGS:
        front.ensure_tag(name, highlight=highlight)


def _fetch_all_sources(front: FrontClient, since_ms: int) -> list[dict]:
    sources: list[tuple[str, list[dict]]] = []
    max_pages = int(os.environ.get("SOURCE_MAX_PAGES", "5"))

    for inbox_id in [i.strip() for i in os.environ.get("INBOX_IDS", "").split(",") if i.strip()]:
        sources.append((f"inbox:{inbox_id}", front.list_inbox_conversations(
            inbox_id, status="open", since_ms=since_ms, max_pages=max_pages)))

    for tm_id in [t.strip() for t in os.environ.get("TEAMMATE_IDS", "").split(",") if t.strip()]:
        sources.append((f"assigned:{tm_id}", front.list_assigned_conversations(
            tm_id, status="open", since_ms=since_ms, max_pages=max_pages)))

    all_convs: list[dict] = []
    for name, convs in sources:
        logger.info(f"Source {name}: {len(convs)} conversations")
        all_convs.extend(convs)
    return all_convs


def _dedupe_by_id(conversations: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for c in conversations:
        cid = c.get("id")
        if cid and cid not in seen:
            seen[cid] = c
    return list(seen.values())


def _filter_open(conversations: list[dict]) -> tuple[list[dict], int]:
    """Drop conversations that are not open (archived, deleted, spam, etc.)."""
    active: list[dict] = []
    skipped = 0
    for c in conversations:
        # No "or open" default — missing/null status is treated as unknown and
        # filtered out conservatively rather than assumed open.
        if c.get("status") in _OPEN_STATUSES:
            active.append(c)
        else:
            skipped += 1
    if skipped:
        logger.info(f"Status filter: dropped {skipped} non-open conversation(s)")
    return active, skipped


def _filter_by_date(conversations: list[dict], since_ms: int) -> tuple[list[dict], int]:
    """Drop conversations created before since_ms. Front API q[after] is unreliable."""
    since_s = since_ms // 1000
    in_window: list[dict] = []
    before_cutoff = 0
    for c in conversations:
        if (c.get("created_at") or 0) >= since_s:
            in_window.append(c)
        else:
            before_cutoff += 1
    logger.info(f"Date filter: {len(in_window)} on/after cutoff, {before_cutoff} before cutoff dropped")
    return in_window, before_cutoff


def _filter_unprocessed(conversations: list[dict]) -> tuple[list[dict], list[dict]]:
    """Use tags already embedded in list response — no extra API calls."""
    todo: list[dict] = []
    skipped: list[dict] = []
    for c in conversations:
        tag_names = {t.get("name") for t in (c.get("tags") or [])}
        if PROCESSED_TAG in tag_names:
            skipped.append(c)
        else:
            todo.append(c)
    logger.info(f"Gate: {len(conversations)} total / {len(skipped)} already processed / {len(todo)} to do")
    return todo, skipped


def _process_one(conv: dict, front: FrontClient, claude: ClaudeClient, dry_run: bool) -> dict:
    cid = conv["id"]
    started = time.time()
    cost = 0.0
    errored = False
    module_results: dict = {}

    # Defense-in-depth: skip any conversation that is not currently open.
    # _filter_open handles the bulk-fetch path; this guard covers single-
    # conversation mode and the race window between fetch and process.
    conv_status = conv.get("status") or ""
    if conv_status not in _OPEN_STATUSES:
        logger.info(f"[skip] {cid} status={conv_status!r} — not open, no AI review")
        return {
            "conversation_id": cid, "subject": conv.get("subject"),
            "duration_s": 0.0, "cost_usd": 0.0,
            "errored": False, "prefiltered": True,
            "modules": {"skip_reason": f"not_open:{conv_status}"},
        }

    try:
        messages = front.get_conversation_messages(cid)
        transcript = front.messages_to_transcript(messages)

        # ── Atlantic Union Positive Pay ───────────────────────────────────────
        is_pp, has_exceptions = prefilter.is_positive_pay(conv, messages)
        if is_pp:
            if not has_exceptions:
                # "No exceptions today" — archive silently, no loop, no AI cost
                if not dry_run:
                    front.add_tag(cid, "AI/positive-pay-clear")
                    front.add_tag(cid, PROCESSED_TAG)
                    front.set_status(cid, "archived")
                logger.info(f"[positive-pay] {cid} — no exceptions, archived")
                module_results["positive_pay"] = {"exceptions": False}
                return {
                    "conversation_id": cid, "subject": conv.get("subject"),
                    "duration_s": time.time() - started, "cost_usd": 0.0,
                    "errored": False, "prefiltered": True,
                    "modules": module_results,
                }
            else:
                # Exceptions present — create urgent loop + SMS notification
                logger.warning(f"[positive-pay] {cid} — EXCEPTIONS FOUND, notifying")
                if not dry_run:
                    from cos import ledger as _ledger, notifier as _notifier
                    _ledger.upsert_loop(
                        direction="i_owe",
                        counterparty="Atlantic Union Bank",
                        summary="POSITIVE PAY — exceptions require your decision",
                        channel="front",
                        source_ref=cid,
                        source_link=front_extract.front_source_link(cid),
                        category="banking",
                        importance=5,
                        fyi=False,
                    )
                    front.add_tag(cid, "AI/positive-pay-exceptions")
                    front.add_tag(cid, PROCESSED_TAG)
                    _notifier.send_sms(
                        f"POSITIVE PAY ALERT: Exceptions require your decision. "
                        f"Open Front: https://app.frontapp.com/open/{cid}"
                    )
                module_results["positive_pay"] = {"exceptions": True}
                return {
                    "conversation_id": cid, "subject": conv.get("subject"),
                    "duration_s": time.time() - started, "cost_usd": 0.0,
                    "errored": False, "prefiltered": True,
                    "modules": module_results,
                }

        # ── Plaud.ai meeting notes ────────────────────────────────────────────
        if plaud_extract.is_plaud_email(conv, messages):
            logger.info(f"[plaud] {cid} — extracting action items")
            try:
                action_items, plaud_cost = plaud_extract.extract_action_items(conv, messages, claude, front)
                cost += plaud_cost
            except Exception as exc:
                logger.error(f"[plaud] {cid} extraction failed: {exc}")
                return {
                    "conversation_id": cid, "subject": conv.get("subject"),
                    "duration_s": time.time() - started, "cost_usd": cost,
                    "errored": True, "modules": {"plaud_error": str(exc)},
                }

            # Tag immediately after the paid extraction succeeds — BEFORE loop
            # creation — so a downstream Firestore failure can't leave the
            # conversation untagged and get it re-billed on every pipeline run.
            if not dry_run:
                front.add_tag(cid, "AI/meeting-notes")
                front.add_tag(cid, PROCESSED_TAG)
            else:
                logger.info(f"[dry-run] would apply {PROCESSED_TAG} to {cid}")

            try:
                from cos import ledger as _ledger
                loops = plaud_extract.create_loops(
                    conv, messages, action_items, _ledger,
                    front_extract.front_source_link, dry_run=dry_run)
                module_results["plaud"] = {"action_items": len(action_items), "loops": len(loops)}
                logger.info(f"[plaud] {cid} — {len(loops)} loop(s) created")
            except Exception as exc:
                logger.error(f"[plaud] {cid} loop creation failed: {exc}")
                module_results["plaud_loop_error"] = str(exc)

            return {
                "conversation_id": cid, "subject": conv.get("subject"),
                "duration_s": time.time() - started,
                "cost_usd": cost,
                "errored": "plaud_loop_error" in module_results,
                "modules": module_results,
            }

        # ── Sender-rule pre-filter (skip Claude for known exclude/fyi senders) ──
        # cos/extract.py's loop_from_thread() applies sender_rules AFTER analysis,
        # which never actually saves the Claude cost — this checks the same rule
        # BEFORE paying for a review, for senders explicitly marked exclude/fyi.
        skip_sr, sr_rule, sr_sender = prefilter.sender_rule_skip(conv, messages)
        if skip_sr:
            sr_action = sr_rule.get("action")
            if not dry_run:
                if sr_action == "fyi":
                    inbound = [m for m in messages if m.get("is_inbound")]
                    latest = max(inbound, key=lambda m: m.get("created_at") or 0)
                    author = latest.get("author") or {}
                    from cos import ledger as _ledger
                    _ledger.upsert_loop(
                        direction="owed_to_me",
                        counterparty=author.get("name") or sr_sender,
                        counterparty_email=sr_sender,
                        summary=conv.get("subject") or "(no subject)",
                        channel="front", source_ref=cid,
                        source_link=front_extract.front_source_link(cid),
                        category=sr_rule.get("category") or "",
                        importance=int(sr_rule.get("importance") or 0) or 2,
                        fyi=True, status="open", action_type="FYI",
                    )
                front.add_tag(cid, f"AI/sender-rule-{sr_action}")
                front.add_tag(cid, PROCESSED_TAG)
            else:
                logger.info(f"[dry-run] sender-rule {sr_action}: would skip Claude for {cid} ({sr_sender})")
            logger.info(f"[sender-rule] {cid} skipped AI review — {sr_action} ({sr_sender})")
            return {
                "conversation_id": cid, "subject": conv.get("subject"),
                "duration_s": time.time() - started, "cost_usd": 0.0,
                "errored": False, "prefiltered": True,
                "modules": {"sender_rule": {"action": sr_action, "sender": sr_sender}},
            }

        # ── Standard pre-filter (bulk / calendar) ────────────────────────────
        # Cheap, AI-free: skip Claude review for marketing/bounce mail and
        # calendar meeting-response notifications (Accepted/Declined/Tentative).
        is_bulk, reason = prefilter.looks_like_bulk(conv, messages)
        is_cal = prefilter.is_calendar_response(conv) if not is_bulk else False
        if is_bulk or is_cal:
            skip_reason = reason if is_bulk else "calendar meeting response (no AI needed)"
            if not dry_run:
                if is_bulk:
                    front.add_tag(cid, "AI/spam")  # calendar responses aren't spam
                    front.set_status(cid, "archived")
                front.add_tag(cid, PROCESSED_TAG)
            else:
                logger.info(f"[dry-run] [prefilter] would skip {cid}: {skip_reason}")
            logger.info(f"[prefilter] {cid} skipped AI review — {skip_reason}")
            module_results["prefilter"] = {"skipped": True, "reason": skip_reason}
            return {
                "conversation_id": cid,
                "subject": conv.get("subject"),
                "duration_s": time.time() - started,
                "cost_usd": 0.0,
                "errored": False,
                "prefiltered": True,
                "modules": module_results,
            }

        ctx = {"conv": conv, "messages": messages, "transcript": transcript, "dry_run": dry_run}

        # Single consolidated analysis (replaces M1-M7)
        result = analyze.run(ctx, claude, front)
        module_results["analyze"] = result
        cost += result.get("cost_usd", 0)

        # Apply the gate tag immediately after a successful analysis — BEFORE
        # loop extraction and M8 so a 429 on any later write cannot leave the
        # conversation untagged and get it re-processed on every pipeline run.
        # Retry once on 429: the client already sleeps Retry-After, so a second
        # attempt after that sleep almost always succeeds.
        if not dry_run and result["ok"]:
            for _attempt in range(2):
                try:
                    front.add_tag(cid, PROCESSED_TAG)
                    break
                except Exception as tag_exc:
                    if _attempt == 0:
                        logger.warning(f"{cid} PROCESSED_TAG attempt 1 failed ({tag_exc}), retrying")
                    else:
                        logger.error(f"{cid} PROCESSED_TAG could not be applied after retry: {tag_exc}")
                        raise  # propagate so the conversation is counted as errored
        elif dry_run:
            logger.info(f"[dry-run] would apply {PROCESSED_TAG} to {cid}")

        # CoS open-loop extraction — reuses the analysis above, no extra Claude cost
        if result["ok"]:
            try:
                loop = front_extract.extract_from_analysis(
                    conv, messages, result["output"], dry_run=dry_run)
                if loop:
                    module_results["loop"] = loop
            except Exception as exc:
                logger.warning(f"Loop extraction failed for {cid}: {exc}")

        # M8 draft — only when reply required and urgency is urgent or high
        if result["ok"]:
            out = result["output"] or {}
            if out.get("requires_reply") and out.get("urgency") in ("urgent", "high"):
                ctx["analyze"] = result
                m8 = m8_draft.run(ctx, claude, front)
                module_results["m8"] = m8
                cost += m8.get("cost_usd", 0)
            if (result.get("output") or {}).get("category") == "spam":
                front.set_status(cid, "archived")

    except Exception as exc:
        errored = True
        logger.error(f"Conversation {cid} failed: {exc}")
        module_results["error"] = str(exc)

    return {
        "conversation_id": cid,
        "subject": conv.get("subject"),
        "duration_s": time.time() - started,
        "cost_usd": cost,
        "errored": errored,
        "modules": module_results,
    }


def run_pipeline(*, conversation_id: Optional[str] = None, dry_run: Optional[bool] = None,
                 since_ms: Optional[int] = None) -> dict:
    if dry_run is None:
        dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    if since_ms is None:
        earliest_date = os.environ.get("EARLIEST_DATE", "")
        if earliest_date:
            import datetime
            since_ms = int(datetime.datetime.strptime(earliest_date, "%Y-%m-%d")
                          .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
            logger.info(f"EARLIEST_DATE={earliest_date} → since_ms={since_ms}")
        else:
            lookback_days = int(os.environ.get("LOOKBACK_DAYS", "7"))
            since_ms = int(time.time() * 1000) - lookback_days * 24 * 3600 * 1000
    max_cost = float(os.environ.get("MAX_RUN_COST_USD", "10"))

    front, claude = _build_clients()
    if not dry_run:
        _ensure_required_tags(front)

    if conversation_id:
        conversations = [front.get_conversation(conversation_id)]
        logger.info(f"single-conversation mode: {conversation_id}")
    else:
        conversations = _fetch_all_sources(front, since_ms)

    unique = _dedupe_by_id(conversations)
    unique, _ = _filter_open(unique)
    unique, _ = _filter_by_date(unique, since_ms)
    todo, skipped = _filter_unprocessed(unique)

    results: list[dict] = []
    total_cost = 0.0
    for conv in todo:
        if total_cost >= max_cost:
            logger.warning(f"Cost ceiling ${max_cost} hit at ${total_cost:.4f}, stopping")
            break
        r = _process_one(conv, front, claude, dry_run)
        results.append(r)
        total_cost += r["cost_usd"]
        logger.info(f"{conv['id']} done ${r['cost_usd']:.4f} (cum ${total_cost:.4f})"
                    f"{' ERRORED' if r['errored'] else ''}")

    # M4 cluster — runs over the full batch after individual conversations
    m4_result = None
    processed_results = [r for r in results if not r["errored"] and not r.get("prefiltered")]
    if len(processed_results) >= 2:
        logger.info(f"Running M4 cluster over {len(processed_results)} conversations")
        try:
            m4_result = m4_cluster.run(processed_results, claude, front, dry_run=dry_run)
            total_cost += m4_result.get("cost_usd", 0)
            logger.info(f"M4 complete: {len((m4_result.get('output') or {}).get('clusters', []))} clusters found")
        except Exception as exc:
            logger.error(f"M4 cluster failed: {exc}")

    # Corrections scan — detect category changes Jay made on already-processed convs
    if skipped and not dry_run:
        try:
            from modules import corrections as corr_mod
            found = corr_mod.detect_corrections(skipped, front)
            corr_mod.log_corrections(found)
            corr_mod.apply_corrections(found)
        except Exception as exc:
            logger.warning(f"Corrections scan failed: {exc}")

    # CoS loop reconcile — Claude-free pass that closes/refreshes existing loops
    loop_reconcile = None
    try:
        loop_reconcile = front_extract.reconcile_open_front_loops(front, dry_run=dry_run)
    except Exception as exc:
        logger.warning(f"Loop reconcile failed: {exc}")

    # Auto-clear FYI/notification loops not acted on within 24h.
    try:
        from cos import extract as cos_extract
        cos_extract.expire_fyi_loops(dry_run=dry_run)
    except Exception as exc:
        logger.warning(f"FYI auto-expire failed: {exc}")

    prefiltered = sum(1 for r in results if r.get("prefiltered"))
    analyzed = sum(1 for r in results if not r["errored"] and not r.get("prefiltered"))
    logger.info(f"Pipeline complete: analyzed={analyzed} prefiltered(spam, no AI)={prefiltered} "
                f"errored={sum(1 for r in results if r['errored'])} skipped={len(skipped)} "
                f"cost=${total_cost:.4f}")

    return {"results": results, "m4": m4_result, "total_cost_usd": total_cost,
            "skipped": len(skipped), "prefiltered": prefiltered,
            "loop_reconcile": loop_reconcile}
