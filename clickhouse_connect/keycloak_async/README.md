# `clickhouse-connect` AsyncClient + Keycloak, async refreshable `token_provider`

An asyncio Python script drives ClickHouse through
[`clickhouse-connect`](https://github.com/ClickHouse/clickhouse-connect)'s
`AsyncClient` with an **async `token_provider`** callable (introduced by
[PR #775](https://github.com/ClickHouse/clickhouse-connect/pull/775) in
1.2.0; awaiting an *async* provider needs `clickhouse-connect[async]`
1.4.0+) that
embeds the whole token lifecycle: initial ROPC sign-in to Keycloak,
silent renewal via the **refresh_token grant**, and re-auth fallback when
the refresh token dies. ClickHouse validates tokens against the realm's
JWKS and authenticates **pre-defined** `IDENTIFIED WITH jwt` users
(`alice@example.com`, `bob@example.com`) — no auto-provisioning.

Access tokens are deliberately short-lived (**15s**, a client attribute in
[`keycloak-config/realm.json`](keycloak-config/realm.json)) so expiry is
exercised in seconds, not minutes.

## How the driver + provider cooperate

- The driver invokes the provider **once at client init** and again
  **whenever ClickHouse rejects the current token** (`AUTHENTICATION_FAILED`,
  code 516 in the `X-ClickHouse-Exception-Code` header), then retries the
  request — once per request.
- With the aiohttp-based `AsyncClient`, an async provider is awaited on the
  event loop, so renewal never blocks other in-flight queries. (Sync
  callables also work — they're pushed to an executor thread.)
- N concurrent requests hitting an expired token produce N near-simultaneous
  provider invocations — the driver documents that the callable *must be safe
  to invoke in parallel*. The provider in [`app.py`](app.py) makes renewal
  **single-flight** with an `asyncio.Lock`: the first caller renews, the rest
  reuse the fresh token (a token renewed within the last 10s is trusted for
  the whole rejection burst). The flip side: if a *fresh* token is genuinely
  rejected inside that window, the driver's single retry re-sends it and the
  516 surfaces to the caller; the next call renews.

`python app.py` runs the scenarios below. Each expiry burst is verified
three ways: every request's outcome is collected and asserted recovered
(no exception slips through `gather`), the provider's own counters must
show exactly one refresh grant, and Keycloak's admin events API must have
logged exactly one `REFRESH_TOKEN` event and zero `LOGIN` events in the
burst window — so "one refresh" is confirmed by the IdP's records, not
just self-reporting (the realm enables `REFRESH_TOKEN` events explicitly;
it is not a default event type).

1. **parallel** — 16 overlapping queries × 2 users ⇒ exactly one token fetch
   per user;
2. **expiry** — waits out the 15s lifespan, fires 8 concurrent queries ⇒ all
   get 516, driver re-invokes the provider 8×, **exactly one**
   `refresh_token` grant renews, every query succeeds on retry;
3. **fallback** — refresh token is corrupted ⇒ 8 concurrent renewals collapse
   into one rejected refresh + one ROPC password grant;
4. **shared** — 3 separate `AsyncClient`s share one provider instance (one
   refresh token): parallel queries cause no token activity, and after the
   shared token expires, 12 concurrent 516s across all clients still collapse
   into a single `refresh_token` grant. Each client holds its own copy of the
   token in its `Authorization` header, so every client hits one 516 and
   re-invokes the shared callable — the burst window hands them the
   already-renewed token;
5. **stampede** — SELECTs and INSERTs from both users hit expired tokens
   simultaneously. Inserts are the risky half: the driver retries a
   516-rejected request only if it can replay the body (streamed insert
   bodies are rebuilt via its `retry_body` hook) — asserted here as
   exactly-once delivery (12/12 rows, no duplicates) plus one refresh grant
   per user;
6. **in-flight** — a query longer than the token lifespan (~20s query, 15s
   token) started on a fresh token still completes: ClickHouse validates the
   bearer token once at query start, so it expires mid-flight with no 516 and
   no refresh; the *next* query is the one that refreshes (proving the token
   really did expire during the long one);
7. **unique-token** — the connection-pooling counterpart to scenario 4: 3
   clients each with their **own** provider get 3 **distinct** access tokens,
   all authenticating as the same user. (Shared provider ⇒ one shared token;
   provider-per-client ⇒ unique token per client.)

## Scenario coverage

| Concern | Where |
| --- | --- |
| Concurrent refreshes; one refresh, others recover cleanly | `app.py` 2, 4, 5 (+ IdP event-log check) |
| Query/connection stampede on an expired token | `app.py` 2, 4, 5 |
| Refresh token expired / revoked → error + recovery | `app.py` 3 (fallback); `test_deprovisioning.py` |
| Invalid tokens: malformed, bad signature, wrong audience, clock skew | `test_negative.py` |
| Long-running session across many refresh cycles | `test_long_running.py` |
| User de-provisioned in the IdP mid-session | `test_deprovisioning.py` |
| Connection pooling — shared provider/token; same token across clients | `app.py` 4 |
| Connection pooling — unique token per client | `app.py` 7 |
| High concurrency across threads / processes | `threads_and_processes.py` |
| Query in flight while the token expires | `app.py` 6 |

## Does Keycloak support the refresh-token flow?

Yes — it's standard OIDC and on by default (`refresh_token` is listed in the
realm's `grant_types_supported`). Every grant that logs a user in, including
the ROPC password grant used here, returns a `refresh_token` alongside the
access token. Knobs that matter:

- **Access token lifespan** — realm-wide default 5 min; overridable per
  client via the `access.token.lifespan` attribute (15s here).
- **Refresh token lifetime** — bound to the SSO session: *SSO Session Idle*
  (default 30 min, `refresh_expires_in` in the response) and *SSO Session
  Max* (default 10h). Each refresh resets the idle timer.
- **Rotation** — off by default (the same refresh token can be reused); the
  realm's *Revoke Refresh Token* setting switches to one-time-use tokens.
  The provider here always stores the newest response, so it works either way.
- **Offline tokens** — request `scope=offline_access` for refresh tokens
  that survive SSO-session expiry (weeks, per realm policy).

## ClickHouse gotchas found while testing (Antalya 26.3 build)

Two processor defaults dwarf a 15s token, hiding expiry entirely — see
[`clickhouse-config/jwt_processors.xml`](clickhouse-config/jwt_processors.xml):

- `verifier_leeway` — clock-skew tolerance, **default 60s**: an expired token
  keeps validating for another minute. Set to `0` here.
- `token_cache_lifetime` — **default 3600s**, and contrary to the
  [docs](https://docs.altinity.com/altinityantalya/integrating-oauth/) a
  cached token keeps working until the cache entry lapses *even after its
  `exp` has passed*. Set to `10` here.

## Run

One-time setup — bring the stack up and install the client:

```bash
cp .env.example .env      # defaults work; edit ports if 8080/8123 are busy
docker compose up -d      # Keycloak + ClickHouse; wait ~15s for both

python -m venv .venv && source .venv/bin/activate   # Python 3.10+
pip install -r requirements.txt

set -a; source .env; set +a   # export the ports for the scripts (re-run per shell)
```

Then run any of the entry points (each is standalone; the stack must be up
except where noted):

```bash
python app.py                  # 7 async scenarios              ~2min
python test_provider.py        # offline provider unit tests     ~2s   (no stack needed)
python test_negative.py        # rejected-token paths            ~45s
python threads_and_processes.py# sync threads + processes        ~50s
python test_long_running.py    # session across refresh cycles   ~1min (LONG_RUNNING_CYCLES=N to extend)
python test_deprovisioning.py  # IdP de-provisioning + recovery  ~25s
```

Each prints per-check `OK ...` lines and ends with a `... passed` /
`all scenarios passed` line; any failure raises an `AssertionError` and exits
non-zero. `app.py`'s expected tail is `all scenarios passed` — most of its
runtime is waiting out the 15s token lifespan in scenarios 2, 4, 5 and 6.
See [Scenario coverage](#scenario-coverage) for which script covers what.

Tear down with `docker compose down -v` (the `-v` also drops the ClickHouse
volume, so users/roles are re-provisioned from `startup_scripts.xml` on the
next `up`).

`python test_provider.py` additionally stress-tests the provider offline
(no Docker): a fake IdP drives 50-caller stampedes, renewal failures,
cancelled waiters, refresh responses without a refresh_token, and the
driver's executor-thread invocation style.

`python test_negative.py` (~45s, stack must be up) probes what ClickHouse
*rejects*, asserting HTTP 403 + exception code 516 on each:

- **malformed tokens** — empty, non-JWT, missing segments, junk base64;
- **signatures** — `alg=none` forgery, a genuine alice token with the
  payload rewritten to bob (tamper = privilege-escalation attempt), a
  random signature, and a structurally valid JWT signed by a *different*
  realm's keys (Keycloak master realm — wrong JWKS);
- **principals / audience** — a valid token for `mallory@example.com`, who
  has no ClickHouse user (no auto-provisioning here); and per-user `CLAIMS`
  checks: `carol@example.com` is `IDENTIFIED WITH jwt CLAIMS
  '{"azp": "another-client"}'` so her (otherwise valid) token is rejected,
  while `dave@example.com` requires the real `azp` and passes — same token
  shape, only the claims requirement differs;
- **clock skew / expiry** — with `verifier_leeway=0` a token first
  presented after `exp` is rejected immediately; a token *cached* while
  valid keeps working past `exp` until `token_cache_lifetime` (10s) lapses,
  then is rejected — pinning down the cache-outlives-exp behavior above;
- **driver surface** — `clickhouse-connect` with a garbage `access_token=`
  raises a clean auth error rather than something opaque.

## Threads and processes

[`threads_and_processes.py`](threads_and_processes.py) covers the same
expiry/refresh events for the **sync** client (clickhouse-connect requires a
separate client instance per thread/process):

- **threads** — a `threading.Lock`-based `ThreadSafeTokenProvider` shared by
  8 per-thread clients; queries keep running across an expiry, and the
  combined 516 burst yields exactly one refresh grant;
- **processes** — 3 child processes each get their own provider seeded with
  the parent's refresh token; every process survives the expiry with exactly
  one refresh grant and no password re-auth. This relies on refresh-token
  rotation being off (Keycloak's default); with *Revoke Refresh Token*
  enabled, the first child's refresh invalidates the token for the rest and
  they fall back to password grants.

## Long-running sessions and de-provisioning

`python test_long_running.py` (~1min; `LONG_RUNNING_CYCLES` to run longer)
keeps **one** client alive across several token-refresh cycles — the way a
service holds a connection for hours. Each cycle waits out the token and
issues a query; the client recovers with exactly one more `refresh_token`
grant and zero password re-auths, so the refresh token alone sustains the
session. The 15s access-token lifespan compresses "hours" into a minute.

`python test_deprovisioning.py` (~25s) disables `erin@example.com` in
Keycloak mid-session, then shows the boundary the SOW calls out — *"token is
verified only when the session is initiated / on reconnection"*:

- the already-issued token keeps working after she is disabled (ClickHouse
  validates the JWT signature/expiry against the JWKS; it never calls back to
  the IdP), so the live session is unaffected;
- once the token expires and the client must renew, the refresh is rejected
  (session revoked) and the password fallback is rejected too (`Account
  disabled`) — the client query surfaces a clean `OAuthError: invalid_grant:
  Account disabled`, i.e. access is revoked on the next use;
- re-enabling her restores access on the following query.

It mutates Keycloak state but always restores it (and resets defensively at
start), so it is safe to re-run and to run alongside the other scripts.

## Related

- [`../keycloak/`](../keycloak/) — same stack, sync client, static
  `access_token=`, auto-provisioned users.
- [`../azure/`](../azure/) — sync `token_provider` against Azure Entra
  (device-code + refresh token, cached on disk).
