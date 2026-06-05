"""
One-time seed + backfill for DEDUP-1, FILTER-1, PRIORITY-1, GUIDANCE-1.

Run:  python seed_and_backfill.py
"""
import hashlib
import os
import re
import sys

os.environ.setdefault("LEDGER_BACKEND", "firestore")
os.environ.setdefault("GCP_PROJECT", "cfm-front-mail")

from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / ".env", override=True)

from cos import ledger

# ── Subject normalization (mirrors cos/extract.py) ───────────────────────────
_RE_AMOUNT  = re.compile(r'\$[\d,]+(?:\.\d+)?')
_RE_DATE    = re.compile(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b')
_RE_REF     = re.compile(r'\b[A-Z]{2,6}\d{6,}\b|\bINV[-#]?\d+\b|\breminder\s+#?\d+\b', re.IGNORECASE)
_RE_ORDINAL = re.compile(r'\b\d+(st|nd|rd|th)\b', re.IGNORECASE)
_RE_SPACES  = re.compile(r'\s+')

def _normalize(text):
    s = (text or "").lower()
    s = _RE_AMOUNT.sub(" ", s); s = _RE_DATE.sub(" ", s)
    s = _RE_REF.sub(" ", s);    s = _RE_ORDINAL.sub(" ", s)
    return _RE_SPACES.sub(" ", s).strip()

def _dedup_key(channel, email, summary):
    raw = f"{channel}|{(email or '').lower()}|{_normalize(summary)}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


# ── 1. DEDUP-1 — backfill dedup_key on all loops ────────────────────────────
print("=" * 60)
print("Step 1: Backfill dedup_key on all loops")

loops = ledger.list_loops(include_resolved=True)
print(f"  Total loops to process: {len(loops)}")

from google.cloud import firestore as _fs
db = _fs.Client(project=os.environ["GCP_PROJECT"])

batch = db.batch()
count = 0
for loop in loops:
    if loop.get("dedup_key"):
        continue
    dk = _dedup_key(loop["channel"], loop.get("counterparty_email", ""), loop.get("summary", ""))
    ref = db.collection("loops").document(loop["id"])
    batch.update(ref, {"dedup_key": dk})
    count += 1
    if count % 400 == 0:
        batch.commit()
        print(f"  Committed {count}...")
        batch = db.batch()

if count % 400 != 0:
    batch.commit()

print(f"  Backfilled dedup_key on {count} loops.")

# Verify
missing = sum(1 for l in ledger.list_loops(include_resolved=True) if not l.get("dedup_key"))
print(f"  Verification: loops missing dedup_key = {missing}  (expected 0)")


# ── 2. FILTER-1 — seed sender_rules ─────────────────────────────────────────
print("\n" + "=" * 60)
print("Step 2: Seed sender_rules collection")

SEED_RULES = [
    # Bill.com automated notifications — never an action item for Jay
    dict(email="@hq.bill.com",  action="fyi", notes="Bill.com automated notifications"),
    dict(email="@bill.com",     action="fyi", notes="Bill.com automated notifications"),
    # Atlantic Union Bank Positive Pay — SMS handled separately, drop from loop tracker
    dict(email="@atlanticunionbank.com", action="fyi",
         notes="Atlantic Union Bank alerts — Positive Pay SMS handles these"),
    # Plaud.ai recordings — already handled by Plaud pipeline
    dict(email="@plaud.ai", action="fyi",
         notes="Plaud.ai recording notifications — handled by Plaud pipeline"),
]

for rule in SEED_RULES:
    result = ledger.upsert_sender_rule(**rule)
    print(f"  upserted: {result['email']} -> {result['action']}")

all_rules = ledger.list_sender_rules()
print(f"  Verification: sender_rules count = {len(all_rules)}  (expected >= {len(SEED_RULES)})")


# ── 3. GUIDANCE-1 — seed guidance collection ────────────────────────────────
print("\n" + "=" * 60)
print("Step 3: Seed guidance collection")

SEED_GUIDANCE = [
    dict(
        key="wire-confirmations",
        scope="all",
        body="Wire transfer confirmation emails from any bank are always FYI — "
             "they confirm an action already taken and require no response.",
    ),
    dict(
        key="parish-payment-questions",
        scope="all",
        body="Emails from parish or entity contacts asking about payment status, "
             "invoice questions, or balance inquiries are always i_owe, category finance, "
             "importance high.",
    ),
    dict(
        key="grant-awards",
        scope="category:finance",
        body="Emails announcing grant awards or funding approvals are always importance "
             "urgent and category finance.",
    ),
    dict(
        key="bill-com-fyi",
        scope="sender:hq.bill.com",
        body="Bill.com emails are automated billing notifications. Unless they mention "
             "a payment failure or require an explicit approval action, classify as FYI.",
    ),
]

for g in SEED_GUIDANCE:
    result = ledger.upsert_guidance(**g)
    print(f"  upserted: {result['key']} (scope={result['scope']})")

all_guidance = ledger.list_guidance()
print(f"  Verification: guidance count = {len(all_guidance)}  (expected >= {len(SEED_GUIDANCE)})")


# ── 4. Drop duplicate loop #1528 (DEDUP-1 cleanup) ──────────────────────────
print("\n" + "=" * 60)
print("Step 4: Drop known duplicate loop #1528 (Bill.com reminder duplicate of #1115)")

dup = ledger.get_loop_by_num(1528)
if dup:
    ledger.resolve_loop(dup["id"], "dropped", reason="dedup-cleanup: duplicate of #1115")
    print(f"  Dropped loop #1528 ({dup.get('summary', '')[:60]})")
else:
    print("  Loop #1528 not found or already resolved — skipping.")


print("\n" + "=" * 60)
print("All done.")
