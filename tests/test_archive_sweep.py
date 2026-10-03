"""Tests for the self-healing archive sweep and the paths that feed it.

Jay (2026-10-02): every closed loop gets archived in Front, except where the
sender wrote after we closed it (leave open, list it). Closing a Front loop must
mark it pending; the sweep finishes the job.
"""
import openpyxl

from cos import archive_sweep, front_archive

RESOLVED = "2026-09-20T12:00:00Z"
RESOLVED_EPOCH = front_archive._iso_to_epoch(RESOLVED)


class _Err(Exception):
    def __init__(self, status):
        super().__init__(f"err {status}")
        self.status = status


class _FakeFront:
    def __init__(self, convs=None, get_errors=None, set_errors=None):
        self.convs = convs or {}
        self.get_errors = {k: list(v) for k, v in (get_errors or {}).items()}
        self.set_errors = {k: list(v) for k, v in (set_errors or {}).items()}
        self.set_calls = []

    def get_conversation(self, cid):
        if self.get_errors.get(cid):
            raise self.get_errors[cid].pop(0)
        return self.convs.get(cid, {"status": "assigned"})

    def set_status(self, cid, status):
        if self.set_errors.get(cid):
            raise self.set_errors[cid].pop(0)
        self.set_calls.append((cid, status))


def _loop(n, **kw):
    d = {"id": f"L{n}", "num": n, "source_ref": f"cnv_{n}", "resolved_at": RESOLVED}
    d.update(kw)
    return d


def _sweep(front, loops, **kw):
    stamped, held = [], []
    counts = archive_sweep.sweep(front, loops, stamp=stamped.append, hold=held.append,
                                 printer=lambda m: None, **kw)
    return counts, stamped, held


# ── sweep ─────────────────────────────────────────────────────────────────────

def test_open_conversation_is_archived_and_stamped():
    front = _FakeFront()
    counts, stamped, held = _sweep(front, [_loop(1)])
    assert counts["archived"] == 1 and stamped == ["L1"] and held == []
    assert front.set_calls == [("cnv_1", "archived")]


def test_already_archived_is_stamped_only():
    front = _FakeFront({"cnv_1": {"status": "archived"}})
    counts, stamped, _ = _sweep(front, [_loop(1)])
    assert counts["stamped"] == 1 and stamped == ["L1"] and front.set_calls == []


def test_404_is_stamped():
    front = _FakeFront(get_errors={"cnv_1": [_Err(404)]})
    counts, stamped, _ = _sweep(front, [_loop(1)])
    assert counts["stamped"] == 1 and stamped == ["L1"]


def test_sender_wrote_after_close_is_held_not_archived():
    front = _FakeFront({"cnv_1": {"status": "assigned", "waiting_since": RESOLVED_EPOCH + 600}})
    counts, stamped, held = _sweep(front, [_loop(1)])
    assert counts["held"] == 1 and held == ["L1"] and stamped == []
    assert front.set_calls == []


def test_no_resolution_time_means_hold_not_a_guess():
    front = _FakeFront()
    counts, stamped, held = _sweep(front, [_loop(1, resolved_at="")])
    assert counts["held"] == 1 and held == ["L1"] and front.set_calls == []


def test_rate_limit_is_retried_inside_the_sweep():
    front = _FakeFront(get_errors={"cnv_1": [_Err(429)]}, set_errors={"cnv_1": [_Err(429), _Err(429)]})
    counts, stamped, _ = _sweep(front, [_loop(1)])
    assert counts["archived"] == 1 and stamped == ["L1"] and counts["errors"] == 0


def test_failure_is_counted_and_not_stamped_so_it_retries_next_run():
    front = _FakeFront(set_errors={"cnv_1": [_Err(500)]})
    counts, stamped, _ = _sweep(front, [_loop(1), _loop(2)])
    assert counts["errors"] == 1 and counts["archived"] == 1
    assert stamped == ["L2"]                       # L1 stays pending


def test_dry_run_changes_nothing():
    front = _FakeFront({"cnv_2": {"status": "archived"},
                        "cnv_3": {"status": "assigned", "waiting_since": RESOLVED_EPOCH + 5}})
    counts, stamped, held = _sweep(front, [_loop(1), _loop(2), _loop(3)], dry_run=True)
    assert counts["archived"] == 1 and counts["stamped"] == 1 and counts["held"] == 1
    assert front.set_calls == [] and stamped == [] and held == []


def test_limit_caps_the_work_per_run():
    front = _FakeFront()
    counts, stamped, _ = _sweep(front, [_loop(n) for n in range(1, 8)], limit=3)
    assert counts["checked"] == 3 and len(stamped) == 3


# ── pending query ─────────────────────────────────────────────────────────────

class _Doc:
    def __init__(self, i, d):
        self.id, self._d = i, d

    def to_dict(self):
        return dict(self._d)


class _Query:
    def __init__(self, docs):
        self._docs = docs

    def where(self, filter=None):
        return _Query([d for d in self._docs
                       if d.to_dict().get(filter.field_path) == filter.value])

    def stream(self):
        return iter(self._docs)


class _PendingDb:
    def __init__(self, rows):
        self._docs = [_Doc(i, d) for i, d in rows.items()]

    def collection(self, name):
        return _Query(self._docs)


def test_pending_loops_selects_only_pending_closed_front_loops_without_holds():
    rows = {
        "a": {"channel": "front", "status": "done", "front_archived": False, "num": 1, "resolved_at": "2026-09-02T00:00:00Z"},
        "b": {"channel": "front", "status": "dropped", "front_archived": False, "num": 2, "resolved_at": "2026-09-01T00:00:00Z"},
        "c": {"channel": "front", "status": "done", "front_archived": True, "num": 3},
        "d": {"channel": "front", "status": "done", "num": 4},                         # legacy: no field
        "e": {"channel": "outlook", "status": "done", "front_archived": False, "num": 5},
        "f": {"channel": "front", "status": "open", "front_archived": False, "num": 6},
        "g": {"channel": "front", "status": "done", "front_archived": False, "num": 7, "archive_hold": "2026-10-02T00:00:00Z"},
    }
    got = archive_sweep.pending_loops(_PendingDb(rows))
    assert [l["num"] for l in got] == [2, 1]                                          # oldest first


def test_run_is_a_noop_off_firestore(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "sqlite")
    assert "skipped" in archive_sweep.run(_FakeFront())


def test_run_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("ARCHIVE_SWEEP_ENABLED", "false")
    assert archive_sweep.run(_FakeFront()) == {"disabled": True}


# ── resolve_loop marks Front loops pending ────────────────────────────────────

class _Snap:
    def __init__(self, d):
        self._d = d
        self.exists = d is not None

    def to_dict(self):
        return dict(self._d)

    id = "L1"


class _Ref:
    def __init__(self, store):
        self.store = store

    def get(self):
        return _Snap(self.store)

    def update(self, upd):
        self.store.update(upd)


class _LedgerDb:
    def __init__(self, loop):
        self.loop = loop
        self.feedback = []

    def collection(self, name):
        db = self
        if name == "loops":
            class _C:
                def document(self, _id):
                    return _Ref(db.loop)
            return _C()

        class _F:
            def add(self, doc):
                db.feedback.append(doc)
        return _F()


def test_closing_a_front_loop_marks_it_pending_with_a_resolution_time(monkeypatch):
    from cos import ledger_firestore as lf
    db = _LedgerDb({"channel": "front", "status": "open", "num": 9})
    monkeypatch.setattr(lf, "_db", lambda: db)
    lf.resolve_loop("L1", "done")
    assert db.loop["status"] == "done"
    assert db.loop["front_archived"] is False
    assert db.loop["resolved_at"] == db.loop["last_reviewed"]


def test_closing_a_non_front_loop_is_untouched(monkeypatch):
    from cos import ledger_firestore as lf
    db = _LedgerDb({"channel": "outlook", "status": "open", "num": 9})
    monkeypatch.setattr(lf, "_db", lambda: db)
    lf.resolve_loop("L1", "done")
    assert "front_archived" not in db.loop and "resolved_at" not in db.loop


def test_reopening_does_not_mark_pending(monkeypatch):
    from cos import ledger_firestore as lf
    db = _LedgerDb({"channel": "front", "status": "done"})
    monkeypatch.setattr(lf, "_db", lambda: db)
    lf.resolve_loop("L1", "open")
    assert "front_archived" not in db.loop


# ── importer: visible failures ────────────────────────────────────────────────

class _ImpLedger:
    def __init__(self):
        self.calls = []

    def get_loop(self, loop_id):
        return {"id": loop_id, "channel": "front", "source_ref": f"cnv_{loop_id}"}

    def resolve_loop(self, loop_id, status, reason=None):
        self.calls.append(("resolve", loop_id, status))

    def patch_loop(self, loop_id, **kw):
        self.calls.append(("patch", loop_id, tuple(sorted(kw))))

    def snooze_loop(self, loop_id, until):
        self.calls.append(("snooze", loop_id))


def _wb(*rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Triage"
    ws.append(["#", "Triage Action", "Notes", "_id"])
    for r in rows:
        ws.append(list(r))
    return wb


def test_unknown_action_is_reported_as_an_error_not_silently_skipped(monkeypatch):
    import cos_triage_import as imp
    monkeypatch.setattr(imp, "ledger", _ImpLedger())
    monkeypatch.setattr(imp, "_archive_in_front", lambda *a, **k: True)
    result = imp._run_triage_sheet(_wb((1, "frobnicate", "", "L1")))
    assert result["errored"] == 1 and result["unknown"] == 1 and result["skipped"] == 0


def test_failed_archive_is_counted_and_left_pending_for_the_sweep(monkeypatch):
    import cos_triage_import as imp
    fake = _ImpLedger()
    monkeypatch.setattr(imp, "ledger", fake)
    monkeypatch.setattr(imp, "_archive_in_front", lambda *a, **k: False)
    result = imp._run_triage_sheet(_wb((1, "done", "", "L1")))
    assert result["done"] == 1 and result["archive_failed"] == 1 and result["errored"] == 0
    assert ("patch", "L1", ("front_archived",)) not in fake.calls          # stays pending


def test_successful_archive_is_stamped(monkeypatch):
    import cos_triage_import as imp
    fake = _ImpLedger()
    monkeypatch.setattr(imp, "ledger", fake)
    monkeypatch.setattr(imp, "_archive_in_front", lambda *a, **k: True)
    result = imp._run_triage_sheet(_wb((1, "done", "", "L1"), (2, "drop", "", "L2")))
    assert ("patch", "L1", ("front_archived",)) in fake.calls
    assert ("patch", "L2", ("front_archived",)) in fake.calls
    assert result["archive_failed"] == 0
