"""Tests for the AI-free bulk/spam pre-filter (precision-first)."""
import time

from modules.prefilter import looks_like_bulk


def _conv(cid="cnv_1", subject="Newsletter"):
    return {"id": cid, "subject": subject, "status": "open"}


def _msg(inbound=True, email="someone@example.org", text="Hello Jay,\n\nLet's meet.",
         ago_hours=1.0):
    return {"is_inbound": inbound, "created_at": time.time() - ago_hours * 3600,
            "author": {"email": email}, "text": text, "to": [{"handle": "jay@cfmins.org"}]}


def test_marketing_esp_domain_is_bulk():
    msgs = [_msg(email="bounce@mail.ccsend.com", text="Our spring appeal is live!")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert is_bulk and "marketing-platform" in reason


def test_mailer_daemon_bounce_is_bulk():
    msgs = [_msg(email="mailer-daemon@cfmins.org", text="Delivery failed")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert is_bulk and "system/bounce" in reason


def test_plain_human_email_is_not_bulk():
    msgs = [_msg(email="hannah@claggettcenter.org",
                 text="Hi Jay, here is the operations spreadsheet you asked for.")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert not is_bulk and reason is None


def test_unsubscribe_footer_alone_is_not_bulk():
    # A footer word is NOT enough — too many legit emails carry one.
    msgs = [_msg(email="info@msde.maryland.gov",
                 text="Grant opportunity details...\n\nUnsubscribe | Manage preferences")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert not is_bulk and reason is None


def test_transactional_noreply_with_unsubscribe_not_flagged():
    # Regression guard: real EDOM payroll receipt — no-reply sender + unsubscribe
    # footer — must NOT be skipped.
    msgs = [_msg(email="no-reply@paychex.com",
                 text="Payroll direct deposit receipt for Episcopal Diocese of "
                      "Maryland.\n\nTo unsubscribe from these notifications click here.")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert not is_bulk and reason is None


def test_general_purpose_esp_not_flagged():
    # SendGrid etc. carry transactional mail too — must NOT be blanket-skipped.
    msgs = [_msg(email="receipts@em123.sendgrid.net", text="Your receipt")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert not is_bulk and reason is None


def test_engaged_thread_never_prefiltered():
    msgs = [
        _msg(inbound=True, email="news@mcsv.net", text="appeal", ago_hours=5),
        _msg(inbound=False, email="jay@cfmins.org", text="Thanks", ago_hours=2),
    ]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert not is_bulk and reason is None


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("SPAM_PREFILTER", "false")
    msgs = [_msg(email="news@mcsv.net", text="appeal")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert not is_bulk and reason is None


# ── Calendar meeting-response notifications ──────────────────────────────────

from modules.prefilter import is_calendar_response


def test_calendar_responses_are_detected():
    for prefix in ("Accepted", "Declined", "Tentative", "Tentatively accepted",
                   "New time proposed"):
        assert is_calendar_response(_conv(subject=f"{prefix}: Finance Sync Jun 5"))


def test_normal_subject_is_not_a_calendar_response():
    assert not is_calendar_response(_conv(subject="Re: Finance Sync agenda"))
    assert not is_calendar_response(_conv(subject="Payment accepted: invoice 1023"))


def test_calendar_response_respects_disable_flag(monkeypatch):
    monkeypatch.setenv("SPAM_PREFILTER", "false")
    assert not is_calendar_response(_conv(subject="Declined: Budget review"))


# --- sender_rule_skip: real Front inbound shape (author=None) -------------------
# Regression for 2026-09-22: _sender() read only `author`, which Front leaves
# null on inbound email, so no exclude/fyi rule ever fired before the AI call.
from modules import prefilter as _pf


def _inbound(from_handle, author=None):
    return {"is_inbound": True, "created_at": 1, "author": author,
            "recipients": [{"handle": "jay@cfmins.org", "role": "to"},
                           {"handle": from_handle, "role": "from"}]}


def test_sender_read_from_recipients_when_author_is_null():
    assert _pf._sender(_inbound("Notifications@CFMins.org")) == "notifications@cfmins.org"


def test_teammate_author_still_wins():
    msg = _inbound("x@y.com", author={"email": "jay@cfmins.org"})
    assert _pf._sender(msg) == "jay@cfmins.org"


def test_exclude_rule_fires_for_real_inbound_shape(monkeypatch):
    from cos import extract as cos_extract
    monkeypatch.setattr(cos_extract, "sender_rule_action",
                        lambda e: {"action": "exclude"} if e == "notifications@cfmins.org" else None)
    skip, rule, sender = _pf.sender_rule_skip({"subject": "CFM Daily Processing"},
                                             [_inbound("notifications@cfmins.org")])
    assert skip and rule["action"] == "exclude" and sender == "notifications@cfmins.org"


def test_unruled_inbound_sender_not_skipped(monkeypatch):
    from cos import extract as cos_extract
    monkeypatch.setattr(cos_extract, "sender_rule_action", lambda e: None)
    skip, _, sender = _pf.sender_rule_skip({"subject": "hi"}, [_inbound("person@parish.org")])
    assert not skip and sender == "person@parish.org"
