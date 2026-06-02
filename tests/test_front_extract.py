"""Tests for M3 Front loop extraction (direction logic + reconcile)."""
import importlib
import time

import pytest


@pytest.fixture()
def mods(tmp_path, monkeypatch):
    monkeypatch.setenv("COS_DB_PATH", str(tmp_path / "cos.db"))
    monkeypatch.setenv("QUIET_THRESHOLD_HOURS", "36")
    monkeypatch.setenv("COS_ENABLED", "true")
    from cos import ledger as ledger_mod
    importlib.reload(ledger_mod)
    ledger_mod.init_db()
    from cos import front_extract as fx_mod
    importlib.reload(fx_mod)
    return ledger_mod, fx_mod


def _conv(cid="cnv_1", subject="Re: ordination"):
    return {"id": cid, "subject": subject, "status": "open"}


def _msg(inbound, ago_hours=0.0, email="mary@dio.org", to="jay@cfmins.org"):
    return {"is_inbound": inbound, "created_at": time.time() - ago_hours * 3600,
            "author": {"email": email, "first_name": "Mary", "last_name": "S"},
            "to": [{"handle": to}]}


def _analysis(**over):
    a = {"category": "clergy", "urgency": "high", "urgency_confidence": 0.9,
         "requires_reply": True, "requires_approval": False, "requires_payment": False,
         "action_items": [], "open_questions": ["confirm preacher?"],
         "action_summary": "Reply with processional order", "tldr": "She needs the order",
         "deadline": "2026-06-04"}
    a.update(over)
    return a


def test_inbound_needing_reply_creates_i_owe(mods):
    ledger, fx = mods
    loop = fx.extract_from_analysis(_conv(), [_msg(inbound=True)], _analysis())
    assert loop["direction"] == "i_owe"
    assert loop["status"] == "open"
    assert loop["counterparty"] == "Mary S"
    assert loop["importance"] == 4  # high → 4
    assert loop["due_at"] == "2026-06-04"
    assert loop["source_link"].endswith("/open/cnv_1")


def test_inbound_approval_only_still_creates_loop(mods):
    """Broadest trigger: an invoice needing approval with no question is still a loop."""
    ledger, fx = mods
    a = _analysis(requires_reply=False, open_questions=[], requires_approval=True,
                  action_summary="Approve the rental waiver")
    loop = fx.extract_from_analysis(_conv("cnv_inv"), [_msg(inbound=True)], a)
    assert loop is not None
    assert loop["direction"] == "i_owe"


def test_inbound_fyi_creates_no_loop(mods):
    ledger, fx = mods
    a = _analysis(requires_reply=False, requires_approval=False, requires_payment=False,
                  action_items=[], open_questions=[], action_summary="FYI only")
    assert fx.extract_from_analysis(_conv(), [_msg(inbound=True)], a) is None


def test_spam_is_never_a_loop(mods):
    ledger, fx = mods
    assert fx.extract_from_analysis(_conv(), [_msg(inbound=True)],
                                    _analysis(category="spam")) is None


def test_outbound_quiet_past_threshold_is_owed_to_me(mods):
    ledger, fx = mods
    loop = fx.extract_from_analysis(_conv("cnv_q"), [_msg(inbound=False, ago_hours=48)],
                                    _analysis())
    assert loop["direction"] == "owed_to_me"
    assert loop["status"] == "waiting"
    assert loop["counterparty"] == "jay@cfmins.org"


def test_outbound_still_fresh_creates_no_loop(mods):
    ledger, fx = mods
    loop = fx.extract_from_analysis(_conv("cnv_fresh"), [_msg(inbound=False, ago_hours=2)],
                                    _analysis())
    assert loop is None


def test_dry_run_does_not_write(mods):
    ledger, fx = mods
    preview = fx.extract_from_analysis(_conv(), [_msg(inbound=True)], _analysis(), dry_run=True)
    assert preview["dry_run"] is True
    assert ledger.list_loops() == []


def _real_inbound(email="hgraham@claggettcenter.org", name="Hannah Graham", ago_hours=1.0):
    """A real-shape Front inbound message: author is null, sender is in
    recipients[role='from']."""
    return {"is_inbound": True, "created_at": time.time() - ago_hours * 3600,
            "author": None,
            "recipients": [{"role": "from", "handle": email, "name": name},
                           {"role": "to", "handle": "jboggs@episcopalmaryland.org",
                            "name": "Jay Boggs"}]}


def test_inbound_counterparty_from_recipients_role(mods):
    # Regression: author=null on inbound used to yield counterparty 'unknown'.
    ledger, fx = mods
    loop = fx.extract_from_analysis(_conv("cnv_real"), [_real_inbound()], _analysis())
    assert loop["counterparty"] == "Hannah Graham"
    assert loop["counterparty_email"] == "hgraham@claggettcenter.org"


def test_cos_disabled_short_circuits(mods, monkeypatch):
    ledger, fx = mods
    monkeypatch.setenv("COS_ENABLED", "false")
    assert fx.extract_from_analysis(_conv(), [_msg(inbound=True)], _analysis()) is None


# ── Reconcile ────────────────────────────────────────────────────────────────

class FakeFront:
    def __init__(self, messages_by_conv):
        self._m = messages_by_conv

    def get_conversation_messages(self, conv_id, **_):
        return self._m.get(conv_id, [])


def test_reconcile_resolves_i_owe_after_jay_replies(mods):
    ledger, fx = mods
    fx.extract_from_analysis(_conv("cnv_r"), [_msg(inbound=True)], _analysis())
    # Jay has since replied → last message is now outbound
    front = FakeFront({"cnv_r": [_msg(inbound=True, ago_hours=5),
                                  _msg(inbound=False, ago_hours=1)]})
    res = fx.reconcile_open_front_loops(front)
    assert res["resolved"] == 1
    assert ledger.list_loops() == []  # resolved loops hidden


def test_reconcile_resolves_owed_to_me_after_they_reply(mods):
    ledger, fx = mods
    fx.extract_from_analysis(_conv("cnv_o"), [_msg(inbound=False, ago_hours=48)], _analysis())
    front = FakeFront({"cnv_o": [_msg(inbound=False, ago_hours=48),
                                 _msg(inbound=True, ago_hours=1)]})
    res = fx.reconcile_open_front_loops(front)
    assert res["resolved"] == 1
