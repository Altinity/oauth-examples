"""
Azure Entra interactive browser sign-in (authorization-code + PKCE) ->
clickhouse-connect via a refreshable token_provider (driver 1.2.0+, PR #775).

The explicit user flow: opens a browser, the user authenticates on Microsoft's
page, the redirect lands back on a one-shot localhost server that captures the
code, and the code is exchanged for tokens. Delegated (user) tokens carry
`upn`, so ClickHouse maps them to the pre-defined user via the same `azure`
processor as app.py — no extra server config. Renews silently via the refresh
token (offline_access).

Public vs confidential client: PKCE alone is enough for a public client (same
registration app.py uses). If the app is confidential, set AZURE_CLIENT_SECRET
and it is sent on the code/refresh exchange too — this is what avoids
AADSTS7000218 ("must contain 'client_assertion' or 'client_secret'"), the error
MSAL's public-client interactive_sample.py hits against a confidential app.

Env:
  AZURE_TENANT_ID       required
  AZURE_CLIENT_ID       required
  AZURE_CLIENT_SECRET   optional; required only for a confidential app
  AZURE_SCOPE           optional; default "<CLIENT_ID>/.default offline_access"
  AZURE_REDIRECT_URI    optional; default http://localhost:8400/
                        must be registered on the app (Authentication ->
                        "Mobile and desktop applications" for a public client,
                        or "Web" for a confidential client)
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser

import clickhouse_connect
import requests

TENANT = os.environ["AZURE_TENANT_ID"]
CLIENT = os.environ["AZURE_CLIENT_ID"]
SECRET = os.environ.get("AZURE_CLIENT_SECRET")  # only needed for a confidential app
# Bare-GUID scope (not api://<client_id>): an app requesting a token for itself
# must use the GUID form or Azure returns AADSTS90009.
SCOPE = os.environ.get("AZURE_SCOPE") or f"{CLIENT}/.default offline_access"
REDIRECT_URI = os.environ.get("AZURE_REDIRECT_URI") or "http://localhost:8400/"
BASE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"

CH_HOST = os.environ.get("CLICKHOUSE_HOST") or "localhost"
CH_PORT = int(os.environ.get("CLICKHOUSE_PORT") or "8123")


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def with_secret(data):
    # confidential apps must send the secret on the token endpoint; public
    # clients (PKCE) must omit it
    if SECRET:
        data = {**data, "client_secret": SECRET}
    return data


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


def wait_for_auth_code(expected_state):
    """One-shot localhost server that captures ?code= from the browser redirect."""
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
            elif qs.get("code") and qs.get("state", [None])[0] == expected_state:
                result["code"] = qs["code"][0]
            else:
                result["error"] = "invalid_callback"
                result["error_description"] = "missing code or state mismatch"
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
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    if not done.wait(timeout=300):
        server.server_close()
        raise RuntimeError("timed out waiting for browser sign-in")
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
    return r.json()


def renew(tokens):
    refresh_token = tokens.get("refresh_token")
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
    tokens = {}
    last_issued = None

    def token_provider():
        nonlocal last_issued
        access_token = tokens.get("access_token")
        if access_token and time.time() < token_exp(access_token) - 60 \
                and access_token != last_issued:
            last_issued = access_token
            return access_token

        new = renew(tokens)
        new.setdefault("refresh_token", tokens.get("refresh_token"))
        tokens.clear()
        tokens.update(new)
        last_issued = tokens["access_token"]
        return tokens["access_token"]

    return token_provider


def main():
    client = clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, token_provider=make_token_provider())
    user = client.query("SELECT currentUser()").result_rows[0][0]
    print(f"currentUser(): {user}")


if __name__ == "__main__":
    main()
