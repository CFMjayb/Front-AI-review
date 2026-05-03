"""M4 — Batch clustering. Runs after all individual conversations are processed.
Groups 2+ conversations on the same topic. Tags cluster/{slug} and writes [AI/M4] comment."""
import logging
import re

logger = logging.getLogger(__name__)

COMMENT_PREFIX = "[AI/M4]"

SYSTEM = """You group EDOM email conversations into thematic clusters.
A cluster groups conversations that are about the SAME topic, event, or initiative
(e.g., "Christmas Eve service planning", "Q3 budget approval thread").

Respond with JSON only:
{
  "clusters": [
    {
      "slug": "<short-kebab-case-slug>",
      "title": "<human-readable cluster title>",
      "rationale": "<one sentence on what binds these>",
      "memberConversationIds": ["<conv id>", ...]
    }
  ]
}

Only output a cluster when 2+ conversations clearly belong together. Conversations not
in any cluster are simply omitted from the output."""


def _slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:40]


def run(batch: list[dict], claude, front, dry_run: bool = False) -> dict:
    if not batch or len(batch) < 2:
        return {"ok": True, "output": {"clusters": []}, "cost_usd": 0.0, "writes": []}

    summaries = [
        {
            "id": r["conversation_id"],
            "subject": r.get("subject", ""),
            "tldr": (r.get("modules", {}).get("analyze", {}).get("output") or {}).get("tldr", ""),
            "category": (r.get("modules", {}).get("analyze", {}).get("output") or {}).get("category", ""),
        }
        for r in batch
    ]

    import json
    user_prompt = f"Conversations to cluster:\n\n{json.dumps(summaries, indent=2)}"

    res = claude.call(
        system=SYSTEM,
        user=user_prompt,
        model=claude.default_model,
        max_tokens=1500,
        json_mode=True,
        cached_system=True,
    )

    cost = res["cost_usd"]
    data = res["json"]

    if not data or not isinstance(data.get("clusters"), list):
        logger.warning(f"M4 invalid response: {res.get('parse_error')} | {res['text'][:200]}")
        return {"ok": False, "output": None, "cost_usd": cost,
                "writes": [], "error": res.get("parse_error") or "invalid response"}

    writes: list[dict] = []
    if not dry_run:
        for cluster in data["clusters"]:
            slug = cluster.get("slug") or _slugify(cluster.get("title", ""))
            tag_name = f"cluster/{slug}"
            ids = cluster.get("memberConversationIds") or []
            if len(ids) < 2:
                continue

            for conv_id in ids:
                try:
                    front.add_tag(conv_id, tag_name)
                    writes.append({"type": "tag", "name": tag_name, "conv": conv_id})
                except Exception as exc:
                    logger.warning(f"M4 tag failed for {conv_id}: {exc}")

            try:
                body_lines = [
                    f"{COMMENT_PREFIX} Cluster: {cluster.get('title', slug)}",
                    "",
                    cluster.get("rationale", ""),
                    "",
                    f"Members: {len(ids)} conversations",
                    *[f"  - {i}" for i in ids],
                ]
                front.add_comment(ids[0], "\n".join(body_lines))
                writes.append({"type": "comment", "prefix": COMMENT_PREFIX, "conv": ids[0]})
            except Exception as exc:
                logger.warning(f"M4 comment failed for {ids[0]}: {exc}")

    return {"ok": True, "output": data, "cost_usd": cost, "writes": writes, "error": None}
