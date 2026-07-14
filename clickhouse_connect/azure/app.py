"""
Azure Entra device-code -> clickhouse-connect via a refreshable token_provider
(driver 1.2.0+, PR #775). Signs in once, caches the tokens, renews silently via
the refresh token, and forwards the ACCESS token, which ClickHouse validates
with the `entra` processor.
"""
import base64
import json
import os
import sys
import time

import clickhouse_connect
import requests

TENANT = os.environ["AZURE_TENANT_ID"]
CLIENT = os.environ["AZURE_CLIENT_ID"]
# Bare-GUID scope, not api://<client_id>: an app requesting a token for itself
# must use the GUID form or Azure returns AADSTS90009.
SCOPE = f"{CLIENT}/.default offline_access"
BASE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"
CACHE = os.path.expanduser(
    os.environ.get("AZURE_TOKEN_CACHE") or "~/.cache/ch-azure-connect-jwt.json")

CH_HOST = os.environ.get("CLICKHOUSE_HOST") or "localhost"
CH_PORT = int(os.environ.get("CLICKHOUSE_PORT") or "8123")


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


def device_code_flow():
    dev = requests.post(f"{BASE}/devicecode",
                        data={"client_id": CLIENT, "scope": SCOPE}, timeout=30)
    dev.raise_for_status()
    dev = dev.json()
    print(dev["message"], file=sys.stderr)
    interval = dev.get("interval", 5)
    deadline = time.time() + dev.get("expires_in", 900)
    bad = 0
    while time.time() < deadline:
        time.sleep(interval)
        r = requests.post(f"{BASE}/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": CLIENT, "device_code": dev["device_code"]}, timeout=30)
        if r.status_code == 200:
            return r.json()
        err = oauth_error(r)
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                interval += 5
            bad = 0
            continue
        if err is None and bad < 3:  # tolerate a few transient blips
            bad += 1
            continue
        raise RuntimeError(f"device-code flow failed: {r.text}")
    raise RuntimeError("device-code flow timed out before sign-in")


def refresh(refresh_token):
    r = requests.post(f"{BASE}/token", data={
        "grant_type": "refresh_token", "client_id": CLIENT,
        "refresh_token": refresh_token, "scope": SCOPE}, timeout=30)
    r.raise_for_status()
    return r.json()


def renew(tokens):
    refresh_token = tokens.get("refresh_token")
    if refresh_token:
        try:
            return refresh(refresh_token)
        except requests.HTTPError as exc:
            # only a dead / interaction-required token warrants re-auth; else transient
            if oauth_error(exc.response) not in ("invalid_grant", "interaction_required"):
                raise RuntimeError(f"token refresh failed, retry later: {exc}") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"token refresh failed (network), retry later: {exc}") from exc
    return device_code_flow()


def save(tokens):
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    fd = os.open(CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 0600: holds a refresh token
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tokens, f)


def load():
    try:
        with open(CACHE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def make_token_provider():
    tokens = load()
    last_issued = None

    def token_provider():
        nonlocal last_issued
        access_token = tokens.get("access_token")
        # reuse the cached token unless expired or just rejected (== last_issued)
        if access_token and time.time() < token_exp(access_token) - 60 \
                and access_token != last_issued:
            last_issued = access_token
            return access_token

        new = renew(tokens)
        new.setdefault("refresh_token", tokens.get("refresh_token"))
        tokens.clear()
        tokens.update(new)
        save(tokens)
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
