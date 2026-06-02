"""Tests for M5 Teams ingestion (normalization, seen-gate, loop creation)."""
import importlib

import pytest


class FakeClaude:
    default_model = "fake"

    def __init__(self, output, cost=0.01):
        self.output = output
        self.cost = cost
        self.calls = 0

    def call(self, **_):
        self.calls += 1
        return {"json": self.output, "cost_usd": self.cost, "text": "", "parse_error": None}


@pytest.fixture()
def mods(tmp_path, monkeypatch):
    monkeypatch.setenv("COS_DB_PATH", str(tmp_path / "cos.db"))
    monkeypatch.setenv("COS_OWNER_EMAILS", "jay@cfmins.org")
    monkeypatch.setenv("COS_ENABLED", "true")
    monkeypatch.setenv("QUIET_THRESHOLD_HOURS", "36")
    from cos import ledger as ledger_mod
    importlib.reload(ledger_mod)
    ledger_mod.init_db()
    from cos import extract as extract_mod
    importlib.reload(extract_mod)
    from cos import ms_ingest as ms_mod
    importlib.reload(ms_mod)
    return ledger_mod, ms_mod


def _msg(sender, ts, text="hello?", to="jay@cfmins.org"):
    return {"sender_email": sender, "sender_name": sender.split("@")[0],
            "recipients": [to], "ts_epoch": ts, "text": text}


def _analysis(**over):
    a = {"category": "clergy", "urgency": "high", "urgency_confidence": 0.9,
         "requires_reply": True, "open_questions": ["when?"],
         "action_summary": "Reply to Mary about the schedule", "tldr": "She asked"}
    a.update(over)
    return a


def test_thread_from_teams_sets_direction_by_owner(mods):
    _, ms = mods
    thread = ms.thread_from_teams(chat_id="chat_1",
                                  messages=[_msg("mary@dio.org", 1000),
                                            _msg("jay@cfmins.org", 2000)])
    assert thread["channel"] == "teams"
    by_ts = {m["ts_epoch"]: m for m in thread["messages"]}
    assert by_ts[1000]["inbound"] is True        # from Mary
    assert by_ts[2000]["inbound"] is False       # from Jay (owner)


def test_ingest_creates_i_owe_for_inbound_last(mods):
    ledger, ms = mods
    import time
    thread = ms.thread_from_teams(chat_id="chat_2",
                                  messages=[_msg("mary@dio.org", time.time())])
    claude = FakeClaude(_analysis())
    res = ms.ingest([thread], claude)
    assert res["created"] == 1
    assert res["analyzed"] == 1
    loops = ledger.list_loops(channel="teams")
    assert len(loops) == 1
    assert loops[0]["direction"] == "i_owe"
    assert loops[0]["counterparty"] == "mary"


def test_seen_gate_skips_unchanged_thread(mods):
    ledger, ms = mods
    import time
    thread = ms.thread_from_teams(chat_id="chat_3", messages=[_msg("mary@dio.org", time.time())])
    claude = FakeClaude(_analysis())
    ms.ingest([thread], claude)
    res2 = ms.ingest([thread], claude)
    assert res2["skipped"] == 1
    assert claude.calls == 1  # not re-analyzed


def test_new_message_reanalyzes(mods):
    ledger, ms = mods
    import time
    now = time.time()
    claude = FakeClaude(_analysis())
    ms.ingest([ms.thread_from_teams(chat_id="chat_4", messages=[_msg("mary@dio.org", now)])], claude)
    # A newer message arrives → different marker → re-analyzed
    ms.ingest([ms.thread_from_teams(chat_id="chat_4",
               messages=[_msg("mary@dio.org", now), _msg("mary@dio.org", now + 100)])], claude)
    assert claude.calls == 2


def test_dry_run_writes_nothing(mods):
    ledger, ms = mods
    import time
    thread = ms.thread_from_teams(chat_id="chat_5", messages=[_msg("mary@dio.org", time.time())])
    claude = FakeClaude(_analysis())
    res = ms.ingest([thread], claude, dry_run=True)
    assert res["analyzed"] == 1
    assert ledger.list_loops(channel="teams") == []
    assert ledger.was_seen("teams", "chat_5", "x") is False  # not marked seen


def test_spam_thread_creates_no_loop(mods):
    ledger, ms = mods
    import time
    thread = ms.thread_from_teams(chat_id="chat_6", messages=[_msg("promo@ad.com", time.time())])
    claude = FakeClaude(_analysis(category="spam"))
    res = ms.ingest([thread], claude)
    assert res["analyzed"] == 1
    assert res["created"] == 0
    assert ledger.list_loops(channel="teams") == []
