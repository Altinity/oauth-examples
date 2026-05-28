# ClickHouse + Keycloak (JWT) + local LDAP — combined demo

One ClickHouse user, `alice@example.com`, that can authenticate **either**:

- via **LDAP** (password bind against a local OpenLDAP), or
- via **Keycloak JWT** (token fetched non-interactively from Keycloak).

Both methods resolve to the same identity and the same default role
(`ldap_or_jwt_role`). All services run locally in compose — no external IdP.

## Run

```bash
docker compose up -d
```

## 1. Authenticate with LDAP

```bash
docker compose exec clickhouse clickhouse-client \
  --user alice@example.com --password ldappassword \
  --query "SELECT currentUser(), currentRoles()"
```

## 2. Authenticate with a Keycloak JWT

```bash
docker compose exec clickhouse clickhouse-client \
  --jwt-command /usr/local/bin/keycloak-jwt.sh \
  --query "SELECT currentUser(), currentRoles()"
```

Both should print `alice@example.com   ['ldap_or_jwt_role']`. Drop `--query`
to get an interactive shell.

## How it works

- The CH user is pre-created as `IDENTIFIED WITH ldap SERVER 'local_ldap', jwt`,
  so the server accepts either credential — there is **no `--ldap` flag**.
- `--jwt-command` (Antalya PR
  [#1809](https://github.com/Altinity/ClickHouse/pull/1809)) runs the named
  script and reads a bearer token from its stdout.
- [`keycloak-jwt.sh`](keycloak-jwt.sh) uses the OAuth2 **Resource Owner
  Password Credentials** grant: it POSTs `alice@example.com` / `demo` to
  Keycloak's `/token` endpoint and prints the `id_token`. ROPC is deprecated
  for real apps (no MFA, no consent) — fine for a local demo, zero browser
  interaction. For a real device-code flow against Azure Entra, see
  [`../../jwt_command/azure/`](../../jwt_command/azure/).

## Endpoints

| Service     | URL                                              |
|-------------|--------------------------------------------------|
| ClickHouse  | `localhost:8123` (HTTP), `localhost:9000` (TCP)  |
| Keycloak    | `http://localhost:8080` (admin: `admin`/`admin`) |
| OpenLDAP    | `localhost:1389` (admin: `admin`/`adminpassword`)|
