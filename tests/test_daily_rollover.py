"""Tests for the daily-email rollover (Jay, 2026-10-05): a new daily email from
notifications@cfmins.org archives the earlier ones in the same series.
"""
from cos import daily_rollover as dr

YOUR_DAY = r"Your day\W+\d{4}-\d{2}-\d{2}"
CFM_DAILY = r"^CFM Daily Processing"
RULE = {"email": "notifications@cfmins.org", "action": "exclude", "keep_in_front": True,
        "supersede_series": [YOUR_DAY, CFM_DAILY]}


def _conv(cid, subject, created, status="unassigned"):
    return {"id": cid, "subject": subject, "created_at": created, "status": status}


def _yd(day, n=1):
    return f"☀️ Your day — 2026-10-{day:02d} · {n} on you · 0 waiting · 0 meeting"


def _cfm(day):
    return f"CFM Daily Processing ⚠ — October {day:02d}, 2026"


class _Err(Exception):
    def __init__(self, status):
        super().__init__(f"err {status}")
        self.status = status


class _FakeFront:
    def __init__(self, convs, messages=None, msg_errors=None, set_errors=None):
        self.convs = convs
        self.messages = messages or {}
        self.msg_errors = {k: list(v) for k, v in (msg_errors or {}).items()}
        self.set_errors = {k: list(v) for k, v in (set_errors or {}).items()}
        self.set_calls = []
        self.queries = []

    def search_conversations(self, query, *, max_pages=3):
        self.queries.append(query)
        return list(self.convs)

    def get_conversation_messages(self, cid, **kw):
        if self.msg_errors.get(cid):
            raise self.msg_errors[cid].pop(0)
        return self.messages.get(cid, [{"is_inbound": True, "is_draft": False}])

    def set_status(self, cid, status):
        if self.set_errors.get(cid):
            raise self.set_errors[cid].pop(0)
        self.set_calls.append((cid, status))


def _archived(front):
    return sorted(c for c, s in front.set_calls if s == "archived")


# ---- the rule's patterns match the real subjects, and only those ----------

def test_patterns_match_real_subjects_only():
    assert dr._matches(YOUR_DAY, _yd(5, 95))
    assert dr._matches(CFM_DAILY, _cfm(5))
    for other in ("Staff follow-up drafts: 3 emails ready to forward + your to-do list",
                  "A bill payment has been scheduled for The Claggett",
                  "Daily summary: requests pending your approval", ""):
        assert not dr._matches(YOUR_DAY, other)
        assert not dr._matches(CFM_DAILY, other)


# ---- core behaviour -------------------------------------------------------

def test_archives_older_open_keeps_newest():
    front = _FakeFront([
        _conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400), _conv("n3", _yd(3), 300),
        _conv("c5", _cfm(5), 505), _conv("c4", _cfm(4), 405),
    ])
    out = dr.run_rule(front, RULE)
    assert _archived(front) == ["c4", "n3", "n4"]
    assert out == {"checked": 3, "archived": 3, "kept_acted_on": 0, "errors": 0}
    assert front.queries == ["from:notifications@cfmins.org"]


def test_newest_already_archived_still_supersedes_older_open():
    # Deidre archived today's already; yesterday's is still open and is older.
    front = _FakeFront([_conv("n5", _yd(5), 500, "archived"), _conv("n4", _yd(4), 400)])
    dr.run_rule(front, RULE)
    assert _archived(front) == ["n4"]


def test_already_archived_older_are_not_touched_again():
    front = _FakeFront([_conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400, "archived")])
    out = dr.run_rule(front, RULE)
    assert front.set_calls == [] and out["checked"] == 0


def test_single_conversation_in_series_does_nothing():
    front = _FakeFront([_conv("n5", _yd(5), 500)])
    assert dr.run_rule(front, RULE)["archived"] == 0
    assert front.set_calls == []


def test_same_day_resend_supersedes_the_earlier_copy():
    front = _FakeFront([_conv("n2b", _yd(2, 115), 1155), _conv("n2a", _yd(2, 142), 950)])
    dr.run_rule(front, RULE)
    assert _archived(front) == ["n2a"]


def test_other_mail_from_the_sender_is_never_archived():
    front = _FakeFront([
        _conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400),
        _conv("s1", "Staff follow-up drafts: 3 emails ready to forward", 350),
        _conv("s2", "Staff follow-up drafts: 3 emails ready to forward", 450),
    ])
    dr.run_rule(front, RULE)
    assert _archived(front) == ["n4"]


def test_series_are_independent():
    # A newer "Your day" must not supersede an older "CFM Daily Processing".
    front = _FakeFront([_conv("n5", _yd(5), 900), _conv("c4", _cfm(4), 100)])
    dr.run_rule(front, RULE)
    assert front.set_calls == []


# ---- safety: never archive something a person touched ---------------------

def test_conversation_with_a_reply_or_forward_is_left_open():
    front = _FakeFront(
        [_conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400)],
        messages={"n4": [{"is_inbound": True, "is_draft": False},
                         {"is_inbound": False, "is_draft": False}]})
    out = dr.run_rule(front, RULE)
    assert front.set_calls == [] and out["kept_acted_on"] == 1 and out["archived"] == 0


def test_conversation_with_a_draft_is_left_open():
    front = _FakeFront(
        [_conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400)],
        messages={"n4": [{"is_inbound": True, "is_draft": False},
                         {"is_inbound": True, "is_draft": True}]})
    dr.run_rule(front, RULE)
    assert front.set_calls == []


def test_unreadable_conversation_is_left_open():
    front = _FakeFront([_conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400)],
                       messages={"n4": []})
    dr.run_rule(front, RULE)
    assert front.set_calls == []


# ---- dry run, limits, errors ---------------------------------------------

def test_dry_run_archives_nothing_but_counts():
    front = _FakeFront([_conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400)])
    out = dr.run_rule(front, RULE, dry_run=True)
    assert front.set_calls == [] and out["archived"] == 1


def test_budget_caps_archives_per_run():
    front = _FakeFront([_conv(f"n{d}", _yd(d), d * 100) for d in range(1, 10)])
    out = dr.run_rule(front, RULE, budget=3)
    assert len(front.set_calls) == 3 and out["archived"] == 3
    assert "n9" not in _archived(front)


def test_one_failure_does_not_stop_the_rest():
    front = _FakeFront(
        [_conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400), _conv("n3", _yd(3), 300)],
        set_errors={"n4": [_Err(500)]})
    out = dr.run_rule(front, RULE)
    assert _archived(front) == ["n3"] and out["errors"] == 1


def test_429_is_retried():
    front = _FakeFront([_conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400)],
                       set_errors={"n4": [_Err(429)]})
    dr.run_rule(front, RULE)
    assert _archived(front) == ["n4"]


def test_bad_regex_is_ignored_not_fatal():
    rule = dict(RULE, supersede_series=["(unclosed", YOUR_DAY])
    front = _FakeFront([_conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400)])
    dr.run_rule(front, rule)
    assert _archived(front) == ["n4"]


def test_rule_without_series_or_with_domain_wildcard_is_inert():
    front = _FakeFront([_conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400)])
    assert dr.run_rule(front, {"email": "notifications@cfmins.org"})["checked"] == 0
    assert dr.run_rule(front, {"email": "@cfmins.org", "supersede_series": [YOUR_DAY]})["checked"] == 0
    assert front.queries == [] and front.set_calls == []


# ---- pipeline entry point -------------------------------------------------

def test_run_is_disabled_by_env(monkeypatch):
    monkeypatch.setenv("DAILY_ROLLOVER_ENABLED", "false")
    assert dr.run(_FakeFront([])) == {"disabled": True}


def test_run_skips_non_firestore_ledger(monkeypatch):
    monkeypatch.delenv("DAILY_ROLLOVER_ENABLED", raising=False)
    monkeypatch.setenv("LEDGER_BACKEND", "sqlite")
    assert "skipped" in dr.run(_FakeFront([]))


def test_run_applies_only_rules_that_have_supersede_series(monkeypatch):
    monkeypatch.delenv("DAILY_ROLLOVER_ENABLED", raising=False)
    monkeypatch.setenv("LEDGER_BACKEND", "firestore")
    from cos import ledger_firestore as lf
    monkeypatch.setattr(lf, "list_sender_rules", lambda: [
        {"email": "businessoffice@x.org", "action": "exclude"}, RULE])
    front = _FakeFront([_conv("n5", _yd(5), 500), _conv("n4", _yd(4), 400)])
    out = dr.run(front)
    assert out["archived"] == 1 and _archived(front) == ["n4"]
    assert front.queries == ["from:notifications@cfmins.org"]
