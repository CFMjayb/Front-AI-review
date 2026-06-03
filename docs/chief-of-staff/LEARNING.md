# CoS Learning & Feedback (design)

A Chief of Staff must get more *yours* the longer it runs — learning what you
treat as noise, who matters, and how you triage. This is the design for that.

## Principle
**You can't learn from data you didn't record.** Capture every triage action as
structured signal first; build the learners on top. Reuse the proven pattern from
the email pipeline (category corrections → `analyze-examples` secret → injected
into the prompt): *observe a human decision → persist it → feed it back.*

## Phase 1 — Feedback event log  ✅ built
Every resolution writes a `feedback` record (both ledger backends), at the single
chokepoint inside `resolve_loop` / `snooze_loop` — so it fires no matter the
surface (reply/archive reconcile, conversational `#num`, desktop).

Record fields: `ts, action (done|dropped|snoozed), loop_id, num, direction,
channel, category, counterparty, counterparty_email, importance, due_at,
age_hours (first_seen→resolution), snooze_until, reason`.

- `ledger.list_feedback(action, since)` reads it; `cos_list_feedback` exposes it.
- The resolve/snooze tools accept an optional one-word `reason` ("noise",
  "handled in bank portal") for richer signal — never required.

## Phase 2 — Learners (next, built on the log)
1. **Drop-driven noise learning.** Aggregate `dropped` by sender/domain/category.
   A counterparty/pattern you repeatedly drop → a learned "don't make a loop"
   list that `front_extract` + the spam pre-filter consult. (Mirrors the
   corrections loop; persists to a secret or a `memory` key.) This is how the
   briefing shrinks from 88 to *your* real signal.
2. **Importance learning → ranking.** Resolution timing is the teacher: acted on
   fast = important; snoozed/deferred = less; dropped = noise. Update
   `people.importance` + per-category weights that bias briefing order. Finally
   feeds the (currently empty) `people`/`memory` hooks.
3. **Snooze-pattern learning.** If you always snooze a category to near its due
   date, learn its default surfacing behavior.

## Phase 3 — Transparency & correction
A weekly "What I learned" section in the digest (alongside the existing category
corrections): "you dropped 6 BILL.com loops → I'll stop surfacing them"; "you
prioritized Bishop Carrie → ranked higher." Lets you correct the *learning
itself* — the human-in-the-loop guardrail.

## Why this ordering
The log is cheap, non-destructive, and time-sensitive: loops are live in
Firestore now and every resolution before logging existed is signal lost. The
learners need accumulated data to be any good, so they come after the log has
run for a while.
