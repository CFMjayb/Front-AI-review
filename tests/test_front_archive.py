"""Tests for cos/front_archive.py — the shared archive-on-removal path.

Regression coverage for 2026-08-18: two retirement scripts took loops off the
triage list (Firestore only) without archiving the underlying Front
conversation, so the email stayed open and unread while the row disappeared
from every sheet. Jay caught it directly. This is the fix everything now shares.
"""
from cos import front_archive


class _FakeFront:
    def __init__(self, status="assigned", get_error=None, set_error=None):
        self._status = status
        self._get_error = get_error
        self._set_error = set_error
        self.set_status_calls = []

    def get_conversation(self, conv_id):
        if self._get_error:
            raise self._get_error
        return {"status": self._status}

    def set_status(self, conv_id, status):
        if self._set_error:
            raise self._set_error
        self.set_status_calls.append((conv_id, status))


class _NotFound(Exception):
    status = 404


def test_open_conversation_gets_archived():
    front = _FakeFront(status="assigned")
    ok = front_archive.archive_conversation(front, "cnv_1", printer=lambda m: None)
    assert ok is True
    assert front.set_status_calls == [("cnv_1", "archived")]


def test_already_archived_is_left_alone():
    front = _FakeFront(status="archived")
    ok = front_archive.archive_conversation(front, "cnv_1", printer=lambda m: None)
    assert ok is True
    assert front.set_status_calls == [], "no PATCH needed when Front already agrees"


def test_404_treated_as_already_gone():
    front = _FakeFront(get_error=_NotFound())
    ok = front_archive.archive_conversation(front, "cnv_1", printer=lambda m: None)
    assert ok is True
    assert front.set_status_calls == []


def test_other_read_error_does_not_claim_success():
    front = _FakeFront(get_error=RuntimeError("network blip"))
    ok = front_archive.archive_conversation(front, "cnv_1", printer=lambda m: None)
    assert ok is False


def test_archive_write_failure_does_not_claim_success():
    front = _FakeFront(status="assigned", set_error=RuntimeError("Front 500"))
    ok = front_archive.archive_conversation(front, "cnv_1", printer=lambda m: None)
    assert ok is False


def test_no_source_ref_is_a_no_op():
    front = _FakeFront()
    assert front_archive.archive_conversation(front, "", printer=lambda m: None) is False
    assert front.set_status_calls == []


def test_archive_loop_skips_non_front_channels():
    front = _FakeFront(status="assigned")
    loop = {"channel": "outlook", "source_ref": "msg_1"}
    assert front_archive.archive_loop(front, loop, printer=lambda m: None) is False
    assert front.set_status_calls == []


def test_archive_loop_skips_none():
    front = _FakeFront()
    assert front_archive.archive_loop(front, None, printer=lambda m: None) is False
