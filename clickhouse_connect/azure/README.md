# `clickhouse-connect` + Azure Entra, refreshable access token

Python scripts sign in to Azure Entra, then give
[`clickhouse-connect`](https://github.com/ClickHouse/clickhouse-connect) a
**`token_provider`** callable (driver 1.2.0+,
[PR #775](https://github.com/ClickHouse/clickhouse-connect/pull/775)) that
forwards an **access token** and silently renews it — the driver re-invokes the
callable whenever ClickHouse rejects the current token. ClickHouse validates the
token with the `entra` processor and maps it to a ClickHouse user.

## Scenarios

Named after the [MSAL Python
samples](https://github.com/AzureAD/microsoft-authentication-library-for-python/tree/dev/sample),
though each script hand-rolls the flow with `requests` instead of using MSAL.

| Scenario | Script | Azure client | ClickHouse identity |
| --- | --- | --- | --- |
| **Public client, interactive** (`interactive_sample.py`) | `interactive.py` | no secret; auth code + PKCE, browser redirect to a one-shot localhost server | pre-defined `CH_JWT_USER`, from `upn` |
| **Confidential client, headless** (`confidential_client_sample.py`) | `confidential_client.py` | client secret; client-credentials, no user | auto-provisioned, from the service principal's `oid` |
| **Confidential client, web app** (`authorization-code-flow-sample`) | `web_app.py` | client secret; auth code + PKCE, delegated user access, per-user sessions | pre-defined `CH_JWT_USER`, from `upn` |
| Device code (no MSAL counterpart here) | `app.py` | no secret; sign in from any device | pre-defined `CH_JWT_USER`, from `upn` |
| Connection pool (no MSAL counterpart here) | `pooled.py` | no sign-in of its own — refreshes `interactive.py`'s cached token | pre-defined `CH_JWT_USER`, from `upn` |

`interactive.py` also covers the confidential *desktop* variant: set
`AZURE_INTERACTIVE_CLIENT_SECRET` and it authenticates the client on the code
and refresh exchanges too, which MSAL's public-client `interactive_sample.py`
cannot do against a confidential app. It is deliberately separate from
`AZURE_CLIENT_SECRET`, because one `.env` serves every script here and a public
client must not send a secret — see [Redirect URIs](#redirect-uris).

## Azure app registration

One registration serves every script here.

- **Expose an API** (Application ID URI + a scope) — so it can issue a token for itself.
- **`requestedAccessTokenVersion: 2`** in the manifest — else v1.0 tokens are rejected.
- **`upn`** present in the access token (the three delegated flows).
- *Authentication → Allow public client flows* = **Yes** — device code and
  secretless `interactive.py`.
- *Certificates & secrets* → a **client secret** — `confidential_client.py`, `web_app.py`.
- Redirect URIs — see below.

Full walkthrough: [`../../grafana/azure/Entra_setup.md`](../../grafana/azure/Entra_setup.md).

### Redirect URIs

Azure never returns the authorization code to the program directly: it redirects
the *browser* to a URL registered in advance, with `?code=...&state=...`. Only
the two browser flows need one.

| Script | Redirect URI | Register under | Override with |
| --- | --- | --- | --- |
| `interactive.py` | `http://localhost:8400/` | **Mobile and desktop applications** | `AZURE_REDIRECT_URI` |
| `web_app.py` | `http://localhost:8500/auth/callback` | **Web** | `AZURE_WEB_REDIRECT_URI` (+ `WEB_APP_PORT`) |
| `app.py` | none — device code shows a code instead of redirecting | | |
| `confidential_client.py` | none — no browser, no user | | |
| `pooled.py` | none — never signs in; refreshes `interactive.py`'s cached token | | |

*Authentication → Add a platform*, then add the URI. Two things to get right:

- **The platform decides whether client authentication is required.** A URI
  under *Web* makes Azure demand a `client_secret` on the code and refresh
  exchanges; missing it gives AADSTS7000218 (`must contain 'client_assertion' or
  'client_secret'`). Under *Mobile and desktop applications* PKCE alone is
  enough, and sending a secret anyway is the mirror-image error, AADSTS700025
  (`Client is public`). That is the whole difference between the public and
  confidential variants of the same code exchange: `web_app.py` always needs
  `AZURE_CLIENT_SECRET`, while `interactive.py` needs
  `AZURE_INTERACTIVE_CLIENT_SECRET` only when its URI is registered under *Web*.
- ***Allow public client flows* does not override the platform.** That toggle
  enables the flows with no redirect URI at all — device code, ROPC — which is
  why `app.py` needs no secret. A URI under *Web* still makes its own code
  exchange confidential, toggle or not.
- **A URI belongs to exactly one platform.** The default
  `http://localhost:8400/` sits under *Mobile and desktop applications*; the
  confidential desktop variant needs a second URI of its own under *Web*,
  selected with `AZURE_REDIRECT_URI`.
- **The match is exact**, including the trailing slash and the port. All these
  URIs can coexist on one registration, each under its own platform.
- *Implicit grant and hybrid flows* is **not** part of this: it returns tokens
  straight from the authorize endpoint, which none of these scripts use. Leave
  both checkboxes off.

`web_app.py` prints its redirect URI on startup and `interactive.py` prints the
authorize URL containing it, so a mismatch (AADSTS50011) can be compared against
the portal directly.

## Run

```bash
cp .env.example .env      # set AZURE_TENANT_ID, AZURE_CLIENT_ID, CH_JWT_USER (= your upn)
chmod 600 .env            # it will hold AZURE_CLIENT_SECRET
docker compose up -d

# clickhouse-connect >= 1.2 needs Python >= 3.10; macOS system python3 is 3.9
uv venv --python 3.14 .venv && source .venv/bin/activate
uv pip install -r requirements.txt
set -a; source .env; set +a
```

Then pick a scenario:

```bash
python app.py                  # device code: prints a code to enter in a browser
python interactive.py          # browser on first run, silent refresh after
python confidential_client.py  # needs AZURE_CLIENT_SECRET; no browser
python web_app.py              # needs AZURE_CLIENT_SECRET; then open http://localhost:8500/
python pooled.py               # pool sharing one token; run interactive.py first
```

The delegated scripts print `currentUser(): you@example.com`;
`confidential_client.py` prints the service principal's object id instead.
`web_app.py` also shows the token's remaining lifetime and renews on reload
once it has expired.

The pre-defined CH user is created on first init only, so after changing
`CH_JWT_USER` re-provision with `docker compose down -v && docker compose up -d`.

## Credentials on disk

Two files hold real credentials. Neither is encrypted — both rely on filesystem
permissions alone.

| File | Holds | Mode | Written by |
| --- | --- | --- | --- |
| `.env` | `AZURE_CLIENT_SECRET` | **yours to set** | you |
| `$XDG_CACHE_HOME/clickhouse-connect-azure/interactive.json` | the refresh token | `0600`, enforced on every write | `interactive.py`, `pooled.py` |

**`chmod 600 .env`.** It is gitignored (`*.env`), but `cp .env.example .env`
leaves it at your umask — usually `644`, world-readable. The client secret is
the app's own identity: it needs no user and does not rotate on use, so it is
the strongest credential here.

The token cache falls back to `~/.cache/...` when `XDG_CACHE_HOME` is unset.
`interactive.py` writes it after signing in, `pooled.py` rewrites it on every
renewal, and `app.py`, `confidential_client.py` and `web_app.py` persist
nothing. **Access tokens are never written to disk by any script** — only the
refresh token is.

That refresh token is a ~90-day sliding-window bearer credential sitting in
plaintext, so any process running as you can act as the signed-in user until it
expires, and it rides along in whatever backs up or syncs your cache directory.
Delete the file to force a fresh sign-in, or set `AZURE_TOKEN_CACHE=none` to
keep the token in memory only. Production tools (`az`, `gh`, MSAL) put it in the
OS keychain and fall back to plaintext only with a warning; these examples stay
with a plain file to keep the flow readable.

Not on disk, but the same class of problem: `web_app.py` takes Flask's cookie
defaults, so its session cookie is `HttpOnly` and `SameSite=Lax` but **not**
`Secure`. That is correct for `http://localhost` and wrong the moment it serves
real traffic — behind HTTPS it needs `SESSION_COOKIE_SECURE = True`, or the
cookie that stands in for a signed-in user travels in clear text.

## ClickHouse side

`clickhouse-config/jwt_processors.xml` configures two `entra` processors,
because the two token shapes carry different identity claims:

- `azure` — `username_claim=upn`, no user directory. Delegated tokens map to the
  pre-defined `IDENTIFIED WITH jwt` user from `init-clickhouse.sh`.
- `azure_app` — `username_claim=oid`, with a `<user_directories><token>` bound
  to it. App-only tokens have no `upn`, so the service principal is
  auto-provisioned on first login and granted `azure_jwt_role`.

A delegated token carries **both** `upn` and `oid`, so which processor resolves
it first decides the username — and precedence follows the processor **name's
sort order**, not the order in the file. `azure` sorts before `azure_app`, which
is what keeps delegated tokens on the pre-defined user; a name sorting after
`azure_app` would silently auto-provision an `oid`-named user instead.

## Connection pooling (`pooled.py`)

`pooled.py` runs on its own, but it never signs in. It refreshes the token
`interactive.py` cached, so run that once first:

```bash
python interactive.py   # browser sign-in; writes the refresh-token cache
python pooled.py        # builds a pool of clients that share one access token
```

With no cached token it stops with ``no cached refresh token — run `python
interactive.py` first`` instead of opening a browser. That is deliberate: the
provider runs under a lock on behalf of clients that may be mid-request, so
stalling every one of them for the length of a sign-in is worse than failing
loudly. `web_app.py` takes the same line and redirects to `/login`.

The two scripts share the OAuth plumbing by import, and the refresh token
through the cache file:

```
interactive.py        $XDG_CACHE_HOME/…/interactive.json        pooled.py
──────────────        ──────────────────────────────────        ─────────
browser sign-in ─write─► {"<tenant>/<client>":          ◄─read─── SharedToken
                           {"refresh_token": …}}        ──write─► (rotates it)
```

What they do *not* share is the `token_provider` itself — each builds its own.
`interactive.py`'s serves a single client and may open a browser.
`pooled.py`'s `SharedToken` serves every client in the pool, is lock-guarded,
checks expiry, and never opens anything.

### Why it is built this way

- **Pooling and bearer auth do not interact.** Every plain-HTTP client in a
  process shares one urllib3 `PoolManager`, but `Authorization` is a per-client
  header applied per request, so a pooled connection carries the token of
  whoever is using it. A JWT binds to no connection and no session.
- **One access token for the whole pool is the goal**, not one per client. N
  tokens means N token-endpoint calls, and with rotation each new token
  invalidates the previous refresh token.
- **A pool of clients is one of two options.** The driver rejects concurrent
  queries sharing a session id, so either give each thread its own client (a
  pool, and its own ClickHouse session), or pass `autogenerate_session_id=False`
  to a single shared client, which drops the session id and the guard with it —
  that is what `web_app.py` does. Pool when you want per-thread sessions.
- **The provider must be lock-guarded.** The single-client providers in the
  other scripts renew on every call, which is right when one client owns them —
  the driver only asks at init or after a rejection. Share one across clients
  without a lock and concurrent callers submit the *same* refresh token; Azure
  rotates on every use, so replaying a spent one can invalidate the token
  family and force a fresh browser sign-in.

`SharedToken`'s reuse window is what makes that cheap: a renewal that just
happened satisfies every caller queued behind it. Measured with 12 clients and
240 concurrent queries — one token-endpoint call to build the pool, none under
load, and exactly one renewal when all 12 are rejected at once.

## Why an access token, not an id_token?

ClickHouse is the *resource*. An `openid email profile` token is Graph-audience
with a `nonce` header that breaks JWKS validation; `<client_id>/.default` (+ the
v2.0 manifest) yields a token whose `aud` is this app and that `entra` can verify.

## Related

- [`../keycloak/`](../keycloak/) — same driver, static `access_token=`.
- [`../keycloak_async/`](../keycloak_async/) — async client, async
  refreshable `token_provider`, parallel/expiry test scenarios.
- [`../../jwt_command/azure/`](../../jwt_command/azure/) — `--jwt-command` + auto-provisioning.
