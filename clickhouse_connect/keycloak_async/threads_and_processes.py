"""
Threads and processes querying ClickHouse through the sync clickhouse-connect
client while the token expires and refreshes mid-run.

The async provider in app.py is event-loop-bound; sync clients running in
threads need a threading.Lock-based provider instead. A single sync client
can be shared across threads (scenario C) as long as session IDs are off;
with sessions on you need a client per thread (scenario T). Processes always
need their own client. The provider is the shared piece throughout (threads
share the instance, processes share the refresh token).

Scenarios (`python threads_and_processes.py`):
  C. shared-client — one provider AND one client shared by every thread
                 (autogenerate_session_id=False); concurrent queries overlap
                 on the one client and the expiry burst yields one refresh
  T. threads   — one thread-safe provider, one client per thread; queries
                 keep running across an expiry; the 516 burst from all
                 threads yields one refresh grant
  P. processes — each process gets its own provider seeded with the same
                 refresh token; every process survives the expiry with
                 exactly one refresh grant of its own
"""
import base64
import contextlib
import json
import os
import threading
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import clickhouse_connect
import requests

KEYCLOAK_BASE_URL = os.environ.get(
    "KEYCLOAK_BASE_URL",
    f"http://localhost:{os.environ.get('KEYCLOAK_PORT', '8080')}",
)
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "ch-demo")
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "clickhouse-client")
KEYCLOAK_USERNAME = os.environ.get("KEYCLOAK_USERNAME", "alice@example.com")
KEYCLOAK_PASSWORD = os.environ.get("KEYCLOAK_PASSWORD", "demo")

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


class ThreadSafeTokenProvider:
    """Single-flight sync `token_provider`, sharable across threads.

    Same contract as app.py's async provider — called at client init and once
    per 516-rejected request — but locked with threading.Lock so any number
    of thread-resident clients can share one instance. Seeding `tokens`
    starts from an existing session (e.g. a refresh token handed to a child
    process) instead of a fresh password grant.
    """

    EXP_SKEW = 3         # treat tokens expiring within 3s as already expired
    BURST_WINDOW = 10.0  # a 516 burst reuses a token renewed < 10s ago

    def __init__(self, username: str, password: str,
                 tokens: dict | None = None):
        self._token_url = (f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}"
                           "/protocol/openid-connect/token")
        self._username = username
        self._password = password
        self._tokens = dict(tokens) if tokens else {}
        self._lock = threading.Lock()
        # seeded tokens count as freshly renewed so init calls reuse them
        self._renewed_at = time.monotonic() if tokens else float("-inf")
        self.counters: Counter = Counter()

    def __call__(self) -> str:
        with self._lock:
            self.counters["provider_calls"] += 1
            access = self._tokens.get("access_token")
            if access and time.time() < token_exp(access) - self.EXP_SKEW:
                # a renewal within the burst window already answered this
                # caller's 516; a valid token outside it was truly rejected
                if time.monotonic() - self._renewed_at < self.BURST_WINDOW:
                    return access
            self._renew()
            return self._tokens["access_token"]

    @property
    def access_token_exp(self) -> int:
        return token_exp(self._tokens.get("access_token", ""))

    def _renew(self) -> None:
        refresh_token = self._tokens.get("refresh_token")
        tokens = None
        if refresh_token:
            try:
                tokens = self._grant({
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
            tokens = self._grant({
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

    def _grant(self, fields: dict) -> dict:
        resp = requests.post(self._token_url,
                             data={"client_id": KEYCLOAK_CLIENT_ID, **fields},
                             timeout=10)
        try:
            body = resp.json()
        except ValueError:
            body = {}  # proxy error page, IdP mid-boot: classify below
        if resp.status_code != 200 or "access_token" not in body:
            raise OAuthError(body.get("error", f"http_{resp.status_code}"),
                             body.get("error_description", resp.text[:200]))
        return body


def keycloak_events_since(since_ms: int) -> Counter:
    """Event counts from Keycloak's admin API — verifies grant counts
    against the IdP's own records instead of the provider's counters."""
    resp = requests.post(
        f"{KEYCLOAK_BASE_URL}/realms/master/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "admin-cli",
              "username": KEYCLOAK_ADMIN_USER,
              "password": KEYCLOAK_ADMIN_PASSWORD},
        timeout=10)
    resp.raise_for_status()
    admin_token = resp.json()["access_token"]
    resp = requests.get(
        f"{KEYCLOAK_BASE_URL}/admin/realms/{KEYCLOAK_REALM}/events",
        params={"max": "500"},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=10)
    resp.raise_for_status()
    return Counter(e["type"] for e in resp.json() if e["time"] >= since_ms)


def assert_idp_saw(since_ms: int, refreshes: int) -> None:
    events = keycloak_events_since(since_ms)
    assert events.get("REFRESH_TOKEN", 0) == refreshes, \
        f"Keycloak logged {dict(events)}, expected {refreshes} REFRESH_TOKEN"
    assert events.get("LOGIN", 0) == 0, \
        f"Keycloak logged a LOGIN (password re-auth) during the burst: {dict(events)}"


def wait_for_expiry(provider: ThreadSafeTokenProvider,
                    announce: bool = True) -> None:
    # +5s margin: exp comes from Keycloak's clock, which can lag the host
    wait = provider.access_token_exp - time.time() + 5
    if announce:
        print(f"    waiting {max(wait, 0):.0f}s for the token to expire ...")
    time.sleep(max(wait, 0))


def scenario_shared_client(n_threads: int = 8, n_queries: int = 3) -> None:
    """One client shared by every thread (not one per thread), expiry mid-run.

    A single sync client is safe to share across threads only with
    autogenerate_session_id=False. By default a sync client stamps every
    request with one generated session_id, and the driver rejects a second
    concurrent query on that session with ProgrammingError ("Attempt to
    execute concurrent queries within the same session"). With no session_id
    the queries run concurrently over the client's own connection pool.

    Auth is unaffected by sharing: each request copies the client's headers,
    and on a 516 the driver calls the (single-flight) provider and updates the
    shared Bearer header — so the whole burst still collapses into one refresh.
    """
    print(f"\n[C] shared-client: {n_threads} threads on ONE shared client")
    provider = ThreadSafeTokenProvider(KEYCLOAK_USERNAME, KEYCLOAK_PASSWORD)
    client = clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, token_provider=provider,
        autogenerate_session_id=False)  # required to share one client
    try:
        assert provider.counters["password_grants"] == 1, provider.counters

        def worker(_: int) -> list:
            return [client.query("SELECT currentUser(), sleep(0.2)").result_rows[0][0]
                    for _ in range(n_queries)]

        before = provider.counters.copy()
        started = time.monotonic()
        with ThreadPoolExecutor(n_threads) as pool:
            results = list(pool.map(worker, range(n_threads)))
        elapsed = time.monotonic() - started
        assert all(user == KEYCLOAK_USERNAME
                   for users in results for user in users)
        assert provider.counters == before, \
            f"unexpected token activity: {dict(provider.counters)}"
        # each query sleeps 0.2s server-side; fully serial would be
        # n_threads * n_queries * 0.2s — overlap on one client is far quicker
        total = n_threads * n_queries
        assert elapsed < total * 0.2 * 0.6, \
            f"queries did not overlap on the shared client ({elapsed:.1f}s)"
        print(f"    OK: {total} queries overlapped on one client "
              f"in {elapsed:.1f}s, no extra token fetches")

        wait_for_expiry(provider)

        before = provider.counters.copy()
        # 2s slack: Keycloak stamps events with its own clock
        since = int(time.time() * 1000) - 2000
        with ThreadPoolExecutor(n_threads) as pool:
            futures = [pool.submit(worker, i) for i in range(n_threads)]
        failures = [f.exception() for f in futures if f.exception() is not None]
        assert not failures, (f"{len(failures)}/{len(futures)} threads failed "
                              f"to recover; first: {failures[0]!r}")
        assert all(user == KEYCLOAK_USERNAME
                   for f in futures for user in f.result())
        delta_refresh = provider.counters["refresh_grants"] - before["refresh_grants"]
        delta_password = provider.counters["password_grants"] - before["password_grants"]
        assert delta_refresh == 1, f"expected exactly 1 refresh grant, got {delta_refresh}"
        assert delta_password == 0, "re-auth must not happen while the refresh token is valid"
        assert_idp_saw(since, refreshes=1)
        print(f"    counters: {dict(provider.counters)}")
        print(f"    OK: {n_threads} threads on one client recovered -> "
              "1 refresh grant (confirmed by Keycloak's event log)")
    finally:
        with contextlib.suppress(Exception):
            client.close()


def scenario_threads(n_threads: int = 8, n_queries: int = 4) -> None:
    """One shared provider, one client per thread, expiry mid-run."""
    print(f"\n[T] threads: {n_threads} threads, one shared provider")
    provider = ThreadSafeTokenProvider(KEYCLOAK_USERNAME, KEYCLOAK_PASSWORD)
    clients: list = []

    def worker(client) -> list:
        return [client.query("SELECT currentUser(), sleep(0.2)").result_rows[0][0]
                for _ in range(n_queries)]

    try:
        clients.extend(clickhouse_connect.get_client(
            host=CH_HOST, port=CH_PORT, token_provider=provider)
            for _ in range(n_threads))
        assert provider.counters["password_grants"] == 1, provider.counters

        before = provider.counters.copy()
        with ThreadPoolExecutor(n_threads) as pool:
            results = list(pool.map(worker, clients))
        assert all(user == KEYCLOAK_USERNAME
                   for users in results for user in users)
        assert provider.counters == before, \
            f"unexpected token activity: {dict(provider.counters)}"
        print(f"    OK: {n_threads * n_queries} queries, no extra token fetches")

        wait_for_expiry(provider)

        before = provider.counters.copy()
        # 2s slack: Keycloak stamps events with its own clock
        since = int(time.time() * 1000) - 2000
        with ThreadPoolExecutor(n_threads) as pool:
            futures = [pool.submit(worker, client) for client in clients]
        failures = [f.exception() for f in futures if f.exception() is not None]
        assert not failures, (f"{len(failures)}/{len(futures)} threads failed "
                              f"to recover; first: {failures[0]!r}")
        assert all(user == KEYCLOAK_USERNAME
                   for f in futures for user in f.result())
        delta_refresh = provider.counters["refresh_grants"] - before["refresh_grants"]
        delta_password = provider.counters["password_grants"] - before["password_grants"]
        assert delta_refresh == 1, f"expected exactly 1 refresh grant, got {delta_refresh}"
        assert delta_password == 0, "re-auth must not happen while the refresh token is valid"
        assert_idp_saw(since, refreshes=1)
        print(f"    counters: {dict(provider.counters)}")
        print(f"    OK: {n_threads}/{n_threads} threads recovered -> "
              "1 refresh grant (confirmed by Keycloak's event log)")
    finally:
        # keep closing on failure so one bad close can't mask the scenario
        # error or leak the remaining pools
        for client in clients:
            with contextlib.suppress(Exception):
                client.close()


def process_worker(tokens: dict) -> dict:
    """Runs in a child process: own provider (seeded with the parent's
    refresh token), own client; queries across an expiry."""
    provider = ThreadSafeTokenProvider(KEYCLOAK_USERNAME, KEYCLOAK_PASSWORD,
                                       tokens=tokens)
    client = clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, token_provider=provider)
    try:
        for _ in range(3):
            user = client.query("SELECT currentUser()").result_rows[0][0]
            assert user == KEYCLOAK_USERNAME
        wait_for_expiry(provider, announce=False)
        for _ in range(3):
            user = client.query("SELECT currentUser()").result_rows[0][0]
            assert user == KEYCLOAK_USERNAME
        return dict(provider.counters)
    finally:
        client.close()


def scenario_processes(n_procs: int = 3) -> None:
    """Each process refreshes independently off one shared refresh token.

    Requires refresh-token rotation to be off (Keycloak's default) — with
    rotation on, the first child's refresh would invalidate the token for
    the rest and they would fall back to password grants.
    """
    print(f"\n[P] processes: {n_procs} processes, one shared refresh token")
    parent = ThreadSafeTokenProvider(KEYCLOAK_USERNAME, KEYCLOAK_PASSWORD)
    parent()  # one password grant; children inherit the session
    print("    (each process waits out the expiry on its own)")

    with ProcessPoolExecutor(n_procs) as pool:
        counter_dumps = list(pool.map(process_worker,
                                      [dict(parent._tokens)] * n_procs))

    for i, counters in enumerate(counter_dumps):
        # a slow spawn can expire the seeded token before the child's client
        # init, adding one extra refresh — never a password re-auth
        assert counters.get("refresh_grants") in (1, 2), (i, counters)
        assert not counters.get("password_grants"), (i, counters)
        print(f"    process {i}: {counters}")
    print(f"    OK: {n_procs} processes refreshed independently, 0 password grants")


def main() -> None:
    scenario_shared_client()
    scenario_threads()
    scenario_processes()
    print("\nall scenarios passed")


if __name__ == "__main__":
    main()
