"""Tests for the AI-free bulk/spam pre-filter."""
import time

from modules.prefilter import looks_like_bulk


def _conv(cid="cnv_1", subject="Newsletter"):
    return {"id": cid, "subject": subject, "status": "open"}


def _msg(inbound=True, email="someone@example.org", text="Hello Jay,\n\nLet's meet.",
         ago_hours=1.0):
    return {"is_inbound": inbound, "created_at": time.time() - ago_hours * 3600,
            "author": {"email": email}, "text": text, "to": [{"handle": "jay@cfmins.org"}]}


def test_newsletter_with_unsubscribe_footer_is_bulk():
    msgs = [_msg(text="Our spring appeal is live!\n\nUnsubscribe | View this email in your browser")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert is_bulk and "footer" in reason


def test_known_esp_domain_is_bulk():
    msgs = [_msg(email="campaign@mail.mailchimp.com", text="Plain promo, no footer word")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert is_bulk and "ESP" in reason


def test_mailer_daemon_bounce_is_bulk():
    msgs = [_msg(email="mailer-daemon@cfmins.org", text="Delivery failed")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert is_bulk and "system/bounce" in reason


def test_plain_human_email_is_not_bulk():
    msgs = [_msg(email="hannah@claggettcenter.org",
                 text="Hi Jay, here is the operations spreadsheet you asked for.")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert not is_bulk and reason is None


def test_engaged_thread_never_prefiltered():
    # Even with an unsubscribe footer, if Jay already replied it's a real thread.
    msgs = [
        _msg(inbound=True, text="...unsubscribe...", ago_hours=5),
        _msg(inbound=False, email="jay@cfmins.org", text="Thanks, removing us.", ago_hours=2),
    ]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert not is_bulk and reason is None


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("SPAM_PREFILTER", "false")
    msgs = [_msg(email="campaign@mailchimp.com", text="unsubscribe")]
    is_bulk, reason = looks_like_bulk(_conv(), msgs)
    assert not is_bulk and reason is None
