# `clickhouse-connect` + Azure Entra, refreshable access token

A Python script signs in to Azure (device-code) once, then gives
[`clickhouse-connect`](https://github.com/ClickHouse/clickhouse-connect) a
**`token_provider`** callable (driver 1.2.0+,
[PR #775](https://github.com/ClickHouse/clickhouse-connect/pull/775)) that
forwards an **access token** and silently renews it via the refresh token — the
driver re-invokes the callable whenever ClickHouse rejects the current token.
ClickHouse validates the token with the `entra` processor and authenticates a
pre-defined `IDENTIFIED WITH jwt` user, identity taken from the `upn` claim.

## Azure app registration

- **Public client** (*Allow public client flows* = Yes) — device-code, no secret.
- **Expose an API** (Application ID URI + a scope) — so it can issue a token for itself.
- **`requestedAccessTokenVersion: 2`** in the manifest — else v1.0 tokens are rejected.
- **`upn`** present in the access token.

Full walkthrough: [`../../grafana/azure/Entra_setup.md`](../../grafana/azure/Entra_setup.md).

## Run

```bash
cp .env.example .env      # set AZURE_TENANT_ID, AZURE_CLIENT_ID, CH_JWT_USER (= your upn)
docker compose up -d

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
set -a; source .env; set +a
python app.py             # first run prints a device-code prompt; sign in once
```

Expected: `currentUser(): you@example.com`. Tokens cache at
`~/.cache/ch-azure-connect-jwt.json` (0600); later runs skip the browser. The CH
user is created on first init only, so after changing `CH_JWT_USER` re-provision
with `docker compose down -v && docker compose up -d`.

## Sign-in options

All three feed the same `token_provider` an Entra access token; pick the flow that
fits how the app runs:

- `app.py` — **device-code** (default above). No browser on the host; sign in on
  any device from the printed code. Best for headless/remote shells.
- `interactive.py` — **authorization-code + PKCE**, hand-rolled with `requests`
  (no MSAL). Opens a browser, captures the redirect on a one-shot localhost
  server, and the `token_provider` manages the access/refresh tokens itself.
  This is the pure equivalent of
  [MSAL's `interactive_sample.py`](https://github.com/AzureAD/microsoft-authentication-library-for-python/blob/dev/sample/interactive_sample.py).
  Supports a confidential app via `AZURE_CLIENT_SECRET`.
- `confidential_client.py` — **client-credentials** (app identity, no user, no
  browser). Uses the `entra` processor with a token `user_directory` that
  auto-provisions a user from the SP object id. For daemons/services.

## Why an access token, not an id_token?

ClickHouse is the *resource*. An `openid email profile` token is Graph-audience
with a `nonce` header that breaks JWKS validation; `<client_id>/.default` (+ the
v2.0 manifest) yields a token whose `aud` is this app and that `entra` can verify.

## Related

- [`../keycloak/`](../keycloak/) — same driver, static `access_token=`.
- [`../keycloak_async/`](../keycloak_async/) — async client, async
  refreshable `token_provider`, parallel/expiry test scenarios.
- [`../../jwt_command/azure/`](../../jwt_command/azure/) — `--jwt-command` + auto-provisioning.
