"""Tests for front_client helpers."""
from front_client import _num_header


def test_fractional_epoch_header_does_not_raise():
    """Regression: Front sends x-ratelimit-reset as a fractional epoch
    ("1787064542.997"). A bare int() on that raised ValueError from inside the
    rate-limit handler, turning a routine throttle into a failed request.
    Hit live 2026-08-18 during the mailbox backfill."""
    assert _num_header("1787064542.997", 0) == 1787064542


def test_plain_integer_header():
    assert _num_header("5", 0) == 5


def test_missing_or_unparseable_header_falls_back():
    assert _num_header(None, 5) == 5
    assert _num_header("", 5) == 5
    assert _num_header("garbage", 7) == 7
