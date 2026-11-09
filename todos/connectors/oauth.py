"""Shared OAuth 2.0 helpers for connectors that need a user grant (Google Tasks today).

The connector's secret is one JSON blob {"client_id", "client_secret", "refresh_token", "token_uri"}
kept in the Keychain (or 1Password via secret_ref). `todo connector auth <name> --client <json>`
runs the one-time browser consent with a loopback redirect and PKCE, then stores the blob.
"""
import base64
import hashlib
import http.server
import json
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


class OAuthError(RuntimeError):
    pass


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise OAuthError(f"token endpoint: HTTP {e.code} {e.read()[:200]!r}") from e


def load_client(path: str) -> dict:
    """Read a Google OAuth client JSON (as downloaded from Cloud Console, 'Desktop app' type)."""
    with open(path) as f:
        raw = json.load(f)
    c = raw.get("installed") or raw.get("web") or raw
    if not c.get("client_id") or not c.get("client_secret"):
        raise OAuthError("client file has no client_id/client_secret; download the 'Desktop app' OAuth client JSON")
    return {"client_id": c["client_id"], "client_secret": c["client_secret"],
            "token_uri": c.get("token_uri", GOOGLE_TOKEN)}


def access_token(blob: dict) -> str:
    """Exchange the stored refresh token for a short-lived access token."""
    out = _post_form(blob.get("token_uri", GOOGLE_TOKEN), {
        "client_id": blob["client_id"], "client_secret": blob["client_secret"],
        "refresh_token": blob["refresh_token"], "grant_type": "refresh_token",
    })
    if "access_token" not in out:
        raise OAuthError(f"no access token returned: {str(out)[:200]}")
    return out["access_token"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def consent(client: dict, scopes: list[str], login_hint: str | None = None, timeout_s: int = 300) -> dict:
    """Open the browser for consent, catch the redirect on 127.0.0.1, return the stored-secret blob."""
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got and got.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>todos: Google Tasks connected. You can close this tab.</h3>" if ok
                             else b"<h3>todos: sign-in failed or was cancelled. Check the terminal.</h3>")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = timeout_s
    url = GOOGLE_AUTH + "?" + urllib.parse.urlencode(params)
    print(f"Opening your browser for Google sign-in. If it doesn't open, visit:\n{url}\n", flush=True)
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    server.handle_request()
    server.server_close()
    if got.get("state") != state or "code" not in got:
        raise OAuthError(f"consent not completed: {got.get('error', 'no code received')}")
    tokens = _post_form(client["token_uri"], {
        "client_id": client["client_id"], "client_secret": client["client_secret"], "code": got["code"],
        "code_verifier": verifier, "redirect_uri": redirect, "grant_type": "authorization_code",
    })
    if "refresh_token" not in tokens:
        raise OAuthError("Google returned no refresh token; remove the app's access at "
                         "myaccount.google.com/permissions and run auth again")
    return {**client, "refresh_token": tokens["refresh_token"]}
