"""Tests for the subject line of a delegation e-mail (cos_triage_import._delegate).

Square brackets in an e-mail subject are the trigger for Bracket, the secure-e-mail service CFM uses; it does not work
on shared mailboxes, so mail with "[...]" in the subject can be intercepted, bounced ("Unknown user") and silently lost
(the 26-155 payroll review e-mail was lost this way on 2026-10-16). The delegation e-mail used to start with
"[Delegated]". Standing rule: no [ ] in any subject we send, including brackets that come from the AI's summary or the
sender's name.

Mutation check: set DELEGATE_MODULE=cos_triage_import_pre_no_brackets to run these against the pre-fix file; they must fail.
"""
import importlib
import os

from cos import sender

imp = importlib.import_module(os.environ.get("DELEGATE_MODULE", "cos_triage_import"))


def _delegate_and_capture(monkeypatch, loop, note=""):
    sent = []
    monkeypatch.setattr(sender, "send", lambda **kw: sent.append(kw) or {"transport": "test"})
    ok = imp._delegate(loop, 12, note, "admin@cfmins.org", "Admin")
    return ok, sent


def test_delegation_subject_has_no_square_brackets(monkeypatch):
    loop = {"counterparty": "Acme Supply", "counterparty_email": "ap@acme.example",
            "summary": "Approve the October invoice", "category": "payment"}
    ok, sent = _delegate_and_capture(monkeypatch, loop)
    assert ok is True and len(sent) == 1
    subject = sent[0]["subject"]
    assert "[" not in subject and "]" not in subject, subject
    assert subject.startswith("Delegated #12 - Acme Supply: Approve the October invoice")


def test_brackets_from_the_counterparty_or_summary_are_swapped_for_parentheses(monkeypatch):
    loop = {"counterparty": "Acme [Corp]", "counterparty_email": "ap@acme.example",
            "summary": "Pay the [urgent] invoice", "category": "payment"}
    ok, sent = _delegate_and_capture(monkeypatch, loop)
    assert ok is True
    subject = sent[0]["subject"]
    assert "[" not in subject and "]" not in subject, subject
    assert "Acme (Corp)" in subject and "(urgent)" in subject


def test_the_body_keeps_the_original_text(monkeypatch):
    loop = {"counterparty": "Acme [Corp]", "counterparty_email": "ap@acme.example",
            "summary": "Pay the [urgent] invoice", "category": "payment"}
    ok, sent = _delegate_and_capture(monkeypatch, loop, note="please [handle]")
    assert ok is True
    body = sent[0]["body_md"]
    assert "Pay the [urgent] invoice" in body and "please [handle]" in body


def test_a_failed_send_still_returns_false(monkeypatch):
    def boom(**kw):
        raise RuntimeError("send failed")
    monkeypatch.setattr(sender, "send", boom)
    loop = {"counterparty": "Acme", "summary": "x"}
    assert imp._delegate(loop, 1, "", "admin@cfmins.org", "Admin") is False
