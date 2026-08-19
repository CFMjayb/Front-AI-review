"""Tests for pipeline._mailboxes_for — the To:-field attribution rule that now
drives every NEW loop, not just the one-off backfill.

Jay, 2026-08-18: "These spreadsheets should be according to the To: field...
if an email comes in to jay@cfmins.org it goes on CFM. if an email comes in to
jboggs@episcopalmaryland.org it is on EDOM — this has nothing to do with who
the email is from." Both: goes on both sheets. Cc-only: does not count.
"""
import pipeline


def _msg(to=(), cc=(), sender="someone@example.com"):
    recips = [{"role": "from", "handle": sender}]
    recips += [{"role": "to", "handle": h} for h in to]
    recips += [{"role": "cc", "handle": h} for h in cc]
    return {"is_inbound": True, "created_at": 1, "recipients": recips}


def test_to_cfm_only():
    keys = pipeline._mailboxes_for({}, [_msg(to=["jay@cfmins.org"])], front=None)
    assert keys == ["cfm"]


def test_to_edom_only():
    keys = pipeline._mailboxes_for({}, [_msg(to=["jboggs@episcopalmaryland.org"])], front=None)
    assert keys == ["edom"]


def test_to_both_addresses_gives_both_mailboxes():
    keys = pipeline._mailboxes_for(
        {}, [_msg(to=["jay@cfmins.org", "jboggs@episcopalmaryland.org"])], front=None)
    assert set(keys) == {"cfm", "edom"}


def test_cc_alone_does_not_count():
    """Jay's explicit rule: Cc is not To:. A conversation the address was only
    cc'd on must fall through to the inbox-hint / Front-lookup path, not be
    attributed as if it had been addressed directly."""
    conv = {"_cos_mailbox": "edom"}
    keys = pipeline._mailboxes_for(
        conv, [_msg(to=["someone-else@example.com"], cc=["jay@cfmins.org"])], front=None)
    assert keys == ["edom"], "falls back to the inbox hint, cfm is NOT inferred from cc"


def test_no_messages_falls_back_to_hint():
    conv = {"_cos_mailbox": "dme"}
    assert pipeline._mailboxes_for(conv, [], front=None) == ["dme"]


def test_alias_domain_matches():
    """DME's inbox receives as .net but sends as .org — both must resolve."""
    keys = pipeline._mailboxes_for({}, [_msg(to=["finance@episcopalmaine.net"])], front=None)
    assert keys == ["dme"]
