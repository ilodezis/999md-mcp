"""Offline tests of the OAuth gate in front of the remote MCP endpoint."""

import base64
import hashlib
import os
from urllib.parse import parse_qs, urlsplit

os.environ.update(PASSWORD="pw", TOKEN_SECRET="s" * 32, CLIENT_SECRET="cs")

from starlette.testclient import TestClient  # noqa: E402

import remote as r  # noqa: E402

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 50
AUTH = {"response_type": "code", "client_id": r.CLIENT_ID, "redirect_uri": CALLBACK, "state": "st",
        "code_challenge": r.b64(hashlib.sha256(VERIFIER.encode()).digest()), "code_challenge_method": "S256"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def get_code(c: TestClient) -> str:
    resp = c.post("/oauth/authorize", data={**AUTH, "password": "pw"}, follow_redirects=False)
    assert resp.status_code == 303
    q = parse_qs(urlsplit(resp.headers["location"]).query)
    assert q["state"] == ["st"]
    return q["code"][0]


def mcp_status(c: TestClient, token: str | None) -> int:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return c.post("/mcp", json=INIT, headers=headers).status_code


def test_oauth_flow_and_bearer_gate():
    with TestClient(r.app, base_url="https://999md.example.com") as c:
        assert c.get("/.well-known/oauth-authorization-server").json()["token_endpoint"] == \
            "https://999md.example.com/oauth/token"

        assert c.get("/oauth/authorize", params={**AUTH, "redirect_uri": "https://evil.example/cb"}).status_code == 400
        assert c.get("/oauth/authorize", params={**AUTH, "code_challenge": ""}).status_code == 400
        page = c.get("/oauth/authorize", params=AUTH)
        assert page.status_code == 200 and "claude.ai" in page.text
        assert c.post("/oauth/authorize", data={**AUTH, "password": "nope"}).status_code == 401

        code = get_code(c)
        grant = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                 "client_id": r.CLIENT_ID, "client_secret": "cs", "code_verifier": VERIFIER}
        assert c.post("/oauth/token", data={**grant, "client_secret": "bad"}).status_code == 401
        basic = base64.b64encode(f"{r.CLIENT_ID}:cs".encode()).decode()
        no_secret = {k: v for k, v in grant.items() if k not in ("client_id", "client_secret")}
        token = c.post("/oauth/token", data=no_secret, headers={"Authorization": f"Basic {basic}"}).json()["access_token"]
        assert c.post("/oauth/token", data=grant).status_code == 400  # code is single-use

        wrong_verifier = {**grant, "code": get_code(c), "code_verifier": "x" * 50}
        assert c.post("/oauth/token", data=wrong_verifier).status_code == 400

        assert mcp_status(c, None) == 401
        assert mcp_status(c, token[:-2] + "xx") == 401
        assert mcp_status(c, r.sign_token(ttl=-1)) == 401
        assert mcp_status(c, token) == 200
