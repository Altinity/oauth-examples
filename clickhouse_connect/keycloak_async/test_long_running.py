"""
Long-running session: one clickhouse-connect client kept alive across many
token-refresh cycles, the way a service holds a connection for hours.

Here the access token lives 15s (realm.json), so a handful of cycles takes a
minute instead of hours; set LONG_RUNNING_CYCLES to run longer. Each cycle
waits out the token, issues a query, and checks the client recovered with
exactly one more refresh grant and zero password re-auths — i.e. the refresh
token alone sustains the session across every cycle.

Run: python test_long_running.py  (stack must be up)
"""
import asyncio
import contextlib
import os
import time

import clickhouse_connect

from app import (CH_HOST, CH_PORT, USERS, KeycloakTokenProvider,
                 assert_idp_saw, burst_start_ms)

CYCLES = int(os.environ.get("LONG_RUNNING_CYCLES", "3"))


async def main() -> None:
    user, password = USERS[0]
    print(f"[long-running] one client, {CYCLES} refresh cycles as {user}")
    provider = KeycloakTokenProvider(user, password)
    client = await clickhouse_connect.create_async_client(
        host=CH_HOST, port=CH_PORT, token_provider=provider)
    try:
        for cycle in range(1, CYCLES + 1):
            # +5s margin: exp comes from Keycloak's clock, which can lag the host
            wait = provider.access_token_exp - time.time() + 5
            print(f"    cycle {cycle}/{CYCLES}: waiting {max(wait, 0):.0f}s "
                  "for the token to expire ...")
            await asyncio.sleep(max(wait, 0))

            since = burst_start_ms()
            user_now = (await client.query(
                "SELECT currentUser()")).result_rows[0][0]
            assert user_now == user, f"got {user_now!r} in cycle {cycle}"
            assert provider.counters["refresh_grants"] == cycle, \
                f"cycle {cycle}: {dict(provider.counters)}"
            assert provider.counters["password_grants"] == 1, \
                f"unexpected re-auth: {dict(provider.counters)}"
            await assert_idp_saw(since, refreshes=1)
            print(f"    cycle {cycle}/{CYCLES}: query OK, "
                  f"{dict(provider.counters)}")

        assert provider.counters["refresh_grants"] == CYCLES
        print(f"\n    OK: one client survived {CYCLES} refresh cycles, "
              f"{CYCLES} refresh grants, 1 password grant (the initial sign-in)")
    finally:
        # suppress so a failing close can't mask a cycle's AssertionError
        with contextlib.suppress(Exception):
            await client.close()
        with contextlib.suppress(Exception):
            await provider.aclose()
    print("\nlong-running session test passed")


if __name__ == "__main__":
    asyncio.run(main())
