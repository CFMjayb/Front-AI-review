"""Test configuration. Force the SQLite ledger backend so the suite is fully
local and never touches a real Firestore database, regardless of the ambient
LEDGER_BACKEND (e.g. if it's set to 'firestore' in .env for real use)."""
import os

import pytest

os.environ["LEDGER_BACKEND"] = "sqlite"


@pytest.fixture(autouse=True)
def _no_real_outbound(monkeypatch):
    """Guard against a real email send during the test suite.

    auth.py (and mcp_server.py, digest.py, scheduler.py) call
    load_dotenv(override=True) unconditionally at IMPORT time. If any test
    module imports something that pulls in auth.py — e.g. `import pipeline` —
    the real production .env (real API keys, BRIEFING_DELIVERY=email,
    SENDER_TRANSPORT=http, a real EMAIL_MCP_URL) overwrites os.environ for the
    rest of that pytest process, regardless of what this file or a test's own
    monkeypatch set earlier. override=True means even the LEDGER_BACKEND guard
    above isn't safe against it happening again from a different module.

    Hit for real on 2026-08-18: adding tests/test_pipeline_mailbox_attribution.py
    (which imports pipeline.py) caused test_run_briefing_writes_file to
    actually deliver via the live http transport instead of the "file" default
    it expects — meaning a real HTTP POST to the production email-mcp-server
    very likely fired during an ordinary test run. This fixture re-asserts
    test-safe values before every single test, so no import-order accident can
    make a test send anything real.
    """
    monkeypatch.setenv("LEDGER_BACKEND", "sqlite")   # same override=True risk
    monkeypatch.setenv("BRIEFING_DELIVERY", "file")
    monkeypatch.setenv("SENDER_TRANSPORT", "")
    monkeypatch.delenv("EMAIL_MCP_URL", raising=False)
    monkeypatch.delenv("EMAIL_MCP_API_KEY", raising=False)
    monkeypatch.delenv("FRONT_API_TOKEN", raising=False)
