"""
Azure Entra confidential client (client-credentials) -> clickhouse-connect via
a refreshable token_provider (driver 1.2.0+, PR #775). No user, no browser.

The app authenticates with its own client_id + client_secret and receives an
*app* access token (no `upn`, no user identity). ClickHouse validates it with
the `entra` processor and, via the token user_directory bound to `azure_app`
(username_claim=oid), auto-provisions a user named after the app's
service-principal object id and grants it azure_jwt_role.

Client-credentials tokens have no refresh token, so renewal is just another
client-credentials request — cheap, so we simply re-mint when the cached token
nears expiry.

Env:
  AZURE_TENANT_ID       required
  AZURE_CLIENT_ID       required
  AZURE_CLIENT_SECRET   required
  AZURE_SCOPE           optional; default "<CLIENT_ID>/.default"
                        (must end in /.default; client-credentials cannot use
                        openid/email/profile or offline_access)
"""
import base64
import json
import os
import time

import clickhouse_connect
import requests

TENANT = os.environ["AZURE_TENANT_ID"]
CLIENT = os.environ["AZURE_CLIENT_ID"]
SECRET = os.environ["AZURE_CLIENT_SECRET"]
# Bare-GUID /.default: an app requesting a token for itself must use this form
# or Azure returns AADSTS90009. No offline_access — client-credentials never
# issues a refresh token.
SCOPE = os.environ.get("AZURE_SCOPE") or f"{CLIENT}/.default"
BASE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"

CH_HOST = os.environ.get("CLICKHOUSE_HOST") or "localhost"
CH_PORT = int(os.environ.get("CLICKHOUSE_PORT") or "8123")


def token_exp(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["exp"]
    except Exception:
        return 0  # undecodable -> treat as expired


def acquire():
    r = requests.post(f"{BASE}/token", data={
        "grant_type": "client_credentials", "client_id": CLIENT,
        "client_secret": SECRET, "scope": SCOPE}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"token acquisition failed: {r.text}")
    return r.json()


def make_token_provider():
    tokens = {}
    last_issued = None

    def token_provider():
        nonlocal last_issued
        access_token = tokens.get("access_token")
        # reuse the cached token unless expired or just rejected (== last_issued)
        if access_token and time.time() < token_exp(access_token) - 60 \
                and access_token != last_issued:
            last_issued = access_token
            return access_token

        tokens.clear()
        tokens.update(acquire())
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
