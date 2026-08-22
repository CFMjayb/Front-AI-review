"""Tests for the per-mailbox split — registry, ledger filter, briefing sections."""
import importlib

import pytest


@pytest.fixture()
def mods(tmp_path, monkeypatch):
    monkeypatch.setenv("COS_DB_PATH", str(tmp_path / "cos.db"))
    monkeypatch.setenv("COS_TIMEZONE", "UTC")
    from cos import ledger as ledger_mod
    importlib.reload(ledger_mod)
    ledger_mod.init_db()
    from cos import briefing as briefing_mod, mailboxes as mailboxes_mod
    importlib.reload(mailboxes_mod)
    importlib.reload(briefing_mod)
    return ledger_mod, briefing_mod, mailboxes_mod


def _seed(ledger):
    ledger.upsert_loop(direction="i_owe", counterparty="Canon Sulerud",
                       summary="Confirm processional order", channel="front",
                       source_ref="cnv_1", mailbox="edom", importance=4)
    ledger.upsert_loop(direction="owed_to_me", counterparty="Bob",
                       summary="Council packet", channel="front",
                       source_ref="cnv_2", mailbox="edom", status="waiting")
    ledger.upsert_loop(direction="i_owe", counterparty="Vendor",
                       summary="Approve invoice", channel="front",
                       source_ref="cnv_3", mailbox="cfm")
    # No mailbox at all — a loop from before the split.
    ledger.upsert_loop(direction="i_owe", counterparty="Legacy",
                       summary="Old untagged loop", channel="front",
                       source_ref="cnv_4")


# ── registry ──────────────────────────────────────────────────────────────────

def test_registry_maps_inbox_to_mailbox(mods):
    _, _, mailboxes = mods
    assert mailboxes.key_for_inbox("inb_csx96") == "cfm"
    assert mailboxes.key_for_inbox("inb_cv4ii") == "edom"
    assert mailboxes.key_for_inbox("inb_nope") == mailboxes.UNASSIGNED
    assert mailboxes.key_for_inbox("") == mailboxes.UNASSIGNED


def test_first_registry_match_wins_for_multi_inbox_conversation(mods):
    _, _, mailboxes = mods
    # A conversation in both registered inboxes resolves to the earlier entry,
    # deterministically — never at random.
    assert mailboxes.key_for_inboxes(["inb_cv4ii", "inb_csx96"]) == "cfm"
    assert mailboxes.key_for_inboxes(["inb_cv4ii"]) == "edom"
    assert mailboxes.key_for_inboxes([]) == mailboxes.UNASSIGNED


def test_scan_inbox_ids_unions_env(mods, monkeypatch):
    _, _, mailboxes = mods
    monkeypatch.setenv("INBOX_IDS", "inb_csx96,inb_extra")
    ids = mailboxes.scan_inbox_ids()
    assert ids.count("inb_csx96") == 1, "no duplicate when env repeats a registry inbox"
    assert "inb_cv4ii" in ids, "registry inbox kept even when absent from env"
    assert "inb_extra" in ids, "env-only inbox still scanned"


# ── ledger filter ─────────────────────────────────────────────────────────────

def test_ledger_filters_by_mailbox(mods):
    ledger, _, _ = mods
    _seed(ledger)
    assert len(ledger.list_loops()) == 4, "unfiltered still returns everything"
    assert len(ledger.list_loops(mailbox="edom")) == 2
    assert len(ledger.list_loops(mailbox="cfm")) == 1


def test_unassigned_bucket_catches_loops_with_no_mailbox(mods):
    ledger, _, mailboxes = mods
    _seed(ledger)
    unassigned = ledger.list_loops(mailbox=mailboxes.UNASSIGNED)
    assert [l["counterparty"] for l in unassigned] == ["Legacy"]


def test_mailbox_survives_an_upsert_that_omits_it(mods):
    """reconcile() re-upserts a loop without knowing its mailbox — that must not
    blank the attribution."""
    ledger, _, _ = mods
    _seed(ledger)
    ledger.upsert_loop(direction="i_owe", counterparty="Canon Sulerud",
                       summary="Confirm processional order", channel="front",
                       source_ref="cnv_1", last_activity="2026-08-18T12:00:00Z")
    assert len(ledger.list_loops(mailbox="edom")) == 2


def test_patch_loop_sets_mailbox(mods):
    """The backfill's write path."""
    ledger, _, _ = mods
    _seed(ledger)
    legacy = ledger.list_loops(mailbox="other")[0]
    ledger.patch_loop(legacy["id"], mailbox="cfm")
    assert len(ledger.list_loops(mailbox="cfm")) == 2
    assert ledger.list_loops(mailbox="other") == []


# ── briefing ──────────────────────────────────────────────────────────────────

def test_gather_by_mailbox_splits_loops(mods):
    ledger, briefing, _ = mods
    _seed(ledger)
    per = dict(briefing.gather_by_mailbox())
    assert len(per["edom"]["on_you"]) == 1
    assert len(per["edom"]["waiting"]) == 1
    assert len(per["cfm"]["on_you"]) == 1
    assert len(per["other"]["on_you"]) == 1


def test_empty_unassigned_bucket_is_omitted(mods):
    ledger, briefing, mailboxes = mods
    ledger.upsert_loop(direction="i_owe", counterparty="Vendor", summary="x",
                       channel="front", source_ref="cnv_9", mailbox="cfm")
    keys = [k for k, _ in briefing.gather_by_mailbox()]
    assert "other" not in keys, "an empty unassigned bucket must not get a workbook"
    # Derived from the registry, not hardcoded — adding a mailbox should not
    # break this test (an earlier version listed the keys literally and did).
    assert keys == mailboxes.keys(), \
        "every registered mailbox appears, in registry order, even when empty"


def test_render_all_has_a_section_per_mailbox(mods):
    ledger, briefing, _ = mods
    _seed(ledger)
    per = briefing.gather_by_mailbox()
    subject, body = briefing.render_all(per, briefing.gather(), date="2026-08-18")
    assert "Jay — CFM" in body
    assert "Jay — EDOM" in body
    assert "Unattributed" in body
    # Subject reports the all-mailbox totals, not one mailbox's.
    assert "3 on you" in subject
    assert "1 waiting" in subject
    # 2026-08-22: the body only names urgent/high items; none of the seeded
    # loops set urgency (defaults to "normal"), so none should be named by
    # text — they're still accounted for via each mailbox's "more item(s)"
    # count instead. A split must not silently drop a loop from that count,
    # even though it no longer prints the loop's own name.
    for who in ("Canon Sulerud", "Bob", "Vendor", "Legacy"):
        assert who not in body
    assert "more item(s)" in body


def test_render_all_totals_match_the_sum_of_sections(mods):
    ledger, briefing, _ = mods
    _seed(ledger)
    per = briefing.gather_by_mailbox()
    assert sum(len(sec["on_you"]) for _, sec in per) == len(briefing.gather()["on_you"])
    assert sum(len(sec["waiting"]) for _, sec in per) == len(briefing.gather()["waiting"])


def test_a_loop_can_belong_to_two_mailboxes(mods):
    """Jay's own rule: a message addressed to two of his addresses belongs on
    BOTH sheets. Regression for a real bug found 2026-08-18: both ledger
    backends' list_loops(mailbox=...) either ignored the filter entirely
    (sqlite — the WHERE clause was deleted and never replaced) or called a
    keys_on_loop() that did not exist yet (firestore) — both bugs made every
    mailbox filter a no-op, caught only because these tests failed loudly."""
    ledger, _, mailboxes = mods
    ledger.upsert_loop(direction="i_owe", counterparty="Multi", summary="both",
                       channel="front", source_ref="cnv_multi",
                       mailboxes=["cfm", "edom"])
    assert len(ledger.list_loops(mailbox="cfm")) == 1
    assert len(ledger.list_loops(mailbox="edom")) == 1
    assert len(ledger.list_loops()) == 1, "still exactly one loop, not duplicated"


def test_calendar_appears_once_not_per_mailbox(mods):
    ledger, briefing, _ = mods
    _seed(ledger)
    per = briefing.gather_by_mailbox()
    for _, sec in per:
        assert sec["events"] == [], "per-mailbox sections carry no calendar"
    _, body = briefing.render_all(per, briefing.gather(), date="2026-08-18")
    assert body.count("## 📅 Today") <= 1
