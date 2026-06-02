"""Tests for calendar integration (cache, conflicts, prep loops, briefing Today)."""
import datetime
import importlib
import time

import pytest


@pytest.fixture()
def mods(tmp_path, monkeypatch):
    monkeypatch.setenv("COS_DB_PATH", str(tmp_path / "cos.db"))
    monkeypatch.setenv("COS_OWNER_EMAILS", "jay@cfmins.org")
    monkeypatch.setenv("COS_TIMEZONE", "UTC")
    from cos import ledger as ledger_mod
    importlib.reload(ledger_mod)
    ledger_mod.init_db()
    from cos import extract as extract_mod
    importlib.reload(extract_mod)
    from cos import calendars as cal_mod
    importlib.reload(cal_mod)
    from cos import briefing as briefing_mod
    importlib.reload(briefing_mod)
    return ledger_mod, cal_mod, briefing_mod


def _today_at(hour, minute=0):
    d = datetime.datetime.now(datetime.timezone.utc).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _raw(subject, start, end, *, organizer="jay@cfmins.org", attendees=("mary@dio.org",),
         ev_id=None, all_day=False):
    return {"id": ev_id or subject, "subject": subject,
            "start": {"dateTime": start}, "end": {"dateTime": end},
            "organizer": {"emailAddress": {"address": organizer}},
            "attendees": [{"emailAddress": {"address": a}} for a in attendees],
            "isAllDay": all_day, "webLink": "https://outlook.test/e"}


def test_event_normalization_handles_graph_shape(mods):
    _, cal, _ = mods
    ev = cal.event_from_outlook(_raw("Staff", _today_at(10), _today_at(11)))
    assert ev["subject"] == "Staff"
    assert ev["organizer"] == "jay@cfmins.org"
    assert ev["attendees"] == ["mary@dio.org"]
    assert ev["start_at"].endswith("Z")


def test_ingest_and_events_for_day(mods):
    ledger, cal, _ = mods
    cal.ingest_events([_raw("Staff", _today_at(10), _today_at(11))])
    events = cal.events_for_day()
    assert len(events) == 1
    assert events[0]["subject"] == "Staff"


def test_ingest_is_idempotent(mods):
    ledger, cal, _ = mods
    cal.ingest_events([_raw("Staff", _today_at(10), _today_at(11), ev_id="x")])
    cal.ingest_events([_raw("Staff (moved)", _today_at(12), _today_at(13), ev_id="x")])
    events = cal.events_for_day()
    assert len(events) == 1
    assert events[0]["subject"] == "Staff (moved)"


def test_detect_conflicts(mods):
    _, cal, _ = mods
    events = [cal.event_from_outlook(_raw("A", _today_at(10), _today_at(11), ev_id="a")),
              cal.event_from_outlook(_raw("B", _today_at(10, 30), _today_at(11, 30), ev_id="b")),
              cal.event_from_outlook(_raw("C", _today_at(14), _today_at(15), ev_id="c"))]
    conflicts = cal.detect_conflicts(events)
    assert conflicts == {"a", "b"}


def test_prep_loop_when_you_owe_an_attendee(mods):
    ledger, cal, _ = mods
    # An existing loop: you owe Mary.
    ledger.upsert_loop(direction="i_owe", counterparty="Mary", counterparty_email="mary@dio.org",
                       summary="send agenda", channel="front", source_ref="cnv_1")
    cal.ingest_events([_raw("Vestry call", _today_at(23), _today_at(23, 30),
                            attendees=("mary@dio.org",))])
    created = cal.sync_prep_loops(cal.events_for_day())
    assert len(created) == 1
    assert "you owe Mary" in created[0]["summary"]
    assert created[0]["channel"] == "calendar"


def test_prep_loop_when_you_organize(mods):
    ledger, cal, _ = mods
    cal.ingest_events([_raw("Planning", _today_at(23), _today_at(23, 30),
                            organizer="jay@cfmins.org", attendees=("a@x.org", "b@y.org"))])
    created = cal.sync_prep_loops(cal.events_for_day())
    assert len(created) == 1
    assert created[0]["counterparty"] == "Planning"
    assert "Meeting prep" in created[0]["summary"]


def test_no_prep_loop_for_solo_meeting_you_dont_owe(mods):
    ledger, cal, _ = mods
    cal.ingest_events([_raw("1:1 reminder", _today_at(23), _today_at(23, 30),
                            organizer="someone@else.org", attendees=("jay@cfmins.org",))])
    assert cal.sync_prep_loops(cal.events_for_day()) == []


def test_expire_past_calendar_loops(mods):
    ledger, cal, _ = mods
    loop = ledger.upsert_loop(direction="i_owe", counterparty="m", summary="Prep",
                              channel="calendar", source_ref="ev1",
                              due_at="2000-01-01T10:00:00Z")
    cal.expire_past_calendar_loops()
    assert ledger.get_loop(loop["id"])["status"] == "done"


def test_briefing_today_section(mods):
    ledger, cal, briefing = mods
    cal.ingest_events([_raw("Staff meeting", _today_at(10), _today_at(11))])
    sections = briefing.gather()
    subject, body = briefing.render(sections, date="2026-06-02")
    assert "1 meetings" in subject
    assert "📅 Today (1)" in body
    assert "Staff meeting" in body
    assert "10:00–11:00" in body  # UTC in tests


def test_millisecond_timestamps_parse(mods):
    _, cal, _ = mods
    ev = cal.event_from_outlook(_raw("Mtg", "2026-06-02T17:00:00.000Z", "2026-06-02T18:00:00.000Z"))
    assert ev["start_at"] == "2026-06-02T17:00:00Z"  # fractional seconds stripped
    assert cal._to_epoch(ev["start_at"]) > 0


def test_local_time_conversion(mods, monkeypatch):
    _, cal, _ = mods
    monkeypatch.setenv("COS_TIMEZONE", "America/New_York")
    # 14:00 UTC is 10:00 in New York (EDT, summer)
    assert cal.local_hhmm("2026-06-02T14:00:00Z") == "10:00"
