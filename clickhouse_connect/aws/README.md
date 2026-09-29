# `clickhouse-connect` + Azure Entra → AWS Glue, per-user credentials

A Python script signs the user in to Azure Entra and gives
[`clickhouse-connect`](https://github.com/ClickHouse/clickhouse-connect) a
`token_provider` that returns the user's access token. ClickHouse verifies the
token, then forwards it to AWS STS `AssumeRoleWithWebIdentity`
([Altinity/ClickHouse#2329](https://github.com/Altinity/ClickHouse/pull/2329)).
The temporary credentials STS returns sign the Glue and S3 calls. ClickHouse
holds no AWS credentials of its own.

```
glue.py ──browser sign-in──► Entra ──access token──► glue.py
   │
   └─ Authorization: Bearer ──► ClickHouse (entra processor verifies aud/iss/signature)
                                    │
                                    └─ AssumeRoleWithWebIdentity(token, AWS_ROLE_ARN,
                                       RoleSessionName = ClickHouse user) ──► STS
                                                                               │
                         Glue GetDatabases/GetTables ◄── temporary credentials ┘
```

The script only talks to Entra and ClickHouse. It never calls AWS, and it holds
no AWS credentials.

## Prerequisites

This demo assumes the Entra and AWS side is already set up:

- **Entra**: an API registration exposing a delegated scope (`access_as_user`)
  and issuing v2.0 tokens, plus a public test client that has been granted that
  scope. The client needs the redirect URI `http://localhost` under
  *Authentication → Mobile and desktop applications*. MSAL listens on a random
  localhost port, and Entra ignores the port for `localhost`.
- **AWS**: an IAM OIDC provider for `https://login.microsoftonline.com/<tenant>/v2.0`
  whose client-id list holds the token's `aud`, and a role whose trust policy
  matches that `aud` and your `sub`. The role also needs `glue:GetDatabase(s)`
  and `glue:GetTable(s)` permissions, and the Glue database (`az-tokens` in
  `eu-central-1`) must exist.

Check the AWS side without ClickHouse first. If this call fails, the demo will
fail the same way:

```bash
aws sts assume-role-with-web-identity --region eu-central-1 \
  --role-arn "$AWS_ROLE_ARN" --role-session-name manual-test \
  --web-identity-token "$TOKEN"
```

## Run

```bash
cp .env.example .env      # tenant, client id, scope, aud, role ARN
docker compose up -d

uv venv --python 3.14 .venv && source .venv/bin/activate
uv pip install -r requirements.txt
set -a; source .env; set +a

python glue.py            # opens a browser for sign-in
```

With no tables in `az-tokens` yet, the output is:

```
currentUser(): you@example.com
SHOW TABLES FROM glue:
  (no tables)
```

An empty result still shows that the whole chain worked. `SHOW TABLES` on a
`DataLakeCatalog` database raises STS and Glue errors instead of hiding them, so
`(no tables)` means STS accepted the token and Glue answered as the assumed
role. Once tables exist they show up here as `az-tokens.<table>`.

`init-clickhouse.sh` creates the catalog database only on the first start. After
changing `AWS_ROLE_ARN`, `GLUE_REGION` or `GLUE_DATABASE` in `.env`, recreate the
volume with `docker compose down -v && docker compose up -d`.

## ClickHouse side

`clickhouse-config/token_forwarding.xml`:

- `enable_token_forwarding` keeps the verified token in the session. It is off
  by default.
- The `entra` processor checks the token's signature against the tenant's JWKS,
  the issuer (`https://login.microsoftonline.com/<tenant>/v2.0`), and
  `expected_audience = AZURE_API_AUDIENCE`.
- `username_claim = preferred_username` names the user. It is also the STS
  `RoleSessionName`, so CloudTrail's `AssumeRoleWithWebIdentity` events show who
  ran the query.
- The `token` user directory auto-provisions every verified user with the
  `glue_users` role.

`init-clickhouse.sh` runs as `default` and creates:

```sql
CREATE DATABASE glue
ENGINE = DataLakeCatalog
SETTINGS
    catalog_type = 'glue',
    region = 'eu-central-1',
    warehouse = 'az-tokens',      -- required, unused by Glue
    namespaces = 'az-tokens',     -- only this Glue database
    aws_role_arn = '<AWS_ROLE_ARN>',
    oauth_forward_user_token = 1;

GRANT SHOW, SELECT ON glue.* TO glue_users;
```

- There is no URL and no `aws_sts_endpoint`. Glue resolves from `region`, and
  STS is `https://sts.<region>.amazonaws.com`.
- Nothing falls back to a service identity. A query with no bearer token fails
  with `CATALOG_USER_TOKEN_NOT_AVAILABLE`, even when `default` runs it.
- Whoever can run `CREATE DATABASE` chooses where users' tokens are sent. Only
  `default` has that right here.

## Troubleshooting

| Error | Where | Meaning |
| --- | --- | --- |
| `AADSTS50011` redirect URI mismatch | browser | `http://localhost` is not registered under *Mobile and desktop applications* |
| `AUTHENTICATION_FAILED` / `Token could not be verified` | ClickHouse | `aud` ≠ `AZURE_API_AUDIENCE`, a v1.0 token, or the wrong tenant |
| `Could not assume role … InvalidIdentityToken` | STS | AWS OIDC provider URL or client-id list does not match the token's `iss`/`aud` |
| `Could not assume role … AccessDenied` | STS | the trust policy's `aud`/`sub` conditions do not match this user |
| `Exception calling GetTables … AccessDenied` | Glue | the role lacks `glue:GetDatabases` / `glue:GetTables` |
| `CATALOG_USER_TOKEN_NOT_AVAILABLE` | ClickHouse | the query came without a bearer token, e.g. `clickhouse client` as `default` |

`docker compose logs clickhouse` shows the full server-side error. To look inside
a token, decode it locally. Don't paste it into web decoders.

## Related

- [`../azure/`](../azure/) — the same Entra sign-in patterns without Glue:
  hand-rolled auth code + PKCE, device code, client credentials, pooling.
