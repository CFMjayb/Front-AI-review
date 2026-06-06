"""One-time backfill: archive resolved Front conversations in Front.

For every Firestore loop with status=done or status=dropped and channel=front,
calls PATCH /conversations/{id} status=archived so the conversation disappears
from the Front open list.

Run:
    python backfill_archive_front_resolved.py           # live
    python backfill_archive_front_resolved.py --dry-run # preview only
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("LEDGER_BACKEND", "firestore")
os.environ.setdefault("GCP_PROJECT", "cfm-front-mail")
os.environ.setdefault("USE_SECRET_MANAGER", "true")

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)

from google.cloud import firestore
from front_client import FrontClient, FrontApiError
from auth import get_front_api_token

DRY_RUN = "--dry-run" in sys.argv

front = FrontClient(get_front_api_token())
db    = firestore.Client(project=os.environ["GCP_PROJECT"])

# ── Fetch all resolved Front loops directly from Firestore ──────────────────
print("Querying Firestore for resolved Front loops...")
resolved = []
for snap in db.collection("loops").stream():
    d = snap.to_dict()
    if (d.get("status") in ("done", "dropped")
            and d.get("channel") == "front"
            and d.get("source_ref")):
        resolved.append({**d, "id": snap.id})

print(f"Found {len(resolved)} resolved Front loops to process")
if DRY_RUN:
    print("DRY RUN — no Front API calls will be made\n")
else:
    print()

archived = skipped = errors = 0

for i, loop in enumerate(resolved):
    src         = loop["source_ref"]
    num         = loop.get("num", "?")
    fs_status   = loop.get("status", "?")
    counterparty = (loop.get("counterparty") or "")[:50]

    if DRY_RUN:
        print(f"  #{num} [{fs_status}] {src}  {counterparty}")
        archived += 1
        continue

    try:
        front.set_status(src, "archived")
        print(f"  #{num} [{fs_status}] archived  {src}  {counterparty}")
        archived += 1

    except FrontApiError as exc:
        if exc.status == 404:
            # Conversation deleted or purged from Front — nothing to archive
            print(f"  #{num} SKIP (not found)  {src}")
            skipped += 1
        else:
            errors += 1
            print(f"  #{num} ERROR [{exc.status}]  {src}: {exc}")
            if errors > 20:
                print("  Too many errors — stopping early.")
                break

    except Exception as exc:
        errors += 1
        print(f"  #{num} ERROR  {src}: {exc}")
        if errors > 20:
            print("  Too many errors — stopping early.")
            break

    # Courtesy pause every 20 calls to stay well inside Front rate limits
    if (i + 1) % 20 == 0:
        time.sleep(1)

action = "would archive" if DRY_RUN else "archived"
print(f"\n{action}: {archived}   skipped (not found): {skipped}   errors: {errors}")
if DRY_RUN:
    print("Re-run without --dry-run to apply.")
