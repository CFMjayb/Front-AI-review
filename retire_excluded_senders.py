"""Drop active loops whose sender now matches an 'exclude' sender rule.

Adding an exclude rule stops NEW loops being created, but says nothing about the
ones already in the ledger. This closes that gap.

It reads the rules rather than hardcoding senders, so it stays correct as rules
change: add an exclude rule, re-run this, and the matching backlog clears.

Respects subject_pattern. A rule scoped to part of a sender's mail (e.g.
businessoffice@episcopalmaryland.org limited to Beacon notifications) only drops
the loops whose Front subject actually matches — a real email from a shared
mailbox is never swept out with the automated noise. That costs one Front call
per candidate, which is why only patterned rules pay it.

Also archives each matching Front conversation — a loop leaving the triage list
must not leave its email open and unread. (A first version of this script only
touched the ledger; Jay caught the gap same day and asked for Plaud + all
excluded senders to be archived, not just delisted.)

Reversible: sets status='dropped' (recorded in the feedback log), never deletes.

Usage:
    python retire_excluded_senders.py            # report only (default)
    python retire_excluded_senders.py --live
"""
import argparse
import collections
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("LEDGER_BACKEND", os.environ.get("LEDGER_BACKEND", "firestore"))
os.environ.setdefault("GCP_PROJECT", os.environ.get("GCP_PROJECT", "cfm-front-mail"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from auth import get_front_api_token
from cos import ledger
from front_client import FrontClient

REASON = "sender-excluded"


def _matches(sender: str, rule_email: str) -> bool:
    """Same matching the pipeline uses: exact address, or @domain / @sub.domain."""
    sender = (sender or "").lower()
    rule_email = (rule_email or "").lower()
    if not sender or not rule_email:
        return False
    if rule_email.startswith("@"):
        return sender.endswith(rule_email)
    return sender == rule_email


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="write; default is report-only")
    args = ap.parse_args()

    excludes = [r for r in ledger.list_sender_rules() if r.get("action") == "exclude"]
    if not excludes:
        print("No 'exclude' sender rules. Nothing to do.")
        return 0

    print("exclude rules in force:")
    for r in excludes:
        pat = r.get("subject_pattern")
        print(f"  {r['email']:42s}" + (f"  subject~ {pat}" if pat else ""))
    print()

    front = None
    loops = ledger.list_loops()
    hits: list[tuple[dict, dict]] = []
    needs_subject = 0

    for l in loops:
        sender = (l.get("counterparty_email") or "").lower()
        for r in excludes:
            if not _matches(sender, r["email"]):
                continue
            pat = (r.get("subject_pattern") or "").strip()
            if pat:
                needs_subject += 1
                if front is None:
                    front = FrontClient(get_front_api_token())
                try:
                    subj = front.get_conversation(l["source_ref"]).get("subject") or ""
                except Exception as exc:
                    print(f"  ! #{l.get('num')} subject unreadable ({exc}) — LEFT ALONE")
                    break
                if not re.search(pat, subj, re.I):
                    break          # real mail from a shared mailbox — keep it
            hits.append((l, r))
            break

    if not hits:
        print("No active loops match an exclude rule.")
        return 0

    by_sender = collections.Counter((l.get("counterparty_email") or "?").lower()
                                    for l, _ in hits)
    print(f"{len(hits)} active loop(s) to drop"
          f"{f' ({needs_subject} needed a subject check)' if needs_subject else ''}:")
    for k, n in by_sender.most_common():
        print(f"  {n:5d}  {k}")

    print("\nexamples:")
    for l, _ in hits[:5]:
        print(f"  #{l.get('num')}  {(l.get('summary') or '')[:76]}")

    if not args.live:
        print("\nReport only — nothing written. Re-run with --live to drop.")
        return 0

    from cos import front_archive
    if front is None:
        front = FrontClient(get_front_api_token())
    archived = 0
    for i, (l, _) in enumerate(hits, 1):
        if front_archive.archive_loop(front, l, printer=print):
            archived += 1
        ledger.resolve_loop(l["id"], "dropped", reason=REASON)
        if i % 25 == 0:
            print(f"  {i}/{len(hits)}…", flush=True)

    print(f"\n{len(hits)} loop(s) dropped, {archived} Front conversation(s) archived. "
          f"Active loops now: {len(ledger.list_loops())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
