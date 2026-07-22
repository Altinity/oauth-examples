# `clickhouse-connect` with Keycloak JWTs

A Python script fetches a JWT from a local Keycloak and forwards it to
ClickHouse via [`clickhouse-connect`](https://github.com/ClickHouse/clickhouse-connect)'s
`access_token=` kwarg (added in 0.8.12). The server validates the
signature against the realm's JWKS and auto-provisions the CH user from
the JWT's `email` claim.

Fully self-contained — no external IdP.

## Run

```bash
cp .env.example .env
docker compose up -d

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

set -a; source .env; set +a
python app.py
```

Expected:

```
user:  alice@example.com
roles: ['jwt_role']
```

## Knobs

- [`app.py`](app.py) — passes the JWT as `access_token=`, which
  clickhouse-connect sends as `Authorization: Bearer <jwt>`.
- [`clickhouse-config/jwt_processors.xml`](clickhouse-config/jwt_processors.xml)
  — `jwt_dynamic_jwks` processor + `<user_directories><token>` for
  auto-provisioning.
- [`keycloak-config/realm.json`](keycloak-config/realm.json) — pre-loaded
  realm with `alice@example.com` / `demo`.

For `clickhouse-client --jwt-command` against the same Keycloak (plus an
LDAP fallback on the same user), see [`../../with_ldap/keycloak/`](../../with_ldap/keycloak/).
For the async client with a refreshable `token_provider` and pre-defined
users, see [`../keycloak_async/`](../keycloak_async/).
