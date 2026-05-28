# `clickhouse-client --jwt-command` + Azure Entra device-code flow

Demo of the Antalya `--jwt-command` client feature against Azure Entra.
Whoever signs in gets a ClickHouse user auto-provisioned from the JWT's
`email` claim, with `azure_jwt_role` (SELECT on `default.*`).

## Prereqs

- An Azure app registration in your tenant, either:
  - **Public client** — set *Authentication → Allow public client flows = **Yes***. Leave `AZURE_CLIENT_SECRET` empty.
  - **Confidential client** — keep the public-client toggle OFF, create a secret in *Certificates & secrets*, set `AZURE_CLIENT_SECRET`.

## Run

```bash
cp .env.example .env
# edit .env: AZURE_TENANT_ID, AZURE_CLIENT_ID, optionally AZURE_CLIENT_SECRET
docker compose up -d
```

## Authenticate

```bash
docker compose exec clickhouse clickhouse-client \
  --jwt-command /usr/local/bin/azure-device-flow.sh \
  --jwt-command-timeout 600 \
  --query "SELECT currentUser(), currentRoles()"
```

The script prints something like:

```
To sign in, visit https://microsoft.com/devicelogin and enter code XXXX-XXXX
```

Open the URL in a browser, paste the code, sign in. The script polls Azure,
picks up the `id_token`, and pipes it to `clickhouse-client`. Expected output:

```
<your-email@example.com>   ['azure_jwt_role']
```

Token is cached at `~/.cache/ch-azure-jwt.json` inside the container, so
reconnects within its lifetime skip the device-code round-trip.

## Verify bad JWTs are rejected

```bash
docker compose exec clickhouse clickhouse-client \
  --jwt-command /usr/local/bin/azure-bad-jwt.sh
# DB::Exception: Token is invalid. (AUTHENTICATION_FAILED)
```

— proves the `entra` processor is doing real signature verification (fetching
Azure's JWKS for the configured tenant, checking signature against `kid`),
not just trusting the `email` claim.

## How it works

- `--jwt-command` (Antalya PR
  [#1809](https://github.com/Altinity/ClickHouse/pull/1809)) runs the named
  script and reads a bearer token from its stdout.
- `<token_processors><azure><type>entra</type>…</azure></token_processors>`
  validates signatures via Azure's tenant-scoped JWKS — only tokens issued
  by `$AZURE_TENANT_ID` pass.
- `<user_directories><token>…</token></user_directories>` auto-creates a CH
  user from the JWT's `email` claim on first login and grants them every
  role in `<common_roles>` (here: `azure_jwt_role`). No pre-created user.

For a combined LDAP + IdP demo (same user, either method),
see [`../../with_ldap/keycloak/`](../../with_ldap/keycloak/). For a Python
flow that forwards a JWT through the [`clickhouse-connect`](https://github.com/ClickHouse/clickhouse-connect)
driver's `access_token=` kwarg, see [`../../clickhouse_connect/keycloak/`](../../clickhouse_connect/keycloak/).
