"""Self-healing sweep: archive in Front every conversation whose loop is closed.

Every path that closes a Front loop calls ledger.resolve_loop, which now marks it
front_archived=False (+ resolved_at). Paths that archive immediately (the triage
upload) flip it to True right after; the rest (reply-detected reconcile, FYI
24h expiry, the cos_resolve_loop tool, an archive attempt that hit a 429) are
picked up here on the next pipeline run, so no closed loop can stay open in Front.

Rules (Jay, 2026-10-02): archive them all, EXCEPT leave open — and flag — any
conversation where the sender wrote after the loop was closed. Held loops get an
`archive_hold` timestamp so they are not re-checked every 30 minutes.

Only runs on the Firestore backend. The query is a single-field equality
(front_archived == False), so each run reads just the pending loops, not the
whole ledger.
"""
import datetime
import logging
import os
from typing import Any, Callable, Iterable, Optional

from cos import front_archive
from cos.front_archive import ARCHIVE, HOLD, STAMP

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 100


def sweep(front: Any, loops: Iterable[dict], *, stamp: Callable[[str], None],
          hold: Callable[[str], None], limit: int = DEFAULT_LIMIT,
          dry_run: bool = False, printer=None) -> dict:
    """Classify and act on pending closed loops. Returns counts."""
    say = printer or (lambda m: logger.info(m))
    counts = {"checked": 0, "archived": 0, "stamped": 0, "held": 0, "errors": 0}
    for loop in list(loops)[:limit]:
        src = loop.get("source_ref")
        if not src:
            continue
        counts["checked"] += 1
        label = f"#{loop.get('num')}"
        try:
            try:
                conv = front_archive.call_with_429_retry(front.get_conversation, src)
            except Exception as exc:
                if getattr(exc, "status", None) == 404:
                    conv = None
                else:
                    raise
            resolved_at = loop.get("resolved_at") or ""
            verdict = front_archive.classify_for_archive(conv, resolved_at)
            if verdict == ARCHIVE and not resolved_at:
                verdict = HOLD            # cannot prove nothing new arrived
            if verdict == HOLD:
                counts["held"] += 1
                say(f"    sweep: {label} HELD — sender wrote after the loop was closed")
                if not dry_run:
                    hold(loop["id"])
            elif verdict == STAMP:
                counts["stamped"] += 1
                if not dry_run:
                    stamp(loop["id"])
            else:
                if not dry_run:
                    front_archive.call_with_429_retry(front.set_status, src, "archived")
                    stamp(loop["id"])
                counts["archived"] += 1
                say(f"    sweep: {label} archived in Front")
        except Exception as exc:
            counts["errors"] += 1
            say(f"    sweep WARNING: {label}: {exc}")
    return counts


def pending_loops(db) -> list[dict]:
    """Closed Front loops marked pending (front_archived == False), minus holds."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    out = []
    q = db.collection("loops").where(filter=FieldFilter("front_archived", "==", False))
    for s in q.stream():
        d = s.to_dict()
        if (d.get("channel") == "front" and d.get("status") in ("done", "dropped")
                and not d.get("archive_hold")):
            d["id"] = s.id
            out.append(d)
    out.sort(key=lambda l: l.get("resolved_at") or "")
    return out


def run(front: Any, *, dry_run: bool = False, limit: Optional[int] = None) -> dict:
    """Pipeline entry point. No-op unless the Firestore ledger is in use."""
    if os.environ.get("ARCHIVE_SWEEP_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return {"disabled": True}
    if os.environ.get("LEDGER_BACKEND", "sqlite").lower() != "firestore":
        return {"skipped": "non-firestore ledger"}
    from cos import ledger_firestore as lf
    db = lf._db()
    now = lambda: datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pending = pending_loops(db)
    if not pending:
        return {"checked": 0, "archived": 0, "stamped": 0, "held": 0, "errors": 0}
    cap = limit or int(os.environ.get("ARCHIVE_SWEEP_LIMIT", DEFAULT_LIMIT))
    counts = sweep(
        front, pending, limit=cap, dry_run=dry_run,
        # Direct updates, not patch_loop: patch_loop bumps last_reviewed.
        stamp=lambda lid: db.collection("loops").document(lid).update({"front_archived": True}),
        hold=lambda lid: db.collection("loops").document(lid).update({"archive_hold": now()}),
    )
    counts["pending"] = len(pending)
    logger.info(f"Archive sweep: {counts}")
    return counts
