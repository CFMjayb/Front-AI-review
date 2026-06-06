"""Backfill: archive resolved Front conversations in Front.

For every Firestore loop with status=done or status=dropped, channel=front, and
front_archived != True:
  1. GET the conversation's current Front status.
  2. If already non-open (archived/spam/deleted) — stamp front_archived=True in Firestore,
     no PATCH to Front needed.
  3. If 404 — conversation gone from Front; stamp and move on.
  4. If still open — PATCH status=archived, then stamp front_archived=True.

Re-runnable: already-stamped loops are skipped at the Firestore query step.

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

# Statuses that mean the conversation is still open in Front's inbox.
# Anything else (archived, spam, deleted) means Front already agrees with us.
_OPEN_STATUSES = {"open", "assigned", "unassigned"}

front = FrontClient(get_front_api_token())
db    = firestore.Client(project=os.environ["GCP_PROJECT"])


def _stamp(doc_id: str) -> None:
    """Mark a loop as archived in Front (Firestore only)."""
    db.collection("loops").document(doc_id).update({"front_archived": True})


# ── Fetch resolved Front loops that haven't been archived yet ────────────────
print("Querying Firestore for resolved Front loops not yet archived...")
resolved = []
already_done = 0
for snap in db.collection("loops").stream():
    d = snap.to_dict()
    if (d.get("status") in ("done", "dropped")
            and d.get("channel") == "front"
            and d.get("source_ref")):
        if d.get("front_archived"):
            already_done += 1
        else:
            resolved.append({**d, "id": snap.id})

print(f"Found {len(resolved)} to process  ({already_done} already stamped — skipped)")
if DRY_RUN:
    print("DRY RUN — no Front API calls will be made\n")
else:
    print()

archived = already_in_front = not_found = errors = 0

for i, loop in enumerate(resolved):
    src          = loop["source_ref"]
    num          = loop.get("num", "?")
    fs_status    = loop.get("status", "?")
    counterparty = (loop.get("counterparty") or "")[:50]

    if DRY_RUN:
        print(f"  #{num} [{fs_status}] {src}  {counterparty}")
        archived += 1
        continue

    # ── Phase 1: check current Front status ──────────────────────────────────
    try:
        conv = front.get_conversation(src)
        front_status = conv.get("status") or ""
    except FrontApiError as exc:
        if exc.status == 404:
            _stamp(loop["id"])
            print(f"  #{num} not found in Front — stamped  {src}")
            not_found += 1
            continue
        else:
            errors += 1
            print(f"  #{num} ERROR fetching status [{exc.status}]  {src}: {exc}")
            if errors > 20:
                print("  Too many errors — stopping early.")
                break
            continue
    except Exception as exc:
        errors += 1
        print(f"  #{num} ERROR fetching status  {src}: {exc}")
        if errors > 20:
            print("  Too many errors — stopping early.")
            break
        continue

    if front_status not in _OPEN_STATUSES:
        # Already archived / spam / other — Front already agrees; just stamp Firestore.
        _stamp(loop["id"])
        print(f"  #{num} already {front_status!r} in Front — stamped  {src}")
        already_in_front += 1
        continue

    # ── Phase 2: archive in Front (only reaches here if still open) ──────────
    try:
        front.set_status(src, "archived")
        _stamp(loop["id"])
        print(f"  #{num} [{fs_status}] archived  {src}  {counterparty}")
        archived += 1

    except FrontApiError as exc:
        if exc.status == 429:
            # Client already slept Retry-After; retry once more.
            try:
                front.set_status(src, "archived")
                _stamp(loop["id"])
                print(f"  #{num} [{fs_status}] archived (retry)  {src}  {counterparty}")
                archived += 1
            except Exception as retry_exc:
                errors += 1
                print(f"  #{num} ERROR (retry failed)  {src}: {retry_exc}")
                if errors > 20:
                    print("  Too many errors — stopping early.")
                    break
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
print(f"\n{action}: {archived}  "
      f"already done in Front: {already_in_front}  "
      f"not found: {not_found}  "
      f"errors: {errors}")
if DRY_RUN:
    print("Re-run without --dry-run to apply.")
