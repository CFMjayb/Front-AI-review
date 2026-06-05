# 26-119 Chief-of-Staff — Pending Changes

Items gathered here are ready to implement as a batch. When the user says "proceed" (or similar), implement all items in this file, then clear it.

## Required sections for every item that touches the data model

Every item that adds or changes fields on Firestore/SQLite loops (or any other collection) MUST include:

- **Affected records:** query count of existing records that need updating
- **Derivation:** can the new field be computed from already-stored data? If yes, how. If no, what safe default applies and what populates it over time.
- **Backfill script:** the exact Python to run against Firestore
- **Verification query:** the exact query to confirm 100% population after backfill
- **Execute order:** backfill runs in the same session as the schema change — before the release is declared complete

---

## Batch 2

### ENTITY-1 — Identify entity (QBO company) from Front contact, inbox, and company record
**Problem:** Loops have no entity field — there's no way to know whether an email relates to EDOM, Claggett, CFM, Diocese of Maine, etc. This matters for routing, briefing grouping, and filtering.

**Three signals available in Front, in priority order:**
1. **Inbox** — each Front inbox may belong to a specific entity (e.g. EDOM inbox → entity `EDOM`). Map inbox IDs → entity codes in config (env var or QBOcompanies.xlsx `InboxMap` tab).
2. **Contact's Company in Front** — Front contacts have a Company field. If we store QBO company short-codes (e.g. `EDOM`, `CLAGGETT`, `CFM`) in that field, the pipeline can read `conversation.contact.company` to identify the entity.
3. **Sender domain** — fallback: known domains mapped to entities (e.g. `edom.org` → `EDOM`). Add to sender_rules or a separate domain→entity map.

**Implementation:**
- Add `entity` field to loop records (Firestore + SQLite migration).
- In `cos/front_extract.py` `extract_from_analysis()`: pull inbox ID → look up entity from inbox map. Also check `conv.get("contact", {}).get("company")`.
- Add `entity` to `upsert_loop()` signature (both backends).
- Expose `entity` in triage export as a column (between Dir and Action Type).
- **QBOcompanies.xlsx change:** Add `InboxMap` tab: columns `InboxID`, `EntityCode`, `EntityName`.
- Briefing: group open loops by entity when multiple entities in scope.

**Prerequisite:** Need the Front inbox IDs for each entity before implementing. Pull from Front API or the `inbox-ids` secret.

**Data Migration:**
- Affected: `entity` field will be null on all existing loops
- Derivation: domain-based partial backfill from `counterparty_email`; remaining loops populate as pipeline re-processes them
- Backfill: domain→entity seed map (e.g. `edom.org` → EDOM); leave null otherwise
- Verification: report count with entity set vs null after backfill
- Execute: same session as schema change

---
