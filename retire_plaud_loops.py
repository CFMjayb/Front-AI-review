"""Retire the Plaud.ai meeting-action loops from the triage tool.

Jay, 2026-08-18: "let's remove all plaud.ai from this tool for now" — one loop per
extracted action item swamped the sheet (309 of 687 loops) and many were unusable
(64 assigned to "Speaker 1/2/3/4" where the recording never identified anyone).

Sets status='dropped' with a reason, which is the tool's own "remove" semantics:
the rows leave every workbook and the briefing, the ledger row survives, and the
resolution is recorded in the feedback log like any other triage decision. It is
reversible — see --restore.

Also archives the underlying Front conversation for each loop — a loop coming
off the triage list must not leave its email sitting open and unread in the
mailbox. (A first version of this script only touched the ledger; caught by
Jay same day: "how are you managing items in Front... it does not appear the
emails are getting resolved, just taken off the list." Fixed before it shipped
further.)

Ingestion is stopped separately by PLAUD_ENABLED in pipeline.py.

Usage:
    python retire_plaud_loops.py                # report only (default)
    python retire_plaud_loops.py --live         # drop them
    python retire_plaud_loops.py --restore      # report what would come back
    python retire_plaud_loops.py --restore --live
"""
import argparse
import collections
import os
import sys
from pathlib import Path

os.environ.setdefault("LEDGER_BACKEND", os.environ.get("LEDGER_BACKEND", "firestore"))
os.environ.setdefault("GCP_PROJECT", os.environ.get("GCP_PROJECT", "cfm-front-mail"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from auth import get_front_api_token
from cos import ledger
from front_client import FrontClient

CATEGORY = "meeting-action"
REASON = "plaud-removed-2026-08-18"


def _real_conv_id(source_ref: str) -> str:
    """Plaud loops use a synthetic sub-ref, "<conv_id>::action::N" (one loop per
    extracted action item) — Front only knows the part before "::action::"."""
    return (source_ref or "").split("::action::")[0]


def is_plaud(loop: dict) -> bool:
    """Two independent signals, both set by modules/plaud_extract.py. Requiring
    only one would be looser than necessary; requiring both would miss a loop
    whose category was edited by hand. Either is enough."""
    return ((loop.get("category") or "") == CATEGORY
            or "::action::" in (loop.get("source_ref") or ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="write; default is report-only")
    ap.add_argument("--restore", action="store_true",
                    help="reopen previously retired loops instead of dropping")
    args = ap.parse_args()

    if args.restore:
        candidates = [l for l in ledger.list_loops(include_resolved=True)
                      if is_plaud(l) and l.get("status") == "dropped"]
        verb = "restore"
    else:
        candidates = [l for l in ledger.list_loops() if is_plaud(l)]
        verb = "drop"

    print(f"{len(candidates)} Plaud loop(s) to {verb}  "
          f"({'LIVE' if args.live else 'REPORT ONLY'})\n")
    if not candidates:
        print("Nothing to do.")
        return 0

    by_mb = collections.Counter(l.get("mailbox") or "(unstamped)" for l in candidates)
    print("by mailbox:")
    for k, n in by_mb.most_common():
        print(f"  {n:5d}  {k}")

    by_assignee = collections.Counter(l.get("counterparty") or "?" for l in candidates)
    print("\ntop assignees:")
    for k, n in by_assignee.most_common(8):
        print(f"  {n:5d}  {k}")

    print("\nexamples:")
    for l in candidates[:5]:
        print(f"  #{l.get('num')}  {(l.get('summary') or '')[:78]}")

    if not args.live:
        print(f"\nReport only — nothing written. Re-run with --live to {verb}.")
        return 0

    front = FrontClient(get_front_api_token()) if not args.restore else None
    archived_convs: set[str] = set()   # dedupe — several loops share one meeting

    n = 0
    for l in candidates:
        if args.restore:
            ledger.resolve_loop(l["id"], "open")
        else:
            from cos import front_archive
            conv_id = _real_conv_id(l.get("source_ref", ""))
            if conv_id and conv_id not in archived_convs:
                front_archive.archive_conversation(
                    front, conv_id, label=f"#{l.get('num')}", printer=print)
                archived_convs.add(conv_id)
            ledger.resolve_loop(l["id"], "dropped", reason=REASON)
        n += 1
        if n % 50 == 0:
            print(f"  {n}/{len(candidates)}…", flush=True)

    print(f"\n{n} loop(s) {'restored' if args.restore else 'dropped'}"
          f"{f', {len(archived_convs)} Front conversation(s) archived' if not args.restore else ''}.")
    print(f"Remaining active loops: {len(ledger.list_loops())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
