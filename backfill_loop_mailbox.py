"""Stamp existing loops with the mailbox they came from (see cos/mailboxes.py).

Loops created before the mailbox split carry no attribution, so they would all
land in the "Unattributed" spreadsheet. This walks them and asks Front which
inbox each source conversation actually lives in — the authoritative answer, not
inferred from the recipient list.

Idempotent: a loop that already has a mailbox is skipped, so re-running is a
near-no-op and a partial run (rate limit, network) is safe to resume. Same
pattern as backfill_archive_front_resolved.py.

Scope: active + deferred loops — the ones the triage workbooks and the briefing
actually read. Resolved loops are opt-in (--include-resolved).

Usage:
    python backfill_loop_mailbox.py --dry-run     # report only, write nothing
    python backfill_loop_mailbox.py --remap-unassigned   # after adding a mailbox
    python backfill_loop_mailbox.py               # stamp unstamped loops
    python backfill_loop_mailbox.py --all         # re-resolve every loop,
                                                  # including already-stamped
    python backfill_loop_mailbox.py --include-resolved   # done/dropped too
"""
import argparse
import collections
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("LEDGER_BACKEND", os.environ.get("LEDGER_BACKEND", "firestore"))
os.environ.setdefault("GCP_PROJECT", os.environ.get("GCP_PROJECT", "cfm-front-mail"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from auth import get_front_api_token
from cos import ledger, mailboxes
from front_client import FrontClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--all", action="store_true",
                    help="re-resolve loops that already have a mailbox")
    ap.add_argument("--remap-unassigned", action="store_true",
                    help="re-resolve ONLY loops currently in the unassigned bucket. "
                         "Use after adding a mailbox to the registry: the loops that "
                         "belong to it are already stamped 'other', so plain --all "
                         "would needlessly re-check every other loop too")
    ap.add_argument("--include-resolved", action="store_true",
                    help="also stamp done/dropped loops (adds ~1,500 Front calls; "
                         "they never appear in a triage workbook or the briefing)")
    args = ap.parse_args()

    front = FrontClient(get_front_api_token())

    # Active + deferred only by default. Those are exactly the loops the triage
    # workbooks and the briefing read, so they are the ones that must be
    # attributed. Resolved loops are opt-in: stamping ~1,500 of them costs one
    # Front call each and changes nothing anyone looks at.
    inc = args.include_resolved
    loops  = ledger.list_loops(include_resolved=inc)
    loops += ledger.list_loops(include_resolved=inc, deferred_only=True)
    seen_ids: set[str] = set()
    unique = []
    for l in loops:
        if l["id"] not in seen_ids:
            seen_ids.add(l["id"])
            unique.append(l)

    def _wanted(l: dict) -> bool:
        if l.get("channel") != "front":
            return False
        if args.remap_unassigned:
            return (l.get("mailbox") or mailboxes.UNASSIGNED) == mailboxes.UNASSIGNED
        return args.all or not l.get("mailbox")

    todo = [l for l in unique if _wanted(l)]
    other_channel = [l for l in unique if l.get("channel") != "front"]

    print(f"{len(unique)} loops total / {len(todo)} to resolve"
          f" / {len(other_channel)} non-Front (left alone)")
    if args.dry_run:
        print("DRY RUN — no writes\n")

    counts: collections.Counter = collections.Counter()
    changed = errors = 0
    for i, loop in enumerate(todo, 1):
        conv_id = loop.get("source_ref")
        if not conv_id:
            counts["(no source_ref)"] += 1
            continue
        key = None
        for attempt in range(3):
            try:
                inbox_ids = [x.get("id") for x in front.list_conversation_inboxes(conv_id)]
                key = mailboxes.key_for_inboxes([x for x in inbox_ids if x])
                break
            except Exception as exc:
                # On a 429 the Front client has ALREADY slept Retry-After before
                # raising, so retrying immediately is the correct response — not
                # giving up, which would leave the loop unattributed. Anything
                # else (a 404 from a deleted conversation) fails after retries
                # and stays unstamped rather than being guessed at.
                if "rate limit" in str(exc).lower() and attempt < 2:
                    continue
                print(f"  ! {conv_id}: {exc}")
                errors += 1
                break
        if key is None:
            continue

        counts[key] += 1
        if loop.get("mailbox") == key:
            continue
        if not args.dry_run:
            ledger.patch_loop(loop["id"], mailbox=key)
        changed += 1

        if i % 50 == 0:
            print(f"  {i}/{len(todo)} … {changed} stamped", flush=True)
        time.sleep(0.05)   # be polite to Front's rate limiter

    print("\n=== Attribution ===")
    for key, n in counts.most_common():
        label = mailboxes.label_for(key) if mailboxes.by_key(key) else key
        addr = mailboxes.address_for(key) if mailboxes.by_key(key) else ""
        print(f"  {n:5d}  {key:10s} {label}" + (f"  <{addr}>" if addr else ""))
    print(f"\n{changed} loop(s) {'would be ' if args.dry_run else ''}stamped"
          f"{f', {errors} error(s)' if errors else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
