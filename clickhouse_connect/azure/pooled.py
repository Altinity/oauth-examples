"""
Azure Entra delegated sign-in shared by a pool of clickhouse-connect clients.

One token_provider serves every client in the pool. A lock plus a short reuse
window means a burst of callers — pool warm-up, or every client meeting an
expired token at once — costs exactly one token-endpoint call.

This provider never opens a browser. It runs under a lock on behalf of clients
that may be serving requests, so a dead refresh token raises ReauthRequired
rather than blocking the whole pool for the length of a sign-in; the fix is to
run interactive.py once, which seeds the shared refresh-token cache. web_app.py
takes the same line and redirects to /login instead.

Renewals are wrapped in interactive.py's cache_lock so that processes sharing
the cache file cannot spend the same rotating refresh token twice.

Why a pool of clients: each gets its own ClickHouse session, and the driver
rejects concurrent queries that share a session id. One shared client also
works if you pass autogenerate_session_id=False, which drops the session id
and the guard with it — that is what web_app.py does. Use a pool when you want
per-thread sessions, a shared client when you do not.

The OAuth plumbing (refresh, refresh-token cache) is imported from
interactive.py; this file is only about the sharing layer.

Env:
  as interactive.py, plus
  CH_POOL_SIZE          optional; default 4
"""
import base64
import contextlib
import json
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import clickhouse_connect
import requests

from interactive import (CH_HOST, CH_PORT, cache_lock, load_refresh_token,
                         oauth_error, refresh, save_refresh_token)

POOL_SIZE = int(os.environ.get("CH_POOL_SIZE") or "4")
BURST_WINDOW = 10.0  # a renewal this recent already answers concurrent callers
FAILURE_WINDOW = 5.0  # a failure this recent answers them too, without retrying
EXP_SKEW = 60        # treat tokens expiring within a minute as expired


class ReauthRequired(RuntimeError):
    """The refresh token is gone or dead; only interactive.py can fix it."""


def token_exp(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["exp"]
    except Exception:
        return 0  # undecodable -> treat as expired


class SharedToken:
    """One access token for the whole pool, renewed once per burst of callers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._access_token = None
        self._refresh_token = load_refresh_token()
        self._renewed_at = 0.0
        self._failure = None
        self._failed_at = 0.0
        self.renewals = 0  # how many times we hit the token endpoint

    def __call__(self):
        """The token_provider handed to every client in the pool."""
        with self._lock:
            if self._usable():
                return self._access_token
            # a peer failed moments ago; report that instead of retrying, so a
            # burst of rejected clients does not become a burst of IdP calls
            if self._failure and time.monotonic() - self._failed_at < FAILURE_WINDOW:
                raise self._failure
            try:
                tokens = self._renew()
            except Exception as exc:
                self._failure, self._failed_at = exc, time.monotonic()
                raise
            self._failure = None
            self._access_token = tokens["access_token"]
            self._renewed_at = time.monotonic()
            return self._access_token

    def _usable(self):
        # both halves matter: time.monotonic() does not advance across system
        # suspend, so the window on its own can hand back an expired token
        return bool(self._access_token
                    and time.time() < token_exp(self._access_token) - EXP_SKEW
                    and time.monotonic() - self._renewed_at < BURST_WINDOW)

    def _renew(self):
        with cache_lock():
            # another process may have rotated the token since we last looked
            self._refresh_token = load_refresh_token() or self._refresh_token
            if not self._refresh_token:
                raise ReauthRequired(
                    "no cached refresh token — run `python interactive.py` first")
            try:
                tokens = refresh(self._refresh_token)
            except requests.HTTPError as exc:
                if oauth_error(exc.response) in ("invalid_grant", "interaction_required"):
                    raise ReauthRequired(
                        "refresh token rejected — run `python interactive.py` again") from exc
                raise RuntimeError(f"token refresh failed, retry later: {exc}") from exc
            except requests.RequestException as exc:
                raise RuntimeError(f"token refresh failed (network), retry later: {exc}") from exc
            self._refresh_token = tokens.get("refresh_token")
            save_refresh_token(self._refresh_token)
            self.renewals += 1
            return tokens

    @property
    def access_token(self):
        with self._lock:
            return self._access_token


class ClientPool:
    """Fixed set of clients, one borrower at a time, all sharing one token."""

    def __init__(self, size, token_provider):
        built = []
        built_lock = threading.Lock()

        def build(_):
            client = clickhouse_connect.get_client(
                host=CH_HOST, port=CH_PORT, token_provider=token_provider)
            with built_lock:
                built.append(client)
            return client

        # built concurrently on purpose: `size` clients call the provider at
        # once, which is exactly the burst the reuse window exists for
        try:
            with ThreadPoolExecutor(max_workers=size) as pool:
                self.clients = list(pool.map(build, range(size)))
        except BaseException:
            # one failure aborts the pool; close the siblings that did connect
            # or their sockets leak with nothing left holding a reference
            for client in built:
                with contextlib.suppress(Exception):
                    client.close()
            raise
        self._idle = queue.Queue()
        for client in self.clients:
            self._idle.put(client)

    @contextlib.contextmanager
    def borrow(self, timeout=30):
        try:
            client = self._idle.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                f"no pooled client free within {timeout}s") from None
        try:
            yield client
        finally:
            self._idle.put(client)

    def close(self):
        for client in self.clients:
            with contextlib.suppress(Exception):
                client.close()


def tamper(token):
    """A well-formed JWT with a broken signature, so ClickHouse rejects it."""
    head, payload, sig = token.split(".")
    return f"{head}.{payload}.{'A' * len(sig)}"


def main():
    token = SharedToken()
    try:
        pool = ClientPool(POOL_SIZE, token)
    except ReauthRequired as exc:
        raise SystemExit(f"{exc}") from exc
    # public property, same PoolManager the backend holds
    shared_http = len({id(c.http) for c in pool.clients}) == 1
    print(f"{POOL_SIZE} clients built concurrently")
    print(f"  token-endpoint calls   : {token.renewals}")
    print(f"  shared urllib3 pool    : {shared_http}")

    def who(_):
        with pool.borrow() as client:
            return client.query("SELECT currentUser()").result_rows[0][0]

    queries = POOL_SIZE * 4
    with ThreadPoolExecutor(max_workers=POOL_SIZE * 2) as workers:
        users = set(workers.map(who, range(queries)))
    print(f"\n{queries} concurrent queries over {POOL_SIZE} clients")
    print(f"  currentUser()          : {', '.join(users)}")
    print(f"  token-endpoint calls   : {token.renewals}")

    # every client rejected at once, while the shared token is still good
    for client in pool.clients:
        client.set_access_token(tamper(token.access_token))
    before = token.renewals
    with ThreadPoolExecutor(max_workers=POOL_SIZE) as workers:
        users = set(workers.map(who, range(POOL_SIZE)))
    print(f"\nall {POOL_SIZE} clients rejected, shared token still valid")
    print(f"  currentUser()          : {', '.join(users)}")
    print(f"  token-endpoint calls   : {token.renewals - before} (reused, no renewal)")

    # same again once the reuse window has passed: one renewal covers them all
    print(f"\nwaiting out the {BURST_WINDOW:.0f}s reuse window...")
    time.sleep(BURST_WINDOW + 0.5)
    for client in pool.clients:
        client.set_access_token(tamper(token.access_token))
    before = token.renewals
    with ThreadPoolExecutor(max_workers=POOL_SIZE) as workers:
        users = set(workers.map(who, range(POOL_SIZE)))
    print(f"all {POOL_SIZE} clients rejected, window expired")
    print(f"  currentUser()          : {', '.join(users)}")
    print(f"  token-endpoint calls   : {token.renewals - before} (one for the whole burst)")

    pool.close()


if __name__ == "__main__":
    main()
