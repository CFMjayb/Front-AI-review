"""EDOM weekly digest — aggregates last 7 days and writes a markdown report."""
import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=True)

from auth import get_anthropic_api_key, get_front_api_token
from claude_client import ClaudeClient
from front_client import FrontClient

logger = logging.getLogger(__name__)

DIGEST_DIR = Path(__file__).parent / "data" / "digests"

SYSTEM = """You write a weekly email-operations digest for Jay Bentzen at the Episcopal Diocese of Maryland.
Tone: warm, direct, honest. Audience: Jay (busy diocesan operations lead).

Given aggregated stats from the past 7 days across his five Front sources, produce a
markdown digest with sections:
1. **Headline** — one-paragraph synthesis of the week.
2. **By the numbers** — small bulleted list (volume per inbox, urgent count, sentiment flags, draft count).
3. **What needs attention** — 3-5 specific bullets calling out unresolved items, escalating threads, or notable patterns.
4. **Top clusters** — list of M4 cluster titles with member counts.
5. **Closing note** — one sentence.

Output markdown only — no preamble, no JSON, no code fences."""


def _fetch_sources(front: FrontClient, since_ms: int) -> dict[str, list[dict]]:
    sources = {}

    for inbox_id in [i.strip() for i in os.environ.get("INBOX_IDS", "").split(",") if i.strip()]:
        try:
            sources[f"inbox:{inbox_id}"] = front.list_inbox_conversations(inbox_id, status="open", since_ms=since_ms)
        except Exception as exc:
            logger.warning(f"digest: inbox:{inbox_id} fetch failed: {exc}")
            sources[f"inbox:{inbox_id}"] = []

    for tm_id in [t.strip() for t in os.environ.get("TEAMMATE_IDS", "").split(",") if t.strip()]:
        try:
            sources[f"assigned:{tm_id}"] = front.list_assigned_conversations(tm_id, since_ms=since_ms)
        except Exception as exc:
            logger.warning(f"digest: assigned:{tm_id} fetch failed: {exc}")
            sources[f"assigned:{tm_id}"] = []

    return sources


def _aggregate_stats(source_convs: dict[str, list[dict]]) -> dict:
    stats: dict = {
        "by_source": {},
        "total_convs": 0,
        "by_category": {},
        "by_urgency": {},
        "by_sentiment": {},
        "clusters": {},
    }
    for source, convs in source_convs.items():
        stats["by_source"][source] = len(convs)
        stats["total_convs"] += len(convs)
        for c in convs:
            for tag in (c.get("tags") or []):
                name = tag.get("name") or ""
                if name.startswith("AI/"):
                    cat = name[3:]
                    stats["by_category"][cat] = stats["by_category"].get(cat, 0) + 1
                elif name.startswith("urgency/"):
                    u = name[8:]
                    stats["by_urgency"][u] = stats["by_urgency"].get(u, 0) + 1
                elif name.startswith("sentiment/"):
                    s = name[10:]
                    stats["by_sentiment"][s] = stats["by_sentiment"].get(s, 0) + 1
                elif name.startswith("cluster/"):
                    slug = name[8:]
                    stats["clusters"][slug] = stats["clusters"].get(slug, 0) + 1
    return stats


def run_digest() -> dict:
    started = time.time()
    since_ms = int(started * 1000) - 7 * 24 * 3600 * 1000
    logger.info(f"Digest run starting — 7-day window from {time.strftime('%Y-%m-%d', time.gmtime(since_ms / 1000))}")

    front = FrontClient(get_front_api_token())
    claude = ClaudeClient(
        api_key=get_anthropic_api_key(),
        default_model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7"),
        fast_model=os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5"),
    )

    source_convs = _fetch_sources(front, since_ms)
    stats = _aggregate_stats(source_convs)

    # Detect category corrections in already-processed conversations
    corrections: list[dict] = []
    try:
        from modules import corrections as corr_mod
        all_convs = [c for convs in source_convs.values() for c in convs]
        processed_convs = [
            c for c in all_convs
            if any(t.get("name") == "AI/processed" for t in (c.get("tags") or []))
        ]
        # Dedupe by id
        seen: set = set()
        unique_processed = []
        for c in processed_convs:
            if c.get("id") not in seen:
                seen.add(c["id"])
                unique_processed.append(c)
        corrections = corr_mod.detect_corrections(unique_processed, front)
        if corrections:
            logger.info(f"Digest: {len(corrections)} correction(s) found this week")
    except Exception as exc:
        logger.warning(f"digest: corrections detection failed: {exc}")

    res = claude.call(
        system=SYSTEM,
        user=f"Stats for the past 7 days:\n\n{json.dumps(stats, indent=2)}",
        model=claude.default_model,
        max_tokens=2000,
        json_mode=False,
        cached_system=True,
    )

    date_str = time.strftime("%Y-%m-%d", time.gmtime(started))
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    filepath = DIGEST_DIR / f"{date_str}.md"

    corrections_section = corr_mod.format_digest_section(corrections) if corrections else ""

    sections = [
        f"# EDOM Weekly Digest — {date_str}",
        "",
        f"_Generated {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started))} — cost ${res['cost_usd']:.4f}_",
        "",
        res["text"],
    ]
    if corrections_section:
        sections += ["", "---", "", corrections_section]
    sections += [
        "",
        "---",
        "",
        "## Raw stats",
        "",
        "```json",
        json.dumps(stats, indent=2),
        "```",
    ]
    full_doc = "\n".join(sections)

    filepath.write_text(full_doc, encoding="utf-8")
    logger.info(f"Digest written to {filepath} — cost ${res['cost_usd']:.4f}")
    return {"file": str(filepath), "stats": stats, "cost_usd": res["cost_usd"]}


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    run_digest()
