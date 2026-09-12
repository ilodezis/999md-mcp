"""Remote entry point: the same MCP over Streamable HTTP at /mcp, behind a single-user OAuth gate
for Claude.ai custom connectors.

Flow: Claude.ai opens /oauth/authorize -> the owner types the password once -> Claude trades the
code (PKCE S256 + client secret) at /oauth/token for a signed Bearer token -> calls /mcp with it.
No storage: codes live in memory for 5 minutes, tokens are HMAC-signed and valid for a year.
Rotating TOKEN_SECRET revokes every issued token.

Run: uvicorn remote:app --port 8013 --proxy-headers
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from string import Template
from urllib.parse import urlencode, urlsplit

from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from server import mcp


def required(name: str) -> str:
    if not os.environ.get(name):  # an empty password or key would open the gate
        raise RuntimeError(f"{name} is not set; see .env.example")
    return os.environ[name]


PASSWORD = required("PASSWORD")
SECRET = required("TOKEN_SECRET").encode()
CLIENT_SECRET = required("CLIENT_SECRET")
CLIENT_ID = os.environ.get("CLIENT_ID", "999md-claude-connector")
REDIRECT_URIS = {u.strip() for u in os.environ.get(
    "REDIRECT_URIS", "https://claude.ai/api/mcp/auth_callback").split(",") if u.strip()}
TOKEN_TTL = 365 * 24 * 3600
CODE_TTL = 300
AUTH_FIELDS = ("response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method")

_codes: dict[str, tuple[float, dict]] = {}  # single process, single user: no shared store needed


# ---------- tokens ----------

def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def mac(body: str) -> str:
    return b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())


def sign_token(ttl: int = TOKEN_TTL) -> str:
    body = b64(json.dumps({"client_id": CLIENT_ID, "exp": int(time.time() + ttl)}).encode())
    return f"{body}.{mac(body)}"


def token_valid(token: str) -> bool:
    body, _, sig = token.partition(".")
    if not hmac.compare_digest(sig.encode(), mac(body).encode()):
        return False
    p = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))  # signed by us, so well-formed
    return p["client_id"] == CLIENT_ID and p["exp"] > time.time()


class BearerGate:
    """Lets /mcp through only with a valid token; OAuth routes stay public."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            if not (auth[:7].lower() == "bearer " and token_valid(auth[7:].strip())):
                deny = JSONResponse({"error": "invalid_token"}, 401, headers={"WWW-Authenticate": "Bearer"})
                return await deny(scope, receive, send)
        await self.app(scope, receive, send)


# ---------- OAuth routes ----------

@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_metadata(request: Request) -> Response:
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
    })


def request_error(p: dict) -> str | None:
    if p["client_id"] != CLIENT_ID:
        return "Неизвестный client_id."
    if p["response_type"] != "code":
        return "Поддерживается только response_type=code."
    if p["redirect_uri"] not in REDIRECT_URIS:
        return "Этот redirect_uri не в списке разрешённых."
    if not p["code_challenge"] or p["code_challenge_method"] != "S256":
        return "Нужен PKCE с методом S256."
    return None


@mcp.custom_route("/oauth/authorize", methods=["GET", "POST"])
async def oauth_authorize(request: Request) -> Response:
    src = request.query_params if request.method == "GET" else await request.form()
    p = {k: str(src.get(k) or "") for k in AUTH_FIELDS}
    if err := request_error(p):
        return consent_page(p, error=err, fatal=True, status=400)
    if request.method == "GET":
        return consent_page(p)
    if not hmac.compare_digest(str(src.get("password") or "").encode(), PASSWORD.encode()):
        await asyncio.sleep(1)  # slows guessing per request only; a strong password is the real guard
        return consent_page(p, error="Неверный пароль", status=401)
    now = time.time()
    for k in [k for k, (exp, _) in _codes.items() if exp <= now]:
        del _codes[k]
    code = secrets.token_urlsafe(32)
    _codes[code] = (now + CODE_TTL, {"redirect_uri": p["redirect_uri"], "code_challenge": p["code_challenge"]})
    query = urlencode({"code": code, **({"state": p["state"]} if p["state"] else {})})
    sep = "&" if "?" in p["redirect_uri"] else "?"
    return RedirectResponse(f"{p['redirect_uri']}{sep}{query}", 303)


def oauth_error(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error}, status)


@mcp.custom_route("/oauth/token", methods=["POST"])
async def oauth_token(request: Request) -> Response:
    try:
        body = await request.json() if "json" in request.headers.get("content-type", "") else dict(await request.form())
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return oauth_error("invalid_request")
    client_id, client_secret = body.get("client_id"), body.get("client_secret")
    auth = request.headers.get("authorization", "")
    if not client_secret and auth[:6].lower() == "basic ":
        try:
            cid, _, client_secret = base64.b64decode(auth[6:]).decode().partition(":")
            client_id = client_id or cid
        except ValueError:
            pass
    if client_id != CLIENT_ID or not hmac.compare_digest(str(client_secret or "").encode(), CLIENT_SECRET.encode()):
        return oauth_error("invalid_client", 401)
    if body.get("grant_type") != "authorization_code":
        return oauth_error("unsupported_grant_type")
    entry = _codes.pop(str(body.get("code")), None)  # single use, even when the checks below fail
    if not entry or entry[0] <= time.time() or body.get("redirect_uri") != entry[1]["redirect_uri"]:
        return oauth_error("invalid_grant")
    challenge = b64(hashlib.sha256(str(body.get("code_verifier") or "").encode()).digest())
    if not hmac.compare_digest(challenge.encode(), entry[1]["code_challenge"].encode()):
        return oauth_error("invalid_grant")
    return JSONResponse({"access_token": sign_token(), "token_type": "Bearer", "expires_in": TOKEN_TTL},
                        headers={"Cache-Control": "no-store"})


app = mcp.http_app(middleware=[Middleware(BearerGate)])


# ---------- consent page ----------

def consent_page(p: dict, error: str | None = None, fatal: bool = False, status: int = 200) -> HTMLResponse:
    e = html.escape
    if fatal:
        lede = f"Запрос на доступ не прошёл проверку. {e(error or '')}"
        body, stamp = "", "Отказ"
    else:
        hidden = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k, v in p.items())
        lede = (f"<b>{e(urlsplit(p['redirect_uri']).netloc)}</b> просит доступ к поиску объявлений "
                "999.md. Только чтение: искать, смотреть карточки и цены.")
        body = f"""<form method="post" action="/oauth/authorize" class="rise r4">{hidden}
      <input type="text" name="username" value="999md-mcp" autocomplete="username" hidden>
      <label for="pw">Пароль владельца</label>
      <input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
      <button type="submit">Разрешить доступ</button>
    </form>"""
        stamp = error
    stamp_html = f'<p class="stamp" role="alert">{e(stamp)}</p>' if stamp else ""
    return HTMLResponse(PAGE.substitute(lede=lede, body=body, stamp=stamp_html), status,
                        headers={"X-Frame-Options": "DENY", "Cache-Control": "no-store"})


PAGE = Template("""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="referrer" content="no-referrer">
<title>999.md → Claude</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Alumni+Sans:wght@700;800&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {
    --wall: #8e8a82; --paper: #f4eddc; --paper-edge: #e6dbc2; --ink: #1d1b18;
    --muted: #6e665b; --red: #d3321d; --tape: rgba(240, 222, 150, .74);
    --display: "Alumni Sans", Impact, "Arial Narrow", sans-serif;
    --mono: "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace;
    color-scheme: light;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 48px 16px;
    color: var(--ink); font: 14px/1.55 var(--mono);
    background:
      radial-gradient(120% 80% at 50% 30%, transparent 40%, rgba(0,0,0,.28)),
      url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 .22 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>"),
      var(--wall);
  }
  .stack { display: grid; width: min(100%, 372px); }
  .stack > * { grid-area: 1 / 1; }
  .scrap {
    background: var(--paper-edge); transform: translate(30px, -64px) rotate(6.5deg);
    padding: 18px 24px; font: 800 68px/.82 var(--display); text-transform: uppercase;
    color: rgba(29, 27, 24, .2); overflow: hidden; box-shadow: 0 10px 24px -14px rgba(0,0,0,.5);
    clip-path: polygon(0 0, 100% 0, 100% 71%, 93% 74%, 97% 80%, 88% 84%, 91% 100%, 0 100%);
  }
  .flyer {
    position: relative; padding: 30px 26px 0; transform: rotate(-1.4deg);
    background: radial-gradient(130% 90% at 50% 35%, var(--paper) 58%, var(--paper-edge));
    box-shadow: 0 1px 0 rgba(0,0,0,.06), 0 22px 34px -16px rgba(0,0,0,.55);
    animation: pin .7s cubic-bezier(.2, .9, .3, 1.15) both;
  }
  .tape {
    position: absolute; top: -15px; left: 50%; width: 118px; height: 32px; background: var(--tape);
    transform: translateX(-50%) rotate(2.5deg); box-shadow: 0 1px 2px rgba(0,0,0,.12);
    clip-path: polygon(3% 0, 97% 4%, 100% 30%, 96% 55%, 100% 100%, 2% 96%, 0 70%, 4% 40%);
  }
  .kicker {
    display: flex; justify-content: space-between; margin: 0 0 12px; padding: 6px 0;
    border-top: 3px solid var(--ink); border-bottom: 1px solid var(--ink);
    font-size: 11px; font-weight: 600; letter-spacing: .28em; text-transform: uppercase;
  }
  h1 { margin: 0; font: 800 clamp(96px, 30vw, 124px)/.78 var(--display); letter-spacing: -.01em; }
  h1 .num { color: var(--red); }
  h1 .dom { font-size: .42em; letter-spacing: .02em; }
  .lede { margin: 16px 0 22px; }
  .lede b { background: linear-gradient(transparent 58%, rgba(211, 50, 29, .22) 58%); }
  label {
    display: block; margin-bottom: 2px; color: var(--muted);
    font-size: 11px; font-weight: 600; letter-spacing: .2em; text-transform: uppercase;
  }
  input[type=password] {
    width: 100%; padding: 6px 2px; border: 0; border-bottom: 2px dashed var(--ink);
    background: transparent; color: var(--ink); font: 18px var(--mono); letter-spacing: .12em;
  }
  input[type=password]:focus { outline: none; border-bottom: 2px solid var(--red); }
  button {
    width: 100%; margin-top: 22px; padding: 10px 14px 8px; border: 2px solid var(--ink);
    background: var(--ink); color: var(--paper); cursor: pointer;
    font: 800 30px/1 var(--display); letter-spacing: .05em; text-transform: uppercase;
    box-shadow: 5px 5px 0 var(--red); transition: transform .12s, box-shadow .12s;
  }
  button:hover { transform: translate(-1px, -1px); box-shadow: 7px 7px 0 var(--red); }
  button:active { transform: translate(4px, 4px); box-shadow: 1px 1px 0 var(--red); }
  button:focus-visible { outline: 3px solid var(--red); outline-offset: 4px; }
  .stamp {
    position: absolute; top: 300px; right: 16px; margin: 0; padding: 3px 10px 1px;  /* lands on the password line */
    border: 4px double var(--red); color: var(--red); mix-blend-mode: multiply;
    font: 800 26px/1 var(--display); text-transform: uppercase; letter-spacing: .04em;
    transform: rotate(-11deg); animation: stamp .32s .45s cubic-bezier(.3, 1.6, .5, 1) both;
  }
  .tabs {
    display: flex; margin: 28px -26px 0; padding: 0; list-style: none;
    border-top: 2px dashed rgba(29, 27, 24, .4);
  }
  .tabs li {
    flex: 1; height: 96px; display: grid; place-items: center; color: var(--muted);
    border-right: 1px dashed rgba(29, 27, 24, .28); font-size: 10px; letter-spacing: .14em;
    writing-mode: vertical-rl; transform: rotate(180deg); white-space: nowrap;  /* rotated: right border draws on the left */
  }
  .tabs li:first-child { border-right: 0; }
  .tabs .gone { visibility: hidden; }
  .rise { animation: rise .5s both; }
  .r1 { animation-delay: .18s; } .r2 { animation-delay: .26s; } .r3 { animation-delay: .34s; } .r4 { animation-delay: .42s; }
  @keyframes pin { from { opacity: 0; transform: translateY(-22px) rotate(-5deg); } }
  @keyframes rise { from { opacity: 0; transform: translateY(8px); } }
  @keyframes stamp { from { opacity: 0; transform: scale(1.9) rotate(-11deg); } }
  @media (prefers-reduced-motion: reduce) { *, *::before { animation: none !important; transition: none !important; } }
</style>
</head>
<body>
<main class="stack">
  <div class="scrap" aria-hidden="true">Сдам<br>2-комн.<br>Ботаника<br>срочно</div>
  <article class="flyer">
    <span class="tape" aria-hidden="true"></span>
    <p class="kicker rise r1"><span>Объявление</span><span>MCP</span></p>
    <h1 class="rise r2"><span class="num">999</span><span class="dom">.md</span></h1>
    <p class="lede rise r3">$lede</p>
    $body
    $stamp
    <ul class="tabs" aria-hidden="true">
      <li>999.md</li><li>999.md</li><li class="gone">999.md</li><li>999.md</li><li>999.md</li><li>999.md</li>
    </ul>
  </article>
</main>
</body>
</html>""")
