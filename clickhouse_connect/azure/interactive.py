"""
Azure Entra interactive browser sign-in (authorization-code + PKCE) ->
clickhouse-connect via a refreshable token_provider (driver 1.2.0+, PR #775).

The explicit user flow: opens a browser, the user authenticates on Microsoft's
page, the redirect lands back on a one-shot localhost server that captures the
code, and the code is exchanged for tokens. Delegated (user) tokens carry
`upn`, so ClickHouse maps them to the pre-defined user via the same `azure`
processor as app.py — no extra server config. Renews silently via the refresh
token (offline_access), which is cached on disk so later runs skip the browser.

Public vs confidential client: PKCE alone is enough for a public client (same
registration app.py uses), and a public client must NOT send a secret —
Azure rejects that with AADSTS700025. Only if this script's redirect URI is
registered under "Web" does Azure demand client auth on the code exchange
(AADSTS7000218, the error MSAL's public-client interactive_sample.py hits
against a confidential app); that is what the secret below is for.

Env:
  AZURE_TENANT_ID       required
  AZURE_CLIENT_ID       required
  AZURE_SCOPE           optional; default "<CLIENT_ID>/.default offline_access"
  AZURE_REDIRECT_URI    optional; default http://localhost:8400/
                        must be registered on the app (Authentication ->
                        "Mobile and desktop applications" for a public client,
                        or "Web" for a confidential client)
  AZURE_INTERACTIVE_CLIENT_SECRET
                        optional; set only for a "Web" redirect URI above.
                        Separate from AZURE_CLIENT_SECRET so that setting the
                        latter for confidential_client.py / web_app.py cannot
                        break this script's public-client flow.
  AZURE_TOKEN_CACHE     optional; default
                        ~/.cache/clickhouse-connect-azure/interactive.json
                        holds the refresh token, mode 0600. Set to "none" to
                        keep it in memory only, like the other scripts here.
"""
import base64
import contextlib
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import clickhouse_connect
import requests

TENANT = os.environ["AZURE_TENANT_ID"]
CLIENT = os.environ["AZURE_CLIENT_ID"]
# deliberately not AZURE_CLIENT_SECRET: that one is set for the confidential
# scripts, and sending it on a desktop redirect URI fails with AADSTS700025
SECRET = os.environ.get("AZURE_INTERACTIVE_CLIENT_SECRET")
# Bare-GUID scope (not api://<client_id>): an app requesting a token for itself
# must use the GUID form or Azure returns AADSTS90009.
SCOPE = os.environ.get("AZURE_SCOPE") or f"{CLIENT}/.default offline_access"
REDIRECT_URI = os.environ.get("AZURE_REDIRECT_URI") or "http://localhost:8400/"
BASE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"

CH_HOST = os.environ.get("CLICKHOUSE_HOST") or "localhost"
CH_PORT = int(os.environ.get("CLICKHOUSE_PORT") or "8123")

CACHE_PATH = os.environ.get("AZURE_TOKEN_CACHE") or os.path.join(
    os.environ.get("XDG_CACHE_HOME") or "~/.cache",
    "clickhouse-connect-azure", "interactive.json")
# "none" opts out: the refresh token then lives only as long as this process
CACHE_PATH = None if CACHE_PATH.lower() == "none" else os.path.expanduser(CACHE_PATH)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def with_secret(data):
    # confidential apps must send the secret on the token endpoint; public
    # clients (PKCE) must omit it
    if SECRET:
        data = {**data, "client_secret": SECRET}
    return data


def load_refresh_token():
    """The refresh token left by an earlier run, if it fits this registration."""
    if CACHE_PATH is None:
        return None
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        return None
    if cached.get("tenant") != TENANT or cached.get("client") != CLIENT:
        return None  # cache belongs to a different app registration
    return cached.get("refresh_token")


def save_refresh_token(refresh_token):
    """Mirror the in-memory refresh token to disk; drop the file when there is none."""
    if CACHE_PATH is None:
        return
    if not refresh_token:
        with contextlib.suppress(OSError):
            os.remove(CACHE_PATH)
        return
    try:
        directory = os.path.dirname(CACHE_PATH)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        # O_CREAT with 0600 so the token is never briefly world-readable
        fd = os.open(CACHE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"tenant": TENANT, "client": CLIENT,
                       "refresh_token": refresh_token}, fh)
    except OSError as exc:
        # the token still works for this process; only the next run pays for it
        print(f"warning: could not cache refresh token: {exc}", file=sys.stderr)


def oauth_error(resp):
    try:
        return resp.json().get("error")
    except (ValueError, AttributeError):
        return None


def wait_for_auth_code(expected_state):
    """Localhost server that captures ?code= from the browser redirect.

    Keeps serving until a request actually carries `code` or `error`. Anything
    else on this port -- IDE/devcontainer port-forwarding probes, favicon and
    prefetch requests, a stale tab from an earlier run carrying a different
    `state` -- is rejected and ignored rather than ending the wait.
    """
    parsed = urllib.parse.urlparse(REDIRECT_URI)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    result = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("error"):
                result["error"] = qs["error"][0]
                result["error_description"] = qs.get("error_description", [""])[0]
            elif not qs.get("code"):
                self.send_error(404)  # not the redirect
                return
            elif qs.get("state", [None])[0] != expected_state:
                self.send_error(400, "state mismatch")  # redirect from another run
                return
            else:
                result["code"] = qs["code"][0]
            body = (b"<html><body><h1>Sign-in complete</h1>"
                    b"<p>You can close this window and return to the terminal.</p>"
                    b"</body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *_):  # silence default access log
            return

    server = http.server.HTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout=300):
            raise RuntimeError("timed out waiting for browser sign-in")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    if "code" not in result:
        raise RuntimeError(
            f"sign-in failed: {result.get('error')}: {result.get('error_description')}")
    return result["code"]


def interactive_flow():
    state = b64url(secrets.token_bytes(16))
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {
        "client_id": CLIENT,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    url = f"{BASE}/authorize?{urllib.parse.urlencode(params)}"
    print(f"Opening browser for sign-in:\n  {url}", file=sys.stderr)
    if not webbrowser.open(url):
        print("Could not open a browser; paste the URL above manually.", file=sys.stderr)
    code = wait_for_auth_code(state)
    r = requests.post(f"{BASE}/token", data=with_secret({
        "grant_type": "authorization_code", "client_id": CLIENT,
        "code": code, "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier, "scope": SCOPE}), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"code exchange failed: {r.text}")
    return r.json()


def refresh(refresh_token):
    r = requests.post(f"{BASE}/token", data=with_secret({
        "grant_type": "refresh_token", "client_id": CLIENT,
        "refresh_token": refresh_token, "scope": SCOPE}), timeout=30)
    r.raise_for_status()
    tokens = r.json()
    # RFC 6749 §6 lets the IdP omit refresh_token on a refresh
    tokens.setdefault("refresh_token", refresh_token)
    return tokens


def renew(refresh_token):
    """Silent refresh while the refresh token lives, browser sign-in once it doesn't."""
    if refresh_token:
        try:
            return refresh(refresh_token)
        except requests.HTTPError as exc:
            if oauth_error(exc.response) not in ("invalid_grant", "interaction_required"):
                raise RuntimeError(f"token refresh failed, retry later: {exc}") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"token refresh failed (network), retry later: {exc}") from exc
    return interactive_flow()


def make_token_provider():
    """The token_provider: called at client init and on each ClickHouse rejection.

    Both cases need a token the driver does not already hold, so every call
    renews rather than serving a cache.
    """
    refresh_token = load_refresh_token()

    def token_provider():
        nonlocal refresh_token
        tokens = renew(refresh_token)
        refresh_token = tokens.get("refresh_token")
        save_refresh_token(refresh_token)
        return tokens["access_token"]

    return token_provider


def main():
    client = clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, token_provider=make_token_provider())
    user = client.query("SELECT currentUser()").result_rows[0][0]
    print(f"currentUser(): {user}")


if __name__ == "__main__":
    main()
