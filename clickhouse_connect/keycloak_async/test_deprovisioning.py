"""
User de-provisioning: what happens when a user loses access in the IdP while
a clickhouse-connect session is live.

ClickHouse validates the bearer token's signature and expiry against the
JWKS on each request; it does not call back to the IdP. So a token issued
before de-provisioning keeps working until it expires — access is effectively
re-checked at the IdP only when the client needs a NEW token (a refresh after
expiry). This script disables `erin@example.com` in Keycloak mid-session and
shows exactly that boundary, then re-enables her to show recovery.

Mutates Keycloak state (disable/enable erin) but always restores it.

Run: python test_deprovisioning.py  (stack must be up; runtime ~25s)
"""
import asyncio
import contextlib
import time

import clickhouse_connect
import requests

from app import (CH_HOST, CH_PORT, KEYCLOAK_ADMIN_PASSWORD, KEYCLOAK_ADMIN_USER,
                 KEYCLOAK_BASE_URL, KEYCLOAK_REALM, KeycloakTokenProvider,
                 OAuthError)

USER = "erin@example.com"
PASSWORD = "demo"


def _admin_token() -> str:
    resp = requests.post(
        f"{KEYCLOAK_BASE_URL}/realms/master/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "admin-cli",
              "username": KEYCLOAK_ADMIN_USER, "password": KEYCLOAK_ADMIN_PASSWORD},
        timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def set_enabled(enabled: bool) -> None:
    token = _admin_token()
    headers = {"Authorization": f"Bearer {token}"}
    users = requests.get(
        f"{KEYCLOAK_BASE_URL}/admin/realms/{KEYCLOAK_REALM}/users",
        params={"username": USER, "exact": "true"}, headers=headers,
        timeout=10).json()
    resp = requests.put(
        f"{KEYCLOAK_BASE_URL}/admin/realms/{KEYCLOAK_REALM}/users/{users[0]['id']}",
        json={"enabled": enabled}, headers=headers, timeout=10)
    resp.raise_for_status()


async def current_user(client) -> str:
    return (await client.query("SELECT currentUser()")).result_rows[0][0]


async def main() -> None:
    set_enabled(True)  # defensive reset in case a prior run aborted mid-way
    provider = KeycloakTokenProvider(USER, PASSWORD)
    client = await clickhouse_connect.create_async_client(
        host=CH_HOST, port=CH_PORT, token_provider=provider)
    try:
        assert await current_user(client) == USER
        print(f"OK baseline: {USER} authenticates")

        set_enabled(False)
        print(f"   {USER} disabled in Keycloak (mid-session)")

        # the already-issued token is still a valid, unexpired JWT and CH does
        # not re-check the IdP, so the live session keeps working
        assert await current_user(client) == USER
        print("OK live session survives: CH validates the token, not the IdP")

        # wait past exp (+cache) so CH rejects the token and the client must
        # renew — the renewal is where de-provisioning bites
        wait = provider.access_token_exp - time.time() + 6
        print(f"   waiting {max(wait, 0):.0f}s for the token to expire ...")
        await asyncio.sleep(max(wait, 0))

        try:
            await current_user(client)
            raise AssertionError("query succeeded after de-provisioning")
        except OAuthError as exc:
            # refresh fails (session revoked) then password fails (disabled);
            # the clean OAuthError is what the caller sees
            assert exc.error == "invalid_grant", exc
            print(f"OK revoked on renewal: client query raised -> {exc}")

        set_enabled(True)
        print(f"   {USER} re-enabled in Keycloak")
        assert await current_user(client) == USER
        print("OK recovery: access restored on the next query after re-provisioning")
    finally:
        set_enabled(True)  # never leave erin disabled
        with contextlib.suppress(Exception):
            await client.close()
        with contextlib.suppress(Exception):
            await provider.aclose()
    print("\nde-provisioning test passed")


if __name__ == "__main__":
    asyncio.run(main())
