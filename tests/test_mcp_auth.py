"""Who is let into the Front / Chief of Staff server (26-147 rollout #5, 2026-10-10).

Real Microsoft-style tokens are signed with a throwaway RSA key and checked by the real
bearer_auth.validate_token (signature, expiry, issuer, audience); only the fetch of Microsoft's public
keys and Secret Manager are faked. The middleware under test is the real mcp_auth.AuthMiddleware.
"""
import importlib
import json
import os
import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import bearer_auth
import mcp_auth
import oauth_discovery

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ADMINS, OTHERS = "grp-qbo-admins", "grp-someone-else"
API_KEY = "test-api-key-value"
URL = "https://front.example.test"
ISS = f"https://login.microsoftonline.com/{TENANT}/v2.0"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(groups=(ADMINS,), key=_KEY, exp_in=3600, aud=f"api://{CLIENT}", iss=ISS, name="Test User"):
    claims = {"iss": iss, "aud": aud, "exp": int(time.time()) + exp_in, "name": name}
    if groups is not None:
        claims["groups"] = list(groups)
    return pyjwt.encode(claims, key, algorithm="RS256")


class Env:
    """Fake Secret Manager + a clock, so a permission edit can be made and time moved forward."""
    def __init__(self):
        self.secrets = {
            "front-mail-azure-tenant-id": TENANT,
            "front-mail-azure-client-id": CLIENT,
            "front-permissions": json.dumps({"groups": {ADMINS: {"name": "QBO-Admins"}}}),
        }
        self.cache = {}
        self.fail_reads = False
        self.now = 1_000_000.0

    def get_secret(self, name):
        if self.fail_reads:
            raise RuntimeError("Secret Manager unreachable")
        self.cache[name] = self.secrets[name]
        return self.secrets[name]


@pytest.fixture
def env(monkeypatch):
    e = Env()
    monkeypatch.setattr(oauth_discovery, "SECRET_PREFIX", "front-mail")
    monkeypatch.setattr(bearer_auth, "_perm_cache", None)
    monkeypatch.setattr(bearer_auth, "_perm_cache_time", 0.0)
    monkeypatch.setattr(bearer_auth, "time", SimpleNamespace(time=lambda: e.now))
    monkeypatch.setattr(bearer_auth, "_jwks", lambda tenant: SimpleNamespace(
        get_signing_key_from_jwt=lambda tok: SimpleNamespace(key=_KEY.public_key())))
    return e


def make_client(env, allow_key_on_mcp=True, api_key=API_KEY):
    async def ok(request):
        return JSONResponse({"ok": True})
    app = Starlette(routes=[
        Route("/mcp", ok, methods=["GET", "POST"]),
        Route("/mcp/sub", ok, methods=["GET", "POST"]),
        Route("/api/cos/loops", ok, methods=["GET"]),
        Route("/health", ok), Route("/", ok),
        Route("/.well-known/oauth-protected-resource", ok),
    ])
    app.add_middleware(
        mcp_auth.AuthMiddleware, get_secret=env.get_secret, secret_cache=env.cache, api_key=api_key,
        cloud_run_url=URL, allow_api_key_on_mcp=allow_key_on_mcp, oauth_on=True,
        open_paths={"/", "/health", "/.well-known/oauth-protected-resource"})
    return TestClient(app, raise_server_exceptions=False)


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── Claim 1: only a genuine, current Microsoft token for our app gets in ─────────────────────────

def test_no_credentials_on_mcp_is_refused_and_told_where_to_sign_in(env):
    r = make_client(env).post("/mcp")
    assert r.status_code == 401
    assert f"{URL}/.well-known/oauth-protected-resource" in r.headers["WWW-Authenticate"]


def test_token_signed_by_someone_else_is_refused(env):
    assert make_client(env).post("/mcp", headers=bearer(token(key=_OTHER_KEY))).status_code == 401


def test_expired_token_is_refused(env):
    assert make_client(env).post("/mcp", headers=bearer(token(exp_in=-60))).status_code == 401


def test_token_for_another_app_is_refused(env):
    assert make_client(env).post("/mcp", headers=bearer(token(aud="api://99999999-0000-0000-0000-000000000000"))).status_code == 401


def test_token_from_another_tenant_is_refused(env):
    assert make_client(env).post("/mcp", headers=bearer(token(iss="https://login.microsoftonline.com/other/v2.0"))).status_code == 401


def test_garbage_bearer_is_refused(env):
    assert make_client(env).post("/mcp", headers=bearer("not.a.token")).status_code == 401


# ── Claim 2: the group list decides who may use the tools ────────────────────────────────────────

def test_genuine_token_from_an_allowed_group_gets_in_on_both_mcp_paths(env):
    c = make_client(env)
    assert c.post("/mcp", headers=bearer(token())).status_code == 200
    assert c.post("/mcp/sub", headers=bearer(token())).status_code == 200


def test_genuine_token_from_a_group_not_on_the_list_is_forbidden(env):
    assert make_client(env).post("/mcp", headers=bearer(token(groups=(OTHERS,)))).status_code == 403


def test_genuine_token_with_no_groups_at_all_is_forbidden(env):
    assert make_client(env).post("/mcp", headers=bearer(token(groups=None))).status_code == 403


def test_removing_a_group_stops_working_within_five_minutes_without_a_restart(env):
    c = make_client(env)
    assert c.post("/mcp", headers=bearer(token())).status_code == 200
    env.secrets["front-permissions"] = json.dumps({"groups": {}})      # admin edits the secret
    env.now += 60
    assert c.post("/mcp", headers=bearer(token())).status_code == 200    # still inside the 5-minute window
    env.now += 301
    assert c.post("/mcp", headers=bearer(token())).status_code == 403    # re-read, person is out


def test_adding_a_group_starts_working_within_five_minutes_without_a_restart(env):
    c = make_client(env)
    assert c.post("/mcp", headers=bearer(token(groups=(OTHERS,)))).status_code == 403
    env.secrets["front-permissions"] = json.dumps({"groups": {ADMINS: {"name": "A"}, OTHERS: {"name": "B"}}})
    env.now += 301
    assert c.post("/mcp", headers=bearer(token(groups=(OTHERS,)))).status_code == 200


def test_if_secret_manager_is_down_the_last_good_list_is_kept(env):
    good = env.get_secret("front-permissions")
    env.fail_reads = True
    assert bearer_auth.read_secret_fresh("front-permissions", env.get_secret, env.cache) == good
    assert env.cache["front-permissions"] == good          # and stays cached for the next try
    with pytest.raises(RuntimeError):                      # nothing to fall back on: fail, do not guess
        bearer_auth.read_secret_fresh("never-read-before", env.get_secret, {})


# ── Claim 3: the API key works where it should and nowhere else once phase B is on ───────────────

def test_phase_a_key_still_works_on_mcp(env):
    assert make_client(env).post("/mcp", headers={"X-API-Key": API_KEY}).status_code == 200


def test_phase_a_wrong_key_is_forbidden_and_missing_key_is_unauthorized(env):
    c = make_client(env)
    assert c.post("/mcp", headers={"X-API-Key": "nope"}).status_code == 403
    assert c.post("/mcp").status_code == 401


def test_phase_b_the_right_key_is_no_longer_accepted_on_mcp(env):
    r = make_client(env, allow_key_on_mcp=False).post("/mcp", headers={"X-API-Key": API_KEY})
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    assert make_client(env, allow_key_on_mcp=False).post("/mcp?api_key=" + API_KEY).status_code == 401


def test_phase_b_sign_in_still_works(env):
    assert make_client(env, allow_key_on_mcp=False).post("/mcp", headers=bearer(token())).status_code == 200


@pytest.mark.parametrize("allow", [True, False])
def test_triage_workbook_path_keeps_working_with_the_key_in_both_phases(env, allow):
    c = make_client(env, allow_key_on_mcp=allow)
    assert c.get("/api/cos/loops", headers={"X-API-Key": API_KEY}).status_code == 200
    assert c.get("/api/cos/loops?api_key=" + API_KEY).status_code == 200
    assert c.get("/api/cos/loops").status_code == 401
    assert c.get("/api/cos/loops", headers={"X-API-Key": "nope"}).status_code == 403


def test_a_microsoft_token_does_not_open_the_workbook_path(env):
    assert make_client(env).get("/api/cos/loops", headers=bearer(token())).status_code == 401


def test_server_with_no_key_configured_fails_closed(env):
    assert make_client(env, api_key="").get("/api/cos/loops", headers={"X-API-Key": "anything"}).status_code == 500


# ── Claim 4: the front door pages stay open ──────────────────────────────────────────────────────

def test_health_and_sign_in_discovery_need_no_credentials(env):
    c = make_client(env, allow_key_on_mcp=False)
    for p in ("/", "/health", "/.well-known/oauth-protected-resource"):
        assert c.get(p).status_code == 200, p


# ── Claim 5: the real server is wired this way ───────────────────────────────────────────────────

def test_the_real_server_uses_the_new_door(monkeypatch):
    monkeypatch.setenv("OAUTH_MODE", "proxy")
    monkeypatch.setenv("OAUTH_SECRET_PREFIX", "front-mail")
    monkeypatch.setenv("MCP_ALLOW_API_KEY", "false")
    monkeypatch.setenv("OAUTH_RESOURCE", URL + "/mcp")
    importlib.reload(oauth_discovery)
    import mcp_server
    importlib.reload(mcp_server)
    try:
        assert any(m.cls is mcp_auth.AuthMiddleware for m in mcp_server.app.user_middleware)
        assert mcp_server._ALLOW_API_KEY_ON_MCP is False
        c = TestClient(mcp_server.app, raise_server_exceptions=False)
        assert c.get("/health").status_code == 200
        assert c.post("/mcp").status_code == 401
        assert c.get("/api/cos/loops").status_code == 401
        monkeypatch.setattr(mcp_server, "_get_secret", lambda n: {"front-mail-azure-tenant-id": TENANT,
                                                                  "front-mail-azure-client-id": CLIENT}[n])
        prm = c.get("/.well-known/oauth-protected-resource")
        assert prm.status_code == 200 and prm.json()["resource"].endswith("/mcp")
    finally:
        monkeypatch.undo()
        importlib.reload(oauth_discovery)
        importlib.reload(mcp_server)
