# `clickhouse-client --jwt-command` + Azure Entra device-code flow

A focused demo of the Antalya `--jwt-command` client feature: a pre-defined
ClickHouse user authenticates with an Azure Entra–issued JWT obtained via the
OAuth2 device-authorization grant. No LDAP fallback — JWT is the only allowed
auth method for this user.

## Prereqs

- An Azure app registration in your tenant. Two configurations work:
  - **Public client** — set *Authentication → Advanced settings → Allow public
    client flows* = **Yes**. Simpler, no secret to manage. Leave
    `AZURE_CLIENT_SECRET` unset in `.env`.
  - **Confidential client** — leave the public-client toggle OFF, create a
    secret in *Certificates & secrets*, set `AZURE_CLIENT_SECRET` in `.env`.
- A user account in the same tenant whose `email` claim equals `CH_USERNAME`.

## Run

```bash
cp .env.example .env
# edit .env: set AZURE_TENANT_ID, AZURE_CLIENT_ID (and optionally
# AZURE_CLIENT_SECRET), CH_USERNAME
docker compose up -d
```

ClickHouse: `http://localhost:8123` (HTTP), `9000` (native).

## Authenticate from inside the container

```bash
docker exec -it azure-clickhouse-1 clickhouse-client \
  --jwt-command /usr/local/bin/azure-device-flow.sh \
  --jwt-command-timeout 600
```

`azure-device-flow.sh` runs the OAuth2 device-code dance: it writes
`To sign in, visit https://microsoft.com/devicelogin and enter code XXXX-XXXX`
to the controlling terminal (`/dev/tty`, so it shows up even though
`clickhouse-client` buffers the child's stderr), then polls Azure until you
complete sign-in in a browser. The resulting `id_token` is written to stdout
and consumed by `clickhouse-client`. A `--jwt-command-timeout` of 600 s gives
you 10 minutes to finish.

The script caches the token in `~/.cache/ch-azure-jwt.json` (inside the
container, since `docker exec` is the entrypoint), so reconnects inside the
token's lifetime skip the device dance.

## Authenticate from the host

`--jwt-command` ships only in the Antalya PR build pinned in
`docker-compose.yml`; there is no matching macOS/Linux binary outside that
image. Easiest path is to run `clickhouse-client` via the same image, joined
to this compose's network:

```bash
set -a; source .env; set +a

docker run --rm -it \
  --network azure_default \
  -e AZURE_TENANT_ID -e AZURE_CLIENT_ID -e AZURE_CLIENT_SECRET \
  -v "$PWD/azure-device-flow.sh:/jwt.sh:ro" \
  altinityinfra/clickhouse-server:1809-26.3.10.20001.altinityantalya \
  clickhouse-client --host clickhouse \
    --jwt-command /jwt.sh --jwt-command-timeout 600
```

`-e VAR` (no `=value`) passes the host shell's value through, so the same
`.env` drives both the server and the client.

## Verify that bad JWTs are rejected

`azure-bad-jwt.sh` emits a JWT whose `email` claim equals `$CH_USERNAME` but
whose signature is garbage. ClickHouse should reject it:

```bash
docker exec -it -e CH_USERNAME azure-clickhouse-1 clickhouse-client \
  --jwt-command /usr/local/bin/azure-bad-jwt.sh
# DB::Exception: Token is invalid. (AUTHENTICATION_FAILED)
```

This confirms the `entra` JWT processor is doing real signature verification
(fetching Azure's JWKS, checking the signature against `kid`), not just
trusting the `email` claim at face value.

## Notes

- The user is created via SQL in `startup_scripts.xml` (not `users.xml` — XML
  element names can't contain `@` or `.`). The `CREATE USER` statement is
  built in `docker-compose.yml` from `$CH_USERNAME` and injected via `<query
  from_env="CH_CREATE_USER_QUERY">`.
- `--jwt-command` requires Antalya's PR
  [Altinity/ClickHouse#1809](https://github.com/Altinity/ClickHouse/pull/1809).
  Until a release tag ships, the compose pins the PR build
  `1809-26.3.10.20001.altinityantalya`.
- For a combined LDAP + IdP demo (the same user can authenticate either way),
  see `../../with_ldap/keycloak/`.
