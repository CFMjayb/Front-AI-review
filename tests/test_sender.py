"""Tests for the reusable sender layer (markdown→HTML + transport routing)."""
import importlib

import pytest


@pytest.fixture()
def sender(monkeypatch):
    monkeypatch.delenv("SENDER_TRANSPORT", raising=False)
    monkeypatch.delenv("SENDER_TO", raising=False)
    from cos import sender as sender_mod
    importlib.reload(sender_mod)
    return sender_mod


# ── Markdown → HTML ──────────────────────────────────────────────────────────

def test_headings(sender):
    assert "<h2>Your day</h2>" in sender.to_html("# Your day")
    assert "<h3>On you</h3>" in sender.to_html("## On you")


def test_bold_and_links(sender):
    html = sender.to_html("**Canon Sulerud** — reply → [open](https://x.test/c)")
    assert "<b>Canon Sulerud</b>" in html
    assert '<a href="https://x.test/c">open</a>' in html


def test_list_grouping(sender):
    html = sender.to_html("- one\n- two")
    assert html.count("<ul>") == 1
    assert html.count("</ul>") == 1
    assert html.count("<li>") == 2


def test_hr_and_paragraph(sender):
    html = sender.to_html("intro\n\n---\n\noutro")
    assert "<hr>" in html
    assert "<p>intro</p>" in html and "<p>outro</p>" in html


def test_html_is_escaped(sender):
    assert "&lt;script&gt;" in sender.to_html("a <script> tag")


# ── Transport routing ────────────────────────────────────────────────────────

def test_send_routes_to_registered_transport(sender):
    captured = {}

    def fake(*, subject, body_md, to):
        captured.update(subject=subject, body_md=body_md, to=to)
        return {"transport": "fake", "to": to}

    sender.register_transport("fake", fake)
    res = sender.send(subject="Hi", body_md="**hello**", to=["jay@cfmins.org"],
                      transport="fake")
    assert res["transport"] == "fake"
    assert captured["to"] == ["jay@cfmins.org"]
    assert captured["body_md"] == "**hello**"


def test_unknown_transport_raises(sender):
    with pytest.raises(sender.SendError):
        sender.send(subject="x", body_md="y", to=["a@b.c"], transport="carrier-pigeon")


def test_missing_recipient_raises(sender):
    with pytest.raises(sender.SendError):
        sender.send(subject="x", body_md="y", transport="front")


def test_defaults_from_env(sender, monkeypatch):
    monkeypatch.setenv("SENDER_TO", "a@b.c, d@e.f")
    monkeypatch.setenv("SENDER_TRANSPORT", "fake2")
    seen = {}
    sender.register_transport("fake2", lambda *, subject, body_md, to: seen.update(to=to) or {})
    sender.send(subject="x", body_md="y")
    assert seen["to"] == ["a@b.c", "d@e.f"]


def test_front_is_available(sender):
    assert "front" in sender.available_transports()
