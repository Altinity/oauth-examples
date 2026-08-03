"""
Azure Entra confidential-client web app (authorization code + PKCE with a
client secret, delegated user access) -> clickhouse-connect via a per-user
refreshable token_provider (driver 1.2.0+, PR #775).

A web-app shape rather than a desktop one: the redirect URI is a real route,
the browser holds nothing but an opaque session cookie, and every signed-in
user gets their own tokens and their own ClickHouse client. Each user queries
as themselves — the `upn` in their delegated token is the ClickHouse username,
resolved by the same `azure` processor app.py uses, so ClickHouse grants apply
per user.

The token_provider cannot open a browser here: it runs inside a request, under
a lock, for a user who may have closed the tab. A dead refresh token therefore
raises ReauthRequired, and the view turns that into a redirect to /login — the
one place interaction can happen.

Env:
  AZURE_TENANT_ID        required
  AZURE_CLIENT_ID        required
  AZURE_CLIENT_SECRET    required; this is the confidential-client scenario
  AZURE_SCOPE            optional; default "<CLIENT_ID>/.default offline_access"
  AZURE_WEB_REDIRECT_URI optional; default http://localhost:8500/auth/callback
                         must be registered on the app under Authentication ->
                         "Web" (its own variable, so interactive.py's
                         AZURE_REDIRECT_URI can stay registered as a
                         "Mobile and desktop applications" URI)
  FLASK_SECRET_KEY       optional; random per process, so a restart signs everyone out
  WEB_APP_PORT           optional; default 8500
"""
import base64
import contextlib
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse

import clickhouse_connect
import requests
from flask import Flask, redirect, render_template_string, request, session, url_for

TENANT = os.environ["AZURE_TENANT_ID"]
CLIENT = os.environ["AZURE_CLIENT_ID"]
SECRET = os.environ["AZURE_CLIENT_SECRET"]
# Bare-GUID scope (not api://<client_id>): an app requesting a token for itself
# must use the GUID form or Azure returns AADSTS90009. offline_access asks for
# the refresh token the provider renews with.
SCOPE = os.environ.get("AZURE_SCOPE") or f"{CLIENT}/.default offline_access"
REDIRECT_URI = (os.environ.get("AZURE_WEB_REDIRECT_URI")
                or "http://localhost:8500/auth/callback")
BASE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"
PORT = int(os.environ.get("WEB_APP_PORT") or "8500")

CH_HOST = os.environ.get("CLICKHOUSE_HOST") or "localhost"
CH_PORT = int(os.environ.get("CLICKHOUSE_PORT") or "8123")

EXP_SKEW = 60        # treat tokens expiring within a minute as expired
BURST_WINDOW = 10.0  # concurrent rejections share one renewal

app = Flask(__name__)
# signs the session cookie, which carries only the opaque sid
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_bytes(32)


class ReauthRequired(RuntimeError):
    """The refresh token is dead; only a browser round-trip can fix it."""


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def token_exp(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["exp"]
    except Exception:
        return 0  # undecodable -> treat as expired


def oauth_error(resp):
    try:
        return resp.json().get("error")
    except (ValueError, AttributeError):
        return None


def exchange(fields):
    """Token-endpoint call. Confidential apps must send the secret here."""
    resp = requests.post(f"{BASE}/token",
                         data={"client_id": CLIENT, "client_secret": SECRET,
                               "scope": SCOPE, **fields},
                         timeout=30)
    if resp.status_code == 200:
        return resp.json()
    # invalid_grant / interaction_required mean sign in again; anything else is
    # transient and must not throw the user out of their session
    if oauth_error(resp) in ("invalid_grant", "interaction_required"):
        raise ReauthRequired(resp.text)
    raise RuntimeError(f"token endpoint failed: {resp.text}")


class UserSession:
    """One signed-in user: their tokens, their locks, their ClickHouse client."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.renewed_at = time.monotonic()
        self.token_lock = threading.Lock()
        self.client_lock = threading.Lock()
        self._client = None

    def token(self):
        """The token_provider: called at client init and on each CH rejection."""
        with self.token_lock:
            access_token = self.tokens.get("access_token")
            # a renewal within the burst window already answered this caller
            if access_token and time.time() < token_exp(access_token) - EXP_SKEW \
                    and time.monotonic() - self.renewed_at < BURST_WINDOW:
                return access_token
            refresh_token = self.tokens.get("refresh_token")
            if not refresh_token:
                raise ReauthRequired("no refresh token in this session")
            tokens = exchange({"grant_type": "refresh_token",
                               "refresh_token": refresh_token})
            # RFC 6749 §6 lets the IdP omit refresh_token on a refresh
            tokens.setdefault("refresh_token", refresh_token)
            self.tokens = tokens
            self.renewed_at = time.monotonic()
            return tokens["access_token"]

    def client(self):
        # separate lock: get_client() calls token(), which takes token_lock
        with self.client_lock:
            if self._client is None:
                # autogenerate_session_id=False: two requests from one browser
                # can reach this client at once, and a sync client rejects
                # concurrent queries sharing a session id
                self._client = clickhouse_connect.get_client(
                    host=CH_HOST, port=CH_PORT, token_provider=self.token,
                    autogenerate_session_id=False)
            return self._client

    def expires_in(self):
        with self.token_lock:
            return int(token_exp(self.tokens.get("access_token", "")) - time.time())

    def close(self):
        with self.client_lock:
            if self._client is not None:
                with contextlib.suppress(Exception):
                    self._client.close()
                self._client = None


SESSIONS = {}  # sid -> UserSession; tokens stay here, never in the cookie
SESSIONS_LOCK = threading.Lock()


def remember(tokens):
    sid = secrets.token_urlsafe(32)
    with SESSIONS_LOCK:
        SESSIONS[sid] = UserSession(tokens)
    return sid


def current_user():
    sid = session.get("sid")
    if not sid:
        return None
    with SESSIONS_LOCK:
        return SESSIONS.get(sid)  # missing after a restart -> signed out


def forget():
    sid = session.pop("sid", None)
    with SESSIONS_LOCK:
        user = SESSIONS.pop(sid, None) if sid else None
    if user:
        user.close()


PAGE = """<!doctype html>
<title>clickhouse-connect + Entra web app</title>
<style>
 body{font-family:system-ui,sans-serif;margin:3rem auto;max-width:44rem;line-height:1.6}
 code{background:#f4f4f5;padding:.1rem .3rem;border-radius:.2rem}
 th{text-align:left;padding-right:1.5rem;font-weight:600;white-space:nowrap}
 td{font-family:ui-monospace,monospace}
</style>
{% if ch_user %}
  <h1>Signed in</h1>
  <table>
    <tr><th>currentUser()</th><td>{{ ch_user }}</td></tr>
    <tr><th>currentRoles()</th><td>{{ roles }}</td></tr>
    <tr><th>count() from default.test_table_1</th><td>{{ rows }}</td></tr>
    <tr><th>access token expires in</th><td>{{ expires_in }}s</td></tr>
  </table>
  <p>Reload to query again. Once the token expires ClickHouse rejects it, the
  driver calls the <code>token_provider</code>, and the refresh token renews it
  without leaving the page.</p>
  <p><a href="{{ url_for('logout') }}">Sign out</a></p>
{% else %}
  <h1>Not signed in</h1>
  <p><a href="{{ url_for('login') }}">Sign in with Microsoft</a></p>
{% endif %}
"""


@app.get("/")
def index():
    user = current_user()
    if user is None:
        return render_template_string(PAGE, ch_user=None)
    try:
        row = user.client().query(
            "SELECT currentUser(), currentRoles(), "
            "(SELECT count() FROM default.test_table_1)").result_rows[0]
    except ReauthRequired:
        forget()
        return redirect(url_for("login"))
    return render_template_string(PAGE, ch_user=row[0], roles=list(row[1]),
                                  rows=row[2], expires_in=user.expires_in())


@app.get("/login")
def login():
    state = b64url(secrets.token_bytes(16))
    verifier = b64url(secrets.token_bytes(32))
    # cookie-side, so the callback can check them without server state
    session["state"] = state
    session["verifier"] = verifier
    params = {
        "client_id": CLIENT,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": SCOPE,
        "state": state,
        "code_challenge": b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
        "code_challenge_method": "S256",
        # forces an account picker: sign-out below is local, so Entra still
        # holds a session that would otherwise sign the same user straight back in
        "prompt": "select_account",
    }
    return redirect(f"{BASE}/authorize?{urllib.parse.urlencode(params)}")


@app.get("/auth/callback")
def auth_callback():
    if request.args.get("error"):
        return (f"sign-in failed: {request.args['error']}: "
                f"{request.args.get('error_description', '')}"), 400
    state = session.pop("state", None)
    verifier = session.pop("verifier", None)
    if not state or request.args.get("state") != state:
        return "state mismatch", 400  # CSRF guard on the redirect
    code = request.args.get("code")
    if not code or not verifier:
        return "callback missing code or verifier", 400
    try:
        tokens = exchange({"grant_type": "authorization_code", "code": code,
                           "redirect_uri": REDIRECT_URI,
                           "code_verifier": verifier})
    except (ReauthRequired, RuntimeError) as exc:
        return f"code exchange failed: {exc}", 400
    forget()  # drop any previous session before adopting the new identity
    session["sid"] = remember(tokens)
    return redirect(url_for("index"))


@app.get("/logout")
def logout():
    # local only: an Entra front-channel logout would need its own registered
    # post_logout_redirect_uri
    forget()
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    print(f"redirect URI: {REDIRECT_URI} (must be registered under 'Web')")
    app.run(host="localhost", port=PORT, threaded=True)
