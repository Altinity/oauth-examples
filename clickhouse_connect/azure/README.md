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

## Why an access token, not an id_token?

ClickHouse is the *resource*. An `openid email profile` token is Graph-audience
with a `nonce` header that breaks JWKS validation; `<client_id>/.default` (+ the
v2.0 manifest) yields a token whose `aud` is this app and that `entra` can verify.

## Related

- [`../keycloak/`](../keycloak/) — same driver, static `access_token=`.
- [`../keycloak_async/`](../keycloak_async/) — async client, async
  refreshable `token_provider`, parallel/expiry test scenarios.
- [`../../jwt_command/azure/`](../../jwt_command/azure/) — `--jwt-command` + auto-provisioning.
