"""Tests for archive_before_cutoff.py.

Jay (2026-10-02): archive anything prior to 5/1 — meaning no message since then.
Front's search before: matches CREATION date, so every decision here is made from
the real newest message, never from the search filter.
"""
import csv

import archive_before_cutoff as ab

CUT = ab.cutoff_epoch("2026-05-01")
DAY = 86400
OLD = CUT - 30 * DAY
NEW = CUT + 30 * DAY


class _Err(Exception):
    def __init__(self, status):
        super().__init__(f"err {status}")
        self.status = status


# ── precheck ──────────────────────────────────────────────────────────────────

def test_cutoff_is_midnight_eastern():
    import datetime
    dt = datetime.datetime.fromtimestamp(CUT, datetime.timezone.utc)
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 5, 1, 4)           # 00:00 EDT


def test_unreplied_inbound_after_cutoff_is_active():
    v, _ = ab.precheck({"waiting_since": NEW, "updated_at": NEW, "assignee": None}, CUT)
    assert v == ab.ACTIVE


def test_nothing_since_cutoff_is_archived_without_reading_messages():
    v, _ = ab.precheck({"waiting_since": OLD, "updated_at": OLD, "assignee": None}, CUT)
    assert v == ab.ARCHIVE


def test_recently_updated_but_old_waiting_needs_a_messages_check():
    # updated_at is bumped by our own tags/comments, so it cannot prove activity
    v, _ = ab.precheck({"waiting_since": OLD, "updated_at": NEW, "assignee": None}, CUT)
    assert v == ab.NEED_MESSAGES


def test_other_teammates_conversations_are_listed_not_archived():
    v, why = ab.precheck({"waiting_since": OLD, "updated_at": OLD, "assignee": "deirdre@x.org"}, CUT)
    assert v == ab.OTHER_ASSIGNEE and "deirdre" in why


def test_jay_and_unassigned_are_allowed_and_extra_assignees_can_be_added():
    base = {"waiting_since": OLD, "updated_at": OLD}
    assert ab.precheck({**base, "assignee": "jay@cfmins.org"}, CUT)[0] == ab.ARCHIVE
    assert ab.precheck({**base, "assignee": "admin@cfmins.org"}, CUT)[0] == ab.OTHER_ASSIGNEE
    assert ab.precheck({**base, "assignee": "admin@cfmins.org"}, CUT,
                       allowed=ab.DEFAULT_ALLOWED + ("admin@cfmins.org",))[0] == ab.ARCHIVE


# ── decide_from_messages ──────────────────────────────────────────────────────

def _m(ts, draft=False):
    return {"created_at": ts, "is_draft": draft}


def test_all_messages_before_cutoff_archives():
    v, _, newest, d = ab.decide_from_messages([_m(OLD), _m(OLD + 5)], CUT)
    assert v == ab.ARCHIVE and newest == OLD + 5 and d is False


def test_any_message_after_cutoff_is_active():
    assert ab.decide_from_messages([_m(OLD), _m(NEW)], CUT)[0] == ab.ACTIVE


def test_a_draft_keeps_it_open_even_if_every_real_message_is_old():
    assert ab.decide_from_messages([_m(OLD), _m(NEW, draft=True)], CUT)[0] == ab.DRAFT


def test_unreadable_or_huge_or_empty_is_unknown_never_archived():
    assert ab.decide_from_messages(None, CUT)[0] == ab.UNKNOWN
    assert ab.decide_from_messages([_m(OLD)] * ab.MAX_MESSAGES, CUT)[0] == ab.UNKNOWN
    assert ab.decide_from_messages([], CUT)[0] == ab.UNKNOWN


# ── enumeration ───────────────────────────────────────────────────────────────

def test_enumeration_follows_pagination_and_reports_total(monkeypatch):
    import front_client
    pages = {
        "first": {"_total": 3, "_results": [{"id": "c1", "assignee": {"email": "jay@cfmins.org"}},
                                            {"id": "c2", "assignee": None}],
                  "_pagination": {"next": "https://api2.frontapp.com/page2"}},
        "https://api2.frontapp.com/page2": {"_results": [{"id": "c3"}], "_pagination": {}},
    }
    seen = []

    def fake_request(method, path, token, body=None, params=None):
        seen.append((path, params))
        return (pages["first"] if "search" in path else pages[path]), {}

    monkeypatch.setattr(front_client, "_request", fake_request)

    class _F:
        token = "t"

    class _NoWait(ab.Throttle):
        def wait(self):
            pass

    items, total = ab.enumerate_candidates(_F(), "inb_x", CUT, _NoWait(), say=lambda m: None)
    assert [i["conv_id"] for i in items] == ["c1", "c2", "c3"] and total == 3
    assert items[0]["assignee"] == "jay@cfmins.org" and items[1]["assignee"] is None
    assert "before%3A" in seen[0][0] and "is%3Aopen" in seen[0][0] and "inbox%3Ainb_x" in seen[0][0]
    assert seen[0][1] == {"limit": 100}


# ── execute (just-before re-check) ────────────────────────────────────────────

class _FakeFront:
    def __init__(self, conv=None, messages=None, get_error=None, set_errors=(), after="archived"):
        self.conv = conv if conv is not None else {"status": "assigned", "waiting_since": OLD,
                                                    "updated_at": OLD, "assignee": None}
        self.messages = messages
        self.get_error = get_error
        self.set_errors = list(set_errors)
        self.after = after
        self.set_calls = []

    def get_conversation(self, cid):
        if self.get_error:
            raise self.get_error
        return {"status": self.after} if self.set_calls else self.conv

    def get_conversation_messages(self, cid, max_pages=6):
        return self.messages

    def set_status(self, cid, status):
        if self.set_errors:
            raise self.set_errors.pop(0)
        self.set_calls.append((cid, status))


class _NoWait(ab.Throttle):
    def wait(self):
        pass


def _exec(front, verify=False):
    p = {"conv_id": "cnv_1", "inbox": "inb_x", "subject": "S"}
    row = {"prior_status": "", "assignee": ""}
    res = ab._execute_one(front, p, row, CUT, ab.DEFAULT_ALLOWED, _NoWait(), verify=verify)
    return res, row


def test_execute_archives_and_records_prior_state_for_rollback():
    front = _FakeFront()
    res, row = _exec(front)
    assert res == "archived" and front.set_calls == [("cnv_1", "archived")]
    assert row["prior_status"] == "assigned"


def test_execute_skips_something_that_got_new_mail_since_the_plan():
    front = _FakeFront({"status": "assigned", "waiting_since": NEW, "updated_at": NEW, "assignee": None})
    res, _ = _exec(front)
    assert res.startswith("skipped") and front.set_calls == []


def test_execute_rereads_messages_when_updated_at_moved():
    front = _FakeFront({"status": "unassigned", "waiting_since": OLD, "updated_at": NEW, "assignee": None},
                       messages=[_m(NEW)])
    res, _ = _exec(front)
    assert res.startswith("skipped") and front.set_calls == []
    front2 = _FakeFront({"status": "unassigned", "waiting_since": OLD, "updated_at": NEW, "assignee": None},
                        messages=[_m(OLD)])
    assert _exec(front2)[0] == "archived"


def test_execute_leaves_already_archived_and_gone_alone():
    assert _exec(_FakeFront({"status": "archived"}))[0] == "already-archived"
    assert _exec(_FakeFront(get_error=_Err(404)))[0] == "gone"


def test_execute_errors_are_reported_not_hidden():
    assert _exec(_FakeFront(get_error=_Err(500)))[0].startswith("error")
    assert _exec(_FakeFront(set_errors=[_Err(500)]))[0].startswith("error")


def test_execute_retries_a_rate_limit():
    front = _FakeFront(set_errors=[_Err(429), _Err(429)])
    assert _exec(front)[0] == "archived"


def test_verify_catches_a_patch_that_did_not_take():
    assert _exec(_FakeFront(after="assigned"), verify=True)[0].startswith("error")


# ── plan + rollback files ─────────────────────────────────────────────────────

def test_rollback_reopens_only_archived_rows(monkeypatch, tmp_path):
    p = tmp_path / "exec.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ab.EXEC_FIELDS)
        w.writeheader()
        for cid, res in (("a", "archived"), ("b", "skipped: x"), ("c", "already-archived"), ("d", "archived")):
            w.writerow({"conv_id": cid, "result": res})
    monkeypatch.setattr(ab.Throttle, "wait", lambda self: None)
    front = _FakeFront()
    assert ab.rollback(str(p), execute=False, front=front) == 0 and front.set_calls == []
    assert ab.rollback(str(p), execute=True, front=front) == 0
    assert front.set_calls == [("a", "open"), ("d", "open")]


# ── transient-error retry (a 30s Front read timeout must not kill a long pass) ──

def test_call_with_retry_survives_timeouts_and_gateway_errors(monkeypatch):
    from cos import front_archive
    monkeypatch.setattr(front_archive.time, "sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("The read operation timed out")
        if len(calls) == 2:
            raise _Err(503)
        if len(calls) == 3:
            raise _Err(429)
        return "ok"

    assert front_archive.call_with_retry(flaky) == "ok" and len(calls) == 4


def test_call_with_retry_does_not_retry_real_errors(monkeypatch):
    from cos import front_archive
    monkeypatch.setattr(front_archive.time, "sleep", lambda s: None)
    calls = []

    def bad():
        calls.append(1)
        raise _Err(403)

    try:
        front_archive.call_with_retry(bad)
        assert False
    except _Err:
        pass
    assert len(calls) == 1


def test_call_with_retry_gives_up_eventually(monkeypatch):
    from cos import front_archive
    monkeypatch.setattr(front_archive.time, "sleep", lambda s: None)

    def always():
        raise TimeoutError("x")

    try:
        front_archive.call_with_retry(always, attempts=3)
        assert False
    except TimeoutError:
        pass
