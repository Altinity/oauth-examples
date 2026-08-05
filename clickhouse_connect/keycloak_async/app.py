"""
Keycloak -> clickhouse-connect AsyncClient via an async, refresh-token-aware
token_provider (PR #775; async providers need clickhouse-connect[async] 1.4.0+).

Scenarios (`python app.py`):
  1. parallel — concurrent queries as two users; one token fetch per user
  2. expiry   — concurrent queries on an expired token; one refresh grant
  3. fallback — dead refresh token; one password-grant fallback
  4. shared   — several clients on one provider (one refresh token); their
                combined 516 burst still yields one refresh grant
  5. stampede — both users burst on already-expired tokens; one refresh
                grant each, from two providers renewing at the same instant
  6. in-flight — a query longer than the token lifespan completes; CH checks
                the token only at query start, so only the next query refreshes
  7. unique-token — N clients with their own providers get distinct tokens

See test_long_running.py (many refresh cycles), test_deprovisioning.py (IdP
revocation), test_negative.py (rejected tokens), threads_and_processes.py
(sync threads/processes), test_provider.py (offline provider unit tests).

ROPC is deprecated by OAuth2 BCP — local demo only; the provider logic is
grant-agnostic.
"""
import asyncio
import base64
import contextlib
import json
import os
import time
from collections import Counter

import aiohttp
import clickhouse_connect

KEYCLOAK_BASE_URL = os.environ.get(
    "KEYCLOAK_BASE_URL",
    f"http://localhost:{os.environ.get('KEYCLOAK_PORT', '8080')}",
)
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "ch-demo")
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "clickhouse-client")

USERS = [
    (os.environ.get("KEYCLOAK_USERNAME", "alice@example.com"),
     os.environ.get("KEYCLOAK_PASSWORD", "demo")),
    (os.environ.get("KEYCLOAK_USERNAME_2", "bob@example.com"),
     os.environ.get("KEYCLOAK_PASSWORD_2", "demo")),
]

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CH_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))

KEYCLOAK_ADMIN_USER = os.environ.get("KEYCLOAK_ADMIN_USER", "admin")
KEYCLOAK_ADMIN_PASSWORD = os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "admin")


def token_exp(token: str) -> int:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["exp"]
    except Exception:
        return 0  # undecodable -> treat as expired


class OAuthError(RuntimeError):
    def __init__(self, error: str, description: str = ""):
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error


class KeycloakTokenProvider:
    """Single-flight async `token_provider` for clickhouse-connect.

    The driver awaits it at client init and once per request rejected with
    516; the lock ensures one renewal (refresh_token grant, then password
    grant) per burst. `counters` lets the scenarios assert on behavior.
    """

    EXP_SKEW = 3         # treat tokens expiring within 3s as already expired
    BURST_WINDOW = 10.0  # a 516 burst reuses a token renewed < 10s ago

    def __init__(self, username: str, password: str):
        self._token_url = (f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}"
                           "/protocol/openid-connect/token")
        self._username = username
        self._password = password
        self._tokens: dict = {}
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._renewed_at = float("-inf")
        self.counters: Counter = Counter()

    async def __call__(self) -> str:
        async with self._lock:
            self.counters["provider_calls"] += 1
            access = self._tokens.get("access_token")
            if access and time.time() < token_exp(access) - self.EXP_SKEW:
                # a renewal within the burst window already answered this
                # caller's 516; a valid token outside it was truly rejected
                if time.monotonic() - self._renewed_at < self.BURST_WINDOW:
                    return access
            await self._renew()
            return self._tokens["access_token"]

    async def _renew(self) -> None:
        refresh_token = self._tokens.get("refresh_token")
        tokens = None
        if refresh_token:
            try:
                tokens = await self._grant({
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                })
                self.counters["refresh_grants"] += 1
            except OAuthError as exc:
                # invalid_grant (refresh token dead) -> re-auth below;
                # anything else is not fixable by re-authenticating
                if exc.error != "invalid_grant":
                    raise
                self.counters["refresh_rejected"] += 1
        if tokens is None:
            # no scope needed: the forwarded access token carries
            # preferred_username by default
            tokens = await self._grant({
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
            })
            self.counters["password_grants"] += 1
        # RFC 6749 §6 lets the IdP omit refresh_token in a refresh response;
        # keep the stored one so renewal doesn't degrade to password grants
        if "refresh_token" not in tokens and "refresh_token" in self._tokens:
            tokens["refresh_token"] = self._tokens["refresh_token"]
        self._tokens = tokens
        self._renewed_at = time.monotonic()

    async def _grant(self, fields: dict) -> dict:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10))
        data = {"client_id": KEYCLOAK_CLIENT_ID, **fields}
        async with self._session.post(self._token_url, data=data) as resp:
            text = await resp.text()
            try:
                body = json.loads(text)
            except ValueError:
                body = {}  # proxy error page, IdP mid-boot: classify below
            if resp.status != 200 or "access_token" not in body:
                raise OAuthError(body.get("error", f"http_{resp.status}"),
                                 body.get("error_description", text[:200]))
            return body

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def access_token(self) -> str | None:
        return self._tokens.get("access_token")

    @property
    def access_token_exp(self) -> int:
        return token_exp(self._tokens.get("access_token", ""))

    def drop_access_token(self) -> None:
        self._tokens.pop("access_token", None)

    def corrupt_refresh_token(self) -> None:
        self._tokens["refresh_token"] = "no-longer-valid"


def show(label: str, provider: KeycloakTokenProvider) -> None:
    print(f"    {label}: {dict(provider.counters)}")


def burst_start_ms() -> int:
    # 6s slack: Keycloak stamps events with its own clock, which the expiry
    # waits budget at up to 5s behind the host; prior grants are >=15s older
    return int(time.time() * 1000) - 6000


async def keycloak_events_since(since_ms: int) -> Counter:
    """Event counts from Keycloak's admin API — verifies grant counts
    against the IdP's own records instead of the provider's counters."""
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        resp = await session.post(
            f"{KEYCLOAK_BASE_URL}/realms/master/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": "admin-cli",
                  "username": KEYCLOAK_ADMIN_USER,
                  "password": KEYCLOAK_ADMIN_PASSWORD})
        resp.raise_for_status()
        admin_token = (await resp.json())["access_token"]
        resp = await session.get(
            f"{KEYCLOAK_BASE_URL}/admin/realms/{KEYCLOAK_REALM}/events",
            params={"max": "500"},
            headers={"Authorization": f"Bearer {admin_token}"})
        resp.raise_for_status()
        events = await resp.json()
    return Counter(e["type"] for e in events if e["time"] >= since_ms)


async def assert_idp_saw(since_ms: int, refreshes: int) -> None:
    events = await keycloak_events_since(since_ms)
    assert events.get("REFRESH_TOKEN", 0) == refreshes, \
        f"Keycloak logged {dict(events)}, expected {refreshes} REFRESH_TOKEN"
    assert events.get("LOGIN", 0) == 0, \
        f"Keycloak logged a LOGIN (password re-auth) during the burst: {dict(events)}"


def assert_all_recovered(results: list) -> list:
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, (f"{len(failures)}/{len(results)} requests failed "
                          f"to recover; first: {failures[0]!r}")
    return results


async def scenario_parallel(clients: dict, providers: dict, n: int = 16) -> None:
    """N overlapping queries per user; no token activity beyond the init fetch."""
    print(f"\n[1] parallel: {n} concurrent queries x {len(clients)} users")
    before = {user: provider.counters.copy()
              for user, provider in providers.items()}

    async def one(user: str) -> str:
        result = await clients[user].query("SELECT currentUser(), sleep(0.3)")
        return result.result_rows[0][0]

    started = time.monotonic()
    tasks = [one(user) for user in clients for _ in range(n)]
    results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - started

    for i, user in enumerate(clients):
        got = results[i * n:(i + 1) * n]
        assert got == [user] * n, f"expected {user!r} everywhere, got {set(got)}"
    # each query sleeps 0.3s in CH; fully serial would take 2 * n * 0.3s,
    # truly parallel well under 1s — the bound leaves headroom for slow hosts
    assert elapsed < n * 0.45, f"queries did not overlap ({elapsed:.1f}s)"
    for user, provider in providers.items():
        assert provider.counters == before[user], \
            f"unexpected token activity: {dict(provider.counters)}"
        show(user, provider)
    print(f"    OK: {len(tasks)} queries in {elapsed:.1f}s, no extra token fetches")


async def scenario_expiry(client, provider: KeycloakTokenProvider,
                          user: str, n: int = 8) -> None:
    """Expired token: N concurrent 516s must collapse into one refresh grant."""
    # +5s margin: exp comes from Keycloak's clock, which can lag the host
    wait = provider.access_token_exp - time.time() + 5
    print(f"\n[2] expiry: waiting {max(wait, 0):.0f}s for {user}'s access token to expire ...")
    await asyncio.sleep(max(wait, 0))

    before = provider.counters.copy()
    since = burst_start_ms()
    tasks = [client.query("SELECT currentUser()") for _ in range(n)]
    results = assert_all_recovered(
        await asyncio.gather(*tasks, return_exceptions=True))

    assert all(r.result_rows[0][0] == user for r in results)
    delta_refresh = provider.counters["refresh_grants"] - before["refresh_grants"]
    delta_password = provider.counters["password_grants"] - before["password_grants"]
    delta_calls = provider.counters["provider_calls"] - before["provider_calls"]
    assert delta_refresh == 1, f"expected exactly 1 refresh grant, got {delta_refresh}"
    assert delta_password == 0, "re-auth must not happen while the refresh token is valid"
    await assert_idp_saw(since, refreshes=1)
    show(user, provider)
    print(f"    OK: {n}/{n} recovered, {delta_calls} provider calls -> "
          "1 refresh grant (confirmed by Keycloak's event log)")


async def scenario_refresh_fallback(provider: KeycloakTokenProvider,
                                    user: str, n: int = 8) -> None:
    """Dead refresh token: N direct concurrent renewals -> one password grant."""
    print(f"\n[3] fallback: {user}'s refresh token is dead, {n} concurrent renewals")
    provider.corrupt_refresh_token()
    provider.drop_access_token()
    before = provider.counters.copy()

    tokens = await asyncio.gather(*[provider() for _ in range(n)])

    assert len(set(tokens)) == 1, "single-flight renewal must yield one shared token"
    assert time.time() < token_exp(tokens[0]), "renewed token must be valid"
    delta_rejected = provider.counters["refresh_rejected"] - before["refresh_rejected"]
    delta_password = provider.counters["password_grants"] - before["password_grants"]
    assert (delta_rejected, delta_password) == (1, 1), provider.counters
    show(user, provider)
    print(f"    OK: 1 rejected refresh -> 1 password grant, {n} callers share the token")


async def scenario_shared_provider(provider: KeycloakTokenProvider,
                                   user: str, n_clients: int = 3,
                                   n_queries: int = 4) -> None:
    """N clients share one provider (one refresh token): parallel queries,
    then a shared expiry where every client's 516s hit the same callable."""
    print(f"\n[4] shared: {n_clients} clients on one provider for {user}")
    clients = []
    try:
        for _ in range(n_clients):
            clients.append(await clickhouse_connect.create_async_client(
                host=CH_HOST, port=CH_PORT, token_provider=provider))

        before = provider.counters.copy()
        results = await asyncio.gather(*[
            client.query("SELECT currentUser(), sleep(0.2)")
            for client in clients for _ in range(n_queries)])
        assert all(r.result_rows[0][0] == user for r in results)
        assert provider.counters == before, \
            f"unexpected token activity: {dict(provider.counters)}"

        # +5s margin: exp comes from Keycloak's clock, which can lag the host
        wait = provider.access_token_exp - time.time() + 5
        print(f"    waiting {max(wait, 0):.0f}s for the shared token to expire ...")
        await asyncio.sleep(max(wait, 0))

        before = provider.counters.copy()
        since = burst_start_ms()
        results = assert_all_recovered(await asyncio.gather(*[
            client.query("SELECT currentUser()")
            for client in clients for _ in range(n_queries)],
            return_exceptions=True))
        assert all(r.result_rows[0][0] == user for r in results)
        delta_refresh = provider.counters["refresh_grants"] - before["refresh_grants"]
        delta_password = provider.counters["password_grants"] - before["password_grants"]
        delta_calls = provider.counters["provider_calls"] - before["provider_calls"]
        assert delta_refresh == 1, f"expected exactly 1 refresh grant, got {delta_refresh}"
        assert delta_password == 0, "re-auth must not happen while the refresh token is valid"
        await assert_idp_saw(since, refreshes=1)
        show(user, provider)
        print(f"    OK: {len(results)}/{len(results)} recovered across "
              f"{n_clients} clients, {delta_calls} provider calls -> "
              "1 refresh grant (confirmed by Keycloak's event log)")
    finally:
        for client in clients:
            with contextlib.suppress(Exception):
                await client.close()


async def scenario_expired_stampede(clients: dict, providers: dict,
                                    n: int = 12) -> None:
    """Every token already expired: both users' bursts stampede at once.

    Two providers renewing at the same instant — each must still land exactly
    one refresh grant, with no cross-user interference. n is per provider.
    """
    print(f"\n[5] stampede: {n} queries per user, all tokens expired")
    # +5s margin: exp comes from Keycloak's clock, which can lag the host
    wait = max(p.access_token_exp for p in providers.values()) - time.time() + 5
    print(f"    waiting {max(wait, 0):.0f}s for every token to expire ...")
    await asyncio.sleep(max(wait, 0))

    before = {user: provider.counters.copy()
              for user, provider in providers.items()}
    since = burst_start_ms()

    async def one(user: str) -> None:
        result = await clients[user].query("SELECT currentUser()")
        got = result.result_rows[0][0]
        assert got == user, f"identity mix-up: expected {user!r}, got {got!r}"

    results = assert_all_recovered(await asyncio.gather(
        *(one(user) for user in clients for _ in range(n)),
        return_exceptions=True))
    await assert_idp_saw(since, refreshes=len(providers))

    for user, provider in providers.items():
        delta_refresh = provider.counters["refresh_grants"] - before[user]["refresh_grants"]
        delta_password = provider.counters["password_grants"] - before[user]["password_grants"]
        assert delta_refresh == 1, f"{user}: expected 1 refresh grant, got {delta_refresh}"
        assert delta_password == 0, f"{user}: re-auth during a refreshable expiry"
        show(user, provider)
    print(f"    OK: {len(results)} queries survived expiry, "
          "1 refresh grant per user")


async def scenario_query_in_flight(user: str, password: str,
                                   query_seconds: int = 20) -> None:
    """A query longer than the token lifespan started on a fresh token still
    completes: ClickHouse checks the bearer token once at query start, so it
    expires mid-flight without a 516, and only the NEXT query refreshes."""
    print(f"\n[6] in-flight: a ~{query_seconds}s query on a token that "
          "expires mid-query")
    provider = KeycloakTokenProvider(user, password)
    client = await clickhouse_connect.create_async_client(
        host=CH_HOST, port=CH_PORT, token_provider=provider)
    try:
        lifespan = provider.access_token_exp - time.time()
        assert query_seconds > lifespan, \
            f"query ({query_seconds}s) must outlive the token ({lifespan:.0f}s)"
        before = provider.counters.copy()
        started = time.monotonic()
        # sum() forces sleepEachRow(1) to run per row; one row per block => ~N s
        # (count() alone would let the optimizer skip the sleep)
        result = await client.query(
            f"SELECT count(), sum(sleepEachRow(1)) FROM numbers({query_seconds})",
            settings={"max_block_size": 1})
        elapsed = time.monotonic() - started

        assert result.result_rows[0][0] == query_seconds
        assert elapsed >= lifespan, f"query returned too early ({elapsed:.0f}s)"
        assert provider.counters == before, \
            f"token was touched mid-query: {dict(provider.counters)}"
        assert provider.access_token_exp < time.time(), \
            "token should have expired during the query"
        print(f"    OK: {elapsed:.0f}s query finished on an expired token, "
              "no mid-flight refresh")

        since = burst_start_ms()
        assert (await client.query("SELECT currentUser()")
                ).result_rows[0][0] == user
        assert provider.counters["refresh_grants"] == 1, provider.counters
        await assert_idp_saw(since, refreshes=1)
        print("    OK: next query refreshed once (token had truly expired)")
    finally:
        with contextlib.suppress(Exception):
            await client.close()
        with contextlib.suppress(Exception):
            await provider.aclose()


async def scenario_unique_token(user: str, password: str, n: int = 3) -> None:
    """Connection pooling, the other shape: N clients each with their OWN
    provider get N DISTINCT access tokens (contrast scenario 4's shared one),
    and all authenticate as the same identity."""
    print(f"\n[7] unique-token: {n} clients, one provider each, same user")
    providers = [KeycloakTokenProvider(user, password) for _ in range(n)]
    clients: list = []
    try:
        for provider in providers:
            clients.append(await clickhouse_connect.create_async_client(
                host=CH_HOST, port=CH_PORT, token_provider=provider))
        tokens = [p.access_token for p in providers]
        assert len(set(tokens)) == n, "each client should hold a distinct token"
        results = await asyncio.gather(*(c.query("SELECT currentUser()")
                                         for c in clients))
        assert all(r.result_rows[0][0] == user for r in results)
        print(f"    OK: {n} distinct access tokens, all authenticate as {user}")
    finally:
        for client in clients:
            with contextlib.suppress(Exception):
                await client.close()
        for provider in providers:
            with contextlib.suppress(Exception):
                await provider.aclose()


async def main() -> None:
    providers = {user: KeycloakTokenProvider(user, password)
                 for user, password in USERS}
    alice = USERS[0][0]
    clients: dict = {}
    try:
        # pre-fetch tokens in parallel: auth errors surface before any driver
        # session exists (a failed create_async_client leaks its aiohttp
        # session), and client creation below reuses the cached tokens
        await asyncio.gather(*(provider() for provider in providers.values()))
        for user, provider in providers.items():
            clients[user] = await clickhouse_connect.create_async_client(
                host=CH_HOST, port=CH_PORT, token_provider=provider)

        await scenario_parallel(clients, providers)
        await scenario_expiry(clients[alice], providers[alice], alice)
        await scenario_refresh_fallback(providers[alice], alice)
        await scenario_shared_provider(providers[alice], alice)
        await scenario_expired_stampede(clients, providers)
        await scenario_query_in_flight(*USERS[0])
        await scenario_unique_token(*USERS[0])
    finally:
        # keep closing on failure so one bad close can't mask the scenario
        # error or leak the remaining sessions
        for client in clients.values():
            with contextlib.suppress(Exception):
                await client.close()
        for provider in providers.values():
            with contextlib.suppress(Exception):
                await provider.aclose()
    print("\nall scenarios passed")


if __name__ == "__main__":
    asyncio.run(main())
