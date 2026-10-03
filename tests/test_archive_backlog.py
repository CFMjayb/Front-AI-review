"""Tests for the closed-loop archive backlog: classification + newer-inbound guard,
429 retry, process_loop (dry-run vs execute), selection, and rollback.

Context (2026-10-02): closed loops were left open in Front. Jay's rules: archive
them all (done, FYI-expired, reply-closed), but if the sender wrote AFTER the loop
was closed, leave it open and list it.
"""
import csv

import archive_closed_loops_backlog as bk
from cos import front_archive
from cos.front_archive import ARCHIVE, HOLD, STAMP

RESOLVED = "2026-09-20T12:00:00Z"
RESOLVED_EPOCH = front_archive._iso_to_epoch(RESOLVED)


class _Err(Exception):
    def __init__(self, status, msg="err"):
        super().__init__(msg)
        self.status = status


class _FakeFront:
    def __init__(self, conv=None, get_errors=(), set_errors=(), after_status="archived"):
        self.conv = conv if conv is not None else {"status": "assigned"}
        self.get_errors = list(get_errors)
        self.set_errors = list(set_errors)
        self.after_status = after_status
        self.set_calls = []
        self.get_calls = 0

    def get_conversation(self, cid):
        self.get_calls += 1
        if self.get_errors:
            raise self.get_errors.pop(0)
        if self.set_calls and self.get_calls > 1:        # a post-PATCH verification read
            return {"status": self.after_status}
        return self.conv

    def set_status(self, cid, status):
        if self.set_errors:
            raise self.set_errors.pop(0)
        self.set_calls.append((cid, status))


def _loop(**kw):
    base = {"id": "L1", "num": 7, "source_ref": "cnv_1", "channel": "front",
            "status": "done", "counterparty": "Someone", "summary": "Do a thing",
            "_resolved_ts": RESOLVED, "_group": "done"}
    base.update(kw)
    return base


# ── classification + guard ────────────────────────────────────────────────────

def test_open_with_nothing_new_is_archived():
    assert front_archive.classify_for_archive({"status": "assigned"}, RESOLVED) == ARCHIVE


def test_waiting_since_before_resolution_is_not_new():
    conv = {"status": "unassigned", "waiting_since": RESOLVED_EPOCH - 3600}
    assert front_archive.classify_for_archive(conv, RESOLVED) == ARCHIVE


def test_waiting_since_after_resolution_is_held():
    conv = {"status": "assigned", "waiting_since": RESOLVED_EPOCH + 3600}
    assert front_archive.classify_for_archive(conv, RESOLVED) == HOLD


def test_already_archived_deleted_or_gone_is_stamp_only():
    for st in ("archived", "deleted", "spam"):
        assert front_archive.classify_for_archive({"status": st}, RESOLVED) == STAMP
    assert front_archive.classify_for_archive(None, RESOLVED) == STAMP


def test_newer_activity_on_an_already_archived_thread_is_still_stamp():
    conv = {"status": "archived", "waiting_since": RESOLVED_EPOCH + 3600}
    assert front_archive.classify_for_archive(conv, RESOLVED) == STAMP


# ── 429 retry ─────────────────────────────────────────────────────────────────

def test_429_is_retried_then_succeeds():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _Err(429)
        return "ok"

    assert front_archive.call_with_429_retry(flaky) == "ok"
    assert len(calls) == 3


def test_non_429_error_is_not_retried():
    calls = []

    def boom():
        calls.append(1)
        raise _Err(500)

    try:
        front_archive.call_with_429_retry(boom)
        assert False, "should have raised"
    except _Err:
        pass
    assert len(calls) == 1


def test_persistent_429_eventually_raises():
    def always():
        raise _Err(429)

    try:
        front_archive.call_with_429_retry(always, attempts=3)
        assert False, "should have raised"
    except _Err:
        pass


def test_archive_conversation_survives_a_rate_limit():
    front = _FakeFront(conv={"status": "assigned"}, get_errors=[_Err(429)], set_errors=[_Err(429)])
    ok = front_archive.archive_conversation(front, "cnv_1", printer=lambda m: None)
    assert ok is True
    assert front.set_calls == [("cnv_1", "archived")]


# ── process_loop ──────────────────────────────────────────────────────────────

def _run(front, loop=None, **kw):
    stamped = []
    row = bk.process_loop(front, loop or _loop(), stamp=stamped.append, **kw)
    return row, stamped


def test_dry_run_writes_nothing():
    front = _FakeFront()
    row, stamped = _run(front, execute=False, verify=False)
    assert row["verdict"] == ARCHIVE and row["result"] == "dry-run"
    assert front.set_calls == [] and stamped == []


def test_execute_archives_and_stamps():
    front = _FakeFront()
    row, stamped = _run(front, execute=True, verify=False)
    assert row["result"] == "archived"
    assert front.set_calls == [("cnv_1", "archived")]
    assert stamped == ["L1"]
    assert row["front_status"] == "assigned"          # prior status recorded for rollback


def test_execute_holds_when_sender_wrote_after_close():
    front = _FakeFront(conv={"status": "assigned", "waiting_since": RESOLVED_EPOCH + 60})
    row, stamped = _run(front, execute=True, verify=False)
    assert row["result"] == "held" and row["verdict"] == HOLD
    assert front.set_calls == [] and stamped == []


def test_already_archived_is_stamped_without_a_patch():
    front = _FakeFront(conv={"status": "archived"})
    row, stamped = _run(front, execute=True, verify=False)
    assert row["result"] == "stamped"
    assert front.set_calls == [] and stamped == ["L1"]


def test_404_is_stamped():
    front = _FakeFront(get_errors=[_Err(404)])
    row, stamped = _run(front, execute=True, verify=False)
    assert row["result"] == "stamped" and stamped == ["L1"]


def test_open_without_a_resolution_time_is_held_not_guessed():
    front = _FakeFront()
    row, stamped = _run(front, _loop(_resolved_ts=""), execute=True, verify=False)
    assert row["result"] == "held"
    assert front.set_calls == [] and stamped == []


def test_read_error_leaves_everything_alone():
    front = _FakeFront(get_errors=[_Err(500)])
    row, stamped = _run(front, execute=True, verify=False)
    assert row["result"].startswith("error") and stamped == []


def test_archive_failure_is_not_stamped():
    front = _FakeFront(set_errors=[_Err(500)])
    row, stamped = _run(front, execute=True, verify=False)
    assert row["result"].startswith("error") and stamped == []


def test_verify_catches_a_patch_that_did_not_take():
    front = _FakeFront(after_status="assigned")
    row, stamped = _run(front, execute=True, verify=True)
    assert row["result"].startswith("error") and stamped == []


# ── selection ────────────────────────────────────────────────────────────────

class _Doc:
    def __init__(self, id_, data):
        self.id, self._d = id_, data

    def to_dict(self):
        return dict(self._d)


class _Coll:
    def __init__(self, docs, updates):
        self._docs, self._updates = docs, updates

    def stream(self):
        return iter(self._docs)

    def document(self, id_):
        coll = self

        class _Ref:
            def update(self, data):
                coll._updates.append((id_, data))
        return _Ref()


class _FakeDb:
    project = bk.LEDGER_PROJECT

    def __init__(self, loops, feedback):
        self.updates = []
        self._c = {"loops": [_Doc(i, d) for i, d in loops.items()],
                   "feedback": [_Doc(f"f{n}", d) for n, d in enumerate(feedback)]}

    def collection(self, name):
        return _Coll(self._c[name], self.updates)


def _db():
    loops = {
        "a": {"channel": "front", "status": "done", "source_ref": "c1", "num": 1},
        "b": {"channel": "front", "status": "dropped", "source_ref": "c2", "num": 2},
        "c": {"channel": "front", "status": "done", "source_ref": "c3", "num": 3, "front_archived": True},
        "d": {"channel": "outlook", "status": "done", "source_ref": "m4", "num": 4},
        "e": {"channel": "front", "status": "open", "source_ref": "c5", "num": 5},
        "f": {"channel": "front", "status": "done", "source_ref": "c6", "num": 6, "deferred": True},
    }
    fb = [
        {"loop_id": "a", "action": "done", "ts": "2026-09-01T00:00:00Z"},
        {"loop_id": "a", "action": "done", "ts": "2026-09-05T00:00:00Z"},     # latest wins
        {"loop_id": "b", "action": "dropped", "ts": "2026-09-02T00:00:00Z", "reason": bk.FYI_REASON},
        {"loop_id": "f", "action": "done", "ts": "2026-09-03T00:00:00Z"},
    ]
    return _FakeDb(loops, fb)


def test_selection_picks_only_unstamped_closed_front_loops():
    got = bk.select_loops(_db())
    assert [l["num"] for l in got] == [1, 2, 6]                       # deferred loop included
    by = {l["num"]: l for l in got}
    assert by[1]["_resolved_ts"] == "2026-09-05T00:00:00Z"
    assert by[1]["_group"] == "done" and by[2]["_group"] == "fyi"


def test_scope_filters():
    assert [l["num"] for l in bk.select_loops(_db(), "fyi")] == [2]
    assert [l["num"] for l in bk.select_loops(_db(), "done")] == [1, 6]


# ── rollback ─────────────────────────────────────────────────────────────────

def test_rollback_reopens_only_archived_rows(tmp_path):
    p = tmp_path / "r.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=bk.CSV_FIELDS)
        w.writeheader()
        for lid, res in (("a", "archived"), ("b", "stamped"), ("c", "held"), ("d", "archived")):
            w.writerow({"loop_id": lid, "num": lid, "source_ref": f"cnv_{lid}", "result": res})
    front, db = _FakeFront(), _FakeDb({}, [])
    assert bk.rollback(str(p), execute=False, front=front, db=db) == 0
    assert front.set_calls == []                                      # dry run
    bk.Throttle.wait = lambda self: None                              # no sleeping in tests
    assert bk.rollback(str(p), execute=True, front=front, db=db) == 0
    assert front.set_calls == [("cnv_a", "open"), ("cnv_d", "open")]
    assert db.updates == [("a", {"front_archived": False}), ("d", {"front_archived": False})]
