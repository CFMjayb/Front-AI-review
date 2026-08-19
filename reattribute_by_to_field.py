"""Re-attribute loops by the To: field (see cos/mailboxes.py).

The first version of the mailbox split attributed by Front inbox. Jay's actual
rule is the To: address — "if an email comes in to jay@cfmins.org it goes on CFM;
if it comes in to jboggs@episcopalmaryland.org it is on EDOM — this has nothing
to do with who the email is from." Cc does not count, and mail addressed to two
of his addresses belongs on BOTH spreadsheets.

This walks every loop, reads its conversation's messages from Front, and computes
the mailbox key LIST from the To: handles. The Front inbox is used only when no
To: address matches (BCC, forward, or a non-email channel).

Idempotent, and read-only unless --live is passed.

Usage:
    python reattribute_by_to_field.py                # report only (default)
    python reattribute_by_to_field.py --live         # write the new attribution
    python reattribute_by_to_field.py --limit 100    # sample while testing
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


def to_handles(messages: list[dict]) -> list[str]:
    """Every To: handle across the thread. Cc and Bcc deliberately excluded."""
    out: list[str] = []
    for m in messages or []:
        for r in (m.get("recipients") or []):
            if r.get("role") == "to" and r.get("handle"):
                out.append(r["handle"])
    return out


def _fetch(front: FrontClient, conv_id: str, tries: int = 3):
    """Messages for a conversation, retrying rate limits (the client sleeps
    Retry-After and THEN raises, so a retry is what banks that wait)."""
    for attempt in range(tries):
        try:
            return front.get_conversation_messages(conv_id, max_pages=2)
        except Exception as exc:
            if "rate limit" in str(exc).lower() and attempt < tries - 1:
                continue
            raise
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="write; default is report-only")
    ap.add_argument("--limit", type=int, default=0, help="only the first N loops")
    args = ap.parse_args()

    front = FrontClient(get_front_api_token())

    loops = ledger.list_loops()
    loops += ledger.list_loops(deferred_only=True)
    seen: set[str] = set()
    unique = [l for l in loops if not (l["id"] in seen or seen.add(l["id"]))]
    unique = [l for l in unique if l.get("channel") == "front"]
    if args.limit:
        unique = unique[: args.limit]

    print(f"{len(unique)} loop(s) to re-attribute by To:  "
          f"({'LIVE' if args.live else 'REPORT ONLY'})\n")

    verdict: collections.Counter = collections.Counter()
    moves: collections.Counter = collections.Counter()
    examples: list = []
    changed = errors = 0

    for i, loop in enumerate(unique, 1):
        cid = loop.get("source_ref")
        if not cid:
            verdict["no source_ref"] += 1
            continue
        try:
            msgs = _fetch(front, cid)
        except Exception as exc:
            print(f"  ! {cid}: {exc}")
            errors += 1
            continue

        keys = mailboxes.keys_for_recipients(to_handles(msgs))
        source = "to"
        if not keys:
            # No To: match. Fall back to the inbox rather than guessing, and count
            # these separately so the fallback's real size is visible.
            try:
                ids = [x.get("id") for x in front.list_conversation_inboxes(cid)]
                keys = [mailboxes.key_for_inboxes([x for x in ids if x])]
                source = "inbox-fallback"
            except Exception as exc:
                print(f"  ! {cid} inbox fallback: {exc}")
                errors += 1
                continue

        verdict[f"{source}: {'+'.join(keys)}"] += 1

        was = loop.get("mailboxes") or ([loop["mailbox"]] if loop.get("mailbox") else [])
        if sorted(was) != sorted(keys):
            changed += 1
            moves[f"{'+'.join(sorted(was)) or '(none)'} -> {'+'.join(sorted(keys))}"] += 1
            if len(examples) < 12:
                examples.append((loop, was, keys, source))
            if args.live:
                ledger.patch_loop(loop["id"], mailboxes=keys, mailbox=keys[0])

        if i % 50 == 0:
            print(f"  {i}/{len(unique)} … {changed} would move" if not args.live
                  else f"  {i}/{len(unique)} … {changed} moved", flush=True)
        time.sleep(0.05)

    print("\n=== attribution source & result ===")
    for k, n in verdict.most_common():
        print(f"  {n:5d}  {k}")

    print("\n=== reassignments ===")
    for k, n in moves.most_common():
        print(f"  {n:5d}  {k}")

    print("\n=== examples ===")
    for loop, was, now, src in examples:
        print(f"  #{loop.get('num')}  {'+'.join(was) or '(none)'} -> {'+'.join(now)}  [{src}]")
        print(f"      {loop.get('counterparty','')[:34]} | {(loop.get('summary') or '')[:70]}")

    print(f"\n{changed} loop(s) {'moved' if args.live else 'would move'}"
          f"{f', {errors} error(s)' if errors else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
