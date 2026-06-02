"""Tests for the Chief-of-Staff open-loop ledger (M1)."""
import importlib

import pytest


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("COS_DB_PATH", str(tmp_path / "cos.db"))
    from cos import ledger as ledger_mod
    importlib.reload(ledger_mod)
    ledger_mod.init_db()
    return ledger_mod


def _sample(ledger, **over):
    kw = dict(direction="i_owe", counterparty="Canon Sulerud",
              summary="Confirm processional order", channel="front",
              source_ref="cnv_9f2k1", importance=4)
    kw.update(over)
    return ledger.upsert_loop(**kw)


def test_insert_and_get(ledger):
    loop = _sample(ledger)
    assert loop["status"] == "open"
    assert loop["direction"] == "i_owe"
    assert loop["first_seen"]
    fetched = ledger.get_loop(loop["id"])
    assert fetched["summary"] == "Confirm processional order"


def test_upsert_is_idempotent(ledger):
    a = _sample(ledger)
    b = _sample(ledger, summary="Confirm processional order + preacher")
    assert a["id"] == b["id"]
    assert len(ledger.list_loops()) == 1
    assert ledger.get_loop(a["id"])["summary"].endswith("preacher")
    # first_seen preserved across the re-sweep
    assert ledger.get_loop(a["id"])["first_seen"] == a["first_seen"]


def test_direction_disambiguates_id(ledger):
    same = dict(counterparty="Bob", summary="x", channel="front", source_ref="cnv_1")
    one = ledger.upsert_loop(direction="i_owe", **same)
    two = ledger.upsert_loop(direction="owed_to_me", **same)
    assert one["id"] != two["id"]
    assert len(ledger.list_loops()) == 2


def test_manual_status_not_clobbered_by_upsert(ledger):
    loop = _sample(ledger)
    ledger.resolve_loop(loop["id"], "done")
    # A later ingestion sweep re-sees the thread and upserts again.
    again = _sample(ledger, summary="updated by sweep")
    assert again["status"] == "done"  # human decision preserved
    assert again["summary"] == "updated by sweep"  # machine field still refreshed


def test_list_filters_and_hides_resolved(ledger):
    _sample(ledger, source_ref="a", direction="i_owe")
    _sample(ledger, source_ref="b", direction="owed_to_me")
    done = _sample(ledger, source_ref="c", direction="i_owe")
    ledger.resolve_loop(done["id"], "done")

    assert len(ledger.list_loops()) == 2  # resolved hidden by default
    assert len(ledger.list_loops(include_resolved=True)) == 3
    assert len(ledger.list_loops(direction="i_owe")) == 1
    assert len(ledger.list_loops(direction="owed_to_me")) == 1


def test_overdue_filter(ledger):
    _sample(ledger, source_ref="past", due_at="2000-01-01T00:00:00Z")
    _sample(ledger, source_ref="future", due_at="2999-01-01T00:00:00Z")
    overdue = ledger.list_loops(overdue_only=True)
    assert len(overdue) == 1
    assert overdue[0]["source_ref"] == "past"


def test_snooze(ledger):
    loop = _sample(ledger)
    snoozed = ledger.snooze_loop(loop["id"], "2026-06-10T00:00:00Z")
    assert snoozed["status"] == "snoozed"
    assert snoozed["snooze_until"] == "2026-06-10T00:00:00Z"


def test_loops_get_sequential_stable_numbers(ledger):
    a = _sample(ledger, source_ref="a")
    b = _sample(ledger, source_ref="b")
    c = _sample(ledger, source_ref="c")
    assert [a["num"], b["num"], c["num"]] == [1, 2, 3]
    # Re-sweeping a thread keeps its number (stable reference).
    a2 = _sample(ledger, source_ref="a", summary="changed")
    assert a2["num"] == 1


def test_resolve_and_snooze_by_num(ledger):
    a = _sample(ledger, source_ref="a")
    b = _sample(ledger, source_ref="b")
    assert ledger.get_loop_by_num(b["num"])["id"] == b["id"]
    ledger.resolve_by_num(a["num"], "done")
    assert ledger.get_loop(a["id"])["status"] == "done"
    ledger.snooze_by_num(b["num"], "2026-07-01T00:00:00Z")
    assert ledger.get_loop(b["id"])["status"] == "snoozed"
    # Unknown number is a no-op (returns None), not a crash.
    assert ledger.resolve_by_num(999, "done") is None


def test_ordering_by_importance_then_due(ledger):
    _sample(ledger, source_ref="low", importance=1)
    _sample(ledger, source_ref="high", importance=5)
    loops = ledger.list_loops()
    assert loops[0]["source_ref"] == "high"


def test_invalid_direction_rejected(ledger):
    with pytest.raises(ValueError):
        ledger.upsert_loop(direction="sideways", counterparty="x", summary="y",
                           channel="front", source_ref="z")


def test_people_and_memory(ledger):
    ledger.people_upsert(key="Bob@Example.org", name="Bob", role="treasurer",
                         importance=5)
    people = ledger.list_people()
    assert people[0]["key"] == "bob@example.org"  # normalized lowercase

    ledger.remember("priorities", "Ordinations, budget, clergy care")
    assert ledger.get_memory("priorities")["priorities"].startswith("Ordinations")
    assert "priorities" in ledger.get_memory()


def test_stats(ledger):
    _sample(ledger, source_ref="a", direction="i_owe")
    _sample(ledger, source_ref="b", direction="owed_to_me")
    s = ledger.stats()
    assert s["total"] == 2
    assert s["open_by_direction"]["i_owe"] == 1
