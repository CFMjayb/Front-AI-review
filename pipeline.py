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
from front_client import FrontClient, PROCESSED_TAG
from modules import analyze, m4_cluster, m8_draft

logger = logging.getLogger(__name__)

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
            tm_id, since_ms=since_ms, max_pages=max_pages)))

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


def _filter_unprocessed(conversations: list[dict]) -> tuple[list[dict], int]:
    """Use tags already embedded in list response — no extra API calls."""
    todo: list[dict] = []
    skipped = 0
    for c in conversations:
        tag_names = {t.get("name") for t in (c.get("tags") or [])}
        if PROCESSED_TAG in tag_names:
            skipped += 1
        else:
            todo.append(c)
    logger.info(f"Gate: {len(conversations)} total / {skipped} already processed / {len(todo)} to do")
    return todo, skipped


def _process_one(conv: dict, front: FrontClient, claude: ClaudeClient, dry_run: bool) -> dict:
    cid = conv["id"]
    started = time.time()
    cost = 0.0
    errored = False
    module_results: dict = {}

    try:
        messages = front.get_conversation_messages(cid)
        transcript = front.messages_to_transcript(messages)
        ctx = {"conv": conv, "messages": messages, "transcript": transcript, "dry_run": dry_run}

        # Single consolidated analysis (replaces M1-M7)
        result = analyze.run(ctx, claude, front)
        module_results["analyze"] = result
        cost += result.get("cost_usd", 0)

        # M8 draft — only when reply required and urgency is urgent or high
        if result["ok"]:
            out = result["output"] or {}
            if out.get("requires_reply") and out.get("urgency") in ("urgent", "high"):
                ctx["analyze"] = result
                m8 = m8_draft.run(ctx, claude, front)
                module_results["m8"] = m8
                cost += m8.get("cost_usd", 0)

        # Apply processed tag only after all writes succeed
        if not dry_run and result["ok"]:
            front.add_tag(cid, PROCESSED_TAG)
        elif dry_run:
            logger.info(f"[dry-run] would apply {PROCESSED_TAG} to {cid}")

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
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "7"))
    if since_ms is None:
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
    processed_results = [r for r in results if not r["errored"]]
    if len(processed_results) >= 2:
        logger.info(f"Running M4 cluster over {len(processed_results)} conversations")
        try:
            m4_result = m4_cluster.run(processed_results, claude, front, dry_run=dry_run)
            total_cost += m4_result.get("cost_usd", 0)
            logger.info(f"M4 complete: {len((m4_result.get('output') or {}).get('clusters', []))} clusters found")
        except Exception as exc:
            logger.error(f"M4 cluster failed: {exc}")

    logger.info(f"Pipeline complete: processed={sum(1 for r in results if not r['errored'])} "
                f"errored={sum(1 for r in results if r['errored'])} skipped={skipped} "
                f"cost=${total_cost:.4f}")

    return {"results": results, "m4": m4_result, "total_cost_usd": total_cost, "skipped": skipped}
