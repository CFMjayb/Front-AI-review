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
    if (inbox := os.environ.get("INBOX_BISHOP_ID")):
        try:
            sources["bishop"] = front.list_inbox_conversations(inbox, status="open", since_ms=since_ms)
        except Exception as exc:
            logger.warning(f"digest: bishop fetch failed: {exc}")
            sources["bishop"] = []
    if (inbox := os.environ.get("INBOX_DIOCESE_ID")):
        try:
            sources["diocese"] = front.list_inbox_conversations(inbox, status="open", since_ms=since_ms)
        except Exception as exc:
            logger.warning(f"digest: diocese fetch failed: {exc}")
            sources["diocese"] = []
    if (inbox := os.environ.get("INBOX_AT_EPISCOPALMARYLAND_ID")):
        try:
            sources["@episcopalmaryland"] = front.list_inbox_conversations(inbox, status="open", since_ms=since_ms)
        except Exception as exc:
            logger.warning(f"digest: @episcopalmaryland fetch failed: {exc}")
            sources["@episcopalmaryland"] = []
    if (inbox := os.environ.get("PERSONAL_INBOX_ID")):
        try:
            sources["personal"] = front.list_inbox_conversations(inbox, status="open", since_ms=since_ms)
        except Exception as exc:
            logger.warning(f"digest: personal fetch failed: {exc}")
            sources["personal"] = []
    if (tm := os.environ.get("JAY_TEAMMATE_ID")):
        try:
            sources["assigned"] = front.list_assigned_conversations(tm, since_ms=since_ms)
        except Exception as exc:
            logger.warning(f"digest: assigned fetch failed: {exc}")
            sources["assigned"] = []
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

    full_doc = "\n".join([
        f"# EDOM Weekly Digest — {date_str}",
        "",
        f"_Generated {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started))} — cost ${res['cost_usd']:.4f}_",
        "",
        res["text"],
        "",
        "---",
        "",
        "## Raw stats",
        "",
        "```json",
        json.dumps(stats, indent=2),
        "```",
    ])

    filepath.write_text(full_doc, encoding="utf-8")
    logger.info(f"Digest written to {filepath} — cost ${res['cost_usd']:.4f}")
    return {"file": str(filepath), "stats": stats, "cost_usd": res["cost_usd"]}


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    run_digest()
