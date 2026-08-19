"""One-off: archive Front conversations for loops this session already dropped
without archiving them (retire_plaud_loops.py + retire_excluded_senders.py,
before front_archive.py existed). Every loop dropped for one of these two
reasons and not yet stamped front_archived.

Idempotent — front_archive.archive_conversation() no-ops on an already-archived
conversation, and this only ever touches front_archived=False rows.

Usage:
    python archive_already_dropped.py            # report only
    python archive_already_dropped.py --live
"""
import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("LEDGER_BACKEND", os.environ.get("LEDGER_BACKEND", "firestore"))
os.environ.setdefault("GCP_PROJECT", os.environ.get("GCP_PROJECT", "cfm-front-mail"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from auth import get_front_api_token
from cos import front_archive, ledger
from front_client import FrontClient

REASONS = {"plaud-removed-2026-08-18", "sender-excluded"}


def _real_conv_id(source_ref: str) -> str:
    return (source_ref or "").split("::action::")[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    # Scoped precisely via the feedback log's own `reason` field — NOT "every
    # dropped loop that isn't archived", which would sweep in years of ordinary
    # triage history unrelated to today's incident. The feedback log records
    # exactly which loop_ids were dropped for these two reasons.
    feedback = ledger.list_feedback(action="dropped", limit=2000)
    target_ids = {f["loop_id"] for f in feedback
                  if f.get("reason") in REASONS and f.get("loop_id")}
    print(f"{len(target_ids)} loop(s) in the feedback log dropped for {sorted(REASONS)}")

    candidates = [l for l in ledger.list_loops(include_resolved=True)
                  if l["id"] in target_ids and l.get("channel") == "front"
                  and not l.get("front_archived")]
    print(f"{len(candidates)} of those still need archiving\n")

    if not args.live:
        for l in candidates[:8]:
            print(f"  #{l.get('num')}  {(l.get('summary') or '')[:70]}")
        print("\nReport only — nothing written. Re-run with --live to archive.")
        return 0

    front = FrontClient(get_front_api_token())
    seen: set[str] = set()
    archived = 0
    for i, l in enumerate(candidates, 1):
        conv_id = _real_conv_id(l.get("source_ref", ""))
        if not conv_id or conv_id in seen:
            if conv_id:
                ledger.patch_loop(l["id"], front_archived=True)
            continue
        ok = front_archive.archive_conversation(front, conv_id, label=f"#{l.get('num')}",
                                                 printer=print)
        seen.add(conv_id)
        if ok:
            ledger.patch_loop(l["id"], front_archived=True)
            archived += 1
        if i % 50 == 0:
            print(f"  {i}/{len(candidates)}…", flush=True)

    print(f"\n{archived} Front conversation(s) archived across {len(candidates)} loop(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
