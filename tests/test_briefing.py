"""Tests for M4 daily briefing assembly (deterministic, no network)."""
import importlib

import pytest


@pytest.fixture()
def mods(tmp_path, monkeypatch):
    monkeypatch.setenv("COS_DB_PATH", str(tmp_path / "cos.db"))
    monkeypatch.setenv("COS_TIMEZONE", "UTC")
    from cos import ledger as ledger_mod
    importlib.reload(ledger_mod)
    ledger_mod.init_db()
    from cos import briefing as briefing_mod
    importlib.reload(briefing_mod)
    return ledger_mod, briefing_mod


def _seed(ledger):
    ledger.upsert_loop(direction="i_owe", counterparty="Canon Sulerud",
                       summary="Confirm processional order", channel="front",
                       source_ref="cnv_1", source_link="https://app.frontapp.com/open/cnv_1",
                       importance=4, due_at="2026-06-04")
    ledger.upsert_loop(direction="i_owe", counterparty="St. Anne's vestry",
                       summary="Approve rental waiver", channel="front",
                       source_ref="cnv_2", importance=3)
    ledger.upsert_loop(direction="owed_to_me", counterparty="Bob",
                       summary="Council financial packet", channel="front",
                       source_ref="cnv_3", status="waiting", importance=3)


def test_gather_splits_directions(mods):
    ledger, briefing = mods
    _seed(ledger)
    sections = briefing.gather()
    assert len(sections["on_you"]) == 2
    assert len(sections["waiting"]) == 1


def test_subject_line_counts(mods):
    ledger, briefing = mods
    _seed(ledger)
    subject, _ = briefing.render(briefing.gather(), date="2026-06-02")
    assert "2 on you" in subject
    assert "1 waiting" in subject


def test_render_includes_loops_and_links(mods):
    ledger, briefing = mods
    _seed(ledger)
    _, body = briefing.render(briefing.gather(), date="2026-06-02",
                              filtered_count=18)
    assert "Canon Sulerud" in body
    assert "(due 2026-06-04)" in body
    assert "https://app.frontapp.com/open/cnv_1" in body
    assert "18** marketing" in body
    assert "🔴 On you (2)" in body


def test_importance_orders_on_you(mods):
    ledger, briefing = mods
    _seed(ledger)
    sections = briefing.gather()
    assert sections["on_you"][0]["counterparty"] == "Canon Sulerud"  # importance 4 first


def test_empty_ledger_renders_gracefully(mods):
    ledger, briefing = mods
    subject, body = briefing.render(briefing.gather(), date="2026-06-02")
    assert "0 on you" in subject
    assert "Nothing on you" in body


def test_resolved_loops_excluded(mods):
    ledger, briefing = mods
    _seed(ledger)
    done = ledger.list_loops(direction="i_owe")[0]
    ledger.resolve_loop(done["id"], "done")
    assert len(briefing.gather()["on_you"]) == 1


def test_snoozed_future_loop_hidden(mods):
    ledger, briefing = mods
    _seed(ledger)
    loop = ledger.list_loops(direction="owed_to_me")[0]
    ledger.snooze_loop(loop["id"], "2999-01-01T00:00:00Z")
    assert len(briefing.gather()["waiting"]) == 0


def test_run_briefing_writes_file(mods, tmp_path, monkeypatch):
    ledger, briefing = mods
    monkeypatch.setattr(briefing, "BRIEF_DIR", tmp_path / "briefings")
    _seed(ledger)
    result = briefing.run_briefing(claude=None, filtered_count=5)
    assert result["transport"] == "file"
    assert (tmp_path / "briefings" / f"{result['file'].split('/')[-1]}").exists()
    assert result["counts"]["on_you"] == 2
