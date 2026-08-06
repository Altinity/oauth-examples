# `clickhouse-client --jwt-command` + Azure Entra

`--jwt-command` (Antalya PR
[#1809](https://github.com/Altinity/ClickHouse/pull/1809)) runs a script and
reads a bearer token from its stdout — "invoked on the first connect, before
reconnects when the cached JWT is rejected". These two scripts sign in to Azure
Entra and emit an **access token**, which ClickHouse validates with the `entra`
processor.

## Scenarios

Named after the [MSAL Python
samples](https://github.com/AzureAD/microsoft-authentication-library-for-python/tree/dev/sample),
though the scripts hand-roll the flows in bash.

| Scenario | Script | Azure client | ClickHouse identity |
| --- | --- | --- | --- |
| **Public client, interactive** (`interactive_sample.py`) | `azure-interactive.sh` | no secret; auth code + PKCE, browser redirect to a one-shot `nc` listener | pre-defined `CH_JWT_USER`, from `upn` |
| **Confidential client, headless** (`confidential_client_sample.py`) | `azure-client-credentials.sh` | client secret; client-credentials, no user | pre-defined `CH_JWT_APP_USER`, from `oid` |

The MSAL *web app* scenario has no counterpart here: `--jwt-command` feeds a
single CLI process, so there is no session to keep per user. See
[`../../clickhouse_connect/azure/`](../../clickhouse_connect/azure/) for that one.

## Azure app registration

One registration serves both scripts.

- **Expose an API** (Application ID URI + a scope) — so it can issue a token for itself.
- **`requestedAccessTokenVersion: 2`** in the manifest — else v1.0 tokens are rejected.
- **`upn`** present in the access token (`azure-interactive.sh`).
- The **service principal's object id** — `CH_JWT_APP_USER`, the app-only token's
  `oid`. It is not the client id; read it with
  `az ad sp show --id "$AZURE_CLIENT_ID" --query id -o tsv`.
- *Authentication → Allow public client flows* = **Yes** — `azure-interactive.sh`
  as a public client.
- *Certificates & secrets* → a **client secret** — `azure-client-credentials.sh`.

Full walkthrough: [`../../grafana/azure/Entra_setup.md`](../../grafana/azure/Entra_setup.md).

### Redirect URI

Only `azure-interactive.sh` needs one; client-credentials never redirects.

| Setting | Value |
| --- | --- |
| Redirect URI | `http://localhost:8400/` |
| Register under | **Mobile and desktop applications** |
| Overridden by | `AZURE_REDIRECT_URI` (the listener port is parsed from it) |

The platform decides whether Azure requires client authentication on the code
exchange, and **both directions fail loudly**:

| Platform | `AZURE_INTERACTIVE_USE_SECRET` | Result |
| --- | --- | --- |
| Mobile and desktop applications | `0` (default) | works — PKCE only |
| Mobile and desktop applications | `1` | 401 `AADSTS700025` — client is public |
| Web | `0` | 401 `AADSTS7000218` — needs `client_secret` |
| Web | `1` | works — confidential variant |

The secret is opt-in precisely because this container also holds
`AZURE_CLIENT_SECRET` for the client-credentials script; inheriting it would
break the public flow. Note that wget discards 401 bodies (it reads them as an
HTTP auth challenge), so Azure's JSON never surfaces — the script prints wget's
stderr and names the likely AADSTS code instead.

### Endpoints that must be reachable

The container itself talks to Azure, so egress from the container matters, not
from the host:

| Endpoint | Used by |
| --- | --- |
| `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize` | browser, interactive sign-in |
| `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token` | both scripts, from inside the container |
| `https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys` | the **server**, to fetch JWKS and verify signatures |

Locally published ports: `8123`/`9000` for ClickHouse, and `8400` for the
sign-in redirect — the script listens inside the container while the browser runs
on the host, so that one must stay published and match the redirect URI.

## Testing

```bash
# 1. headless (row 5) — no browser, fully scriptable
docker compose exec clickhouse clickhouse-client \
  --jwt-command /usr/local/bin/azure-client-credentials.sh \
  --jwt-command-timeout 120 \
  --query "SELECT currentUser()"
# expect: $CH_JWT_APP_USER

# 2. interactive (row 4) — open the printed URL on the host and sign in
docker compose exec clickhouse clickhouse-client \
  --jwt-command /usr/local/bin/azure-interactive.sh \
  --jwt-command-timeout 600 \
  --query "SELECT currentUser()"
# expect: $CH_JWT_USER
```

`currentUser()` is itself the processor-precedence check: with no auto-provisioning,
a token that resolves to the wrong claim names a user that does not exist, so a
mis-ordered processor fails as

```
Code: 516 ... <some-guid>: Authentication failed: password is incorrect,
or there is no user with such name. (AUTHENTICATION_FAILED)
```

Both users are pre-defined, so both show `local_directory` and nothing is ever
created at runtime:

```bash
docker compose exec clickhouse clickhouse client --query "
  SELECT name, storage, auth_type FROM system.users ORDER BY name"
```

### Testing without spending a sign-in

The interactive flow can be exercised end to end without touching a browser, by
playing the part of Azure's redirect. Start the script, then deliver a fake code
to its listener:

```bash
docker exec clickhouse-client-azure-clickhouse-1 bash -c \
  'AZURE_SIGNIN_TIMEOUT=60 /usr/local/bin/azure-interactive.sh >/tmp/o 2>/tmp/e' &
sleep 5
STATE=$(docker exec clickhouse-client-azure-clickhouse-1 \
  sh -c "sed -n 's/.*[?&]state=\([^&]*\).*/\1/p' /tmp/e | head -1")
curl -s -o /dev/null "http://localhost:8400/?code=fake&state=$STATE"
docker exec clickhouse-client-azure-clickhouse-1 sh -c 'tail -3 /tmp/e'
```

This proves the listener binds and is reachable from the host, the request is
parsed, and the token endpoint is reached. The expected outcome is a **400** with
`AADSTS9002313` (Azure rejecting the fake code) — a **401** instead means the
client-auth mode is wrong, per the platform table above. Substituting a wrong
`state` must yield `state mismatch on the redirect` with nothing on stdout.

Nothing is persisted between runs, so this is the only way to exercise the flow
repeatedly without signing in every time.

**After editing any mounted script or config**, run `docker compose restart` —
a per-file bind mount follows the inode, and editors replace it, so the running
container otherwise reports `No such file or directory`.

## Run

```bash
cp .env.example .env      # set AZURE_TENANT_ID, AZURE_CLIENT_ID, CH_JWT_USER (= your upn)
                          # plus AZURE_CLIENT_SECRET and CH_JWT_APP_USER
                          # (= the service principal oid) for the headless flow
set -a; source .env; set +a
docker compose up -d
```

Interactive (row 4) — prints a URL, waits for the redirect:

```bash
docker compose exec clickhouse clickhouse-client \
  --jwt-command /usr/local/bin/azure-interactive.sh \
  --jwt-command-timeout 600 \
  --query "SELECT currentUser()"
```

Open the printed URL in a browser on the host and sign in. Expected:
`your-upn@example.com`.

Headless (row 5) — no browser, no user:

```bash
docker compose exec clickhouse clickhouse-client \
  --jwt-command /usr/local/bin/azure-client-credentials.sh \
  --jwt-command-timeout 120 \
  --query "SELECT currentUser()"
```

Expected: `$CH_JWT_APP_USER`, the service principal's object id.

## How it works

- **Pure bash + wget + openssl + nc.** The stock ClickHouse image has no `curl`,
  `jq` or `python3`, so JSON is picked apart with `sed`, PKCE uses `openssl`, and
  the redirect is captured by BusyBox `nc`.
- **Prompts go to `/dev/tty`, tokens to stdout.** `clickhouse-client` forwards
  the child's stderr through a buffered writer that only flushes once the script
  exits, which would hide the sign-in URL until it was too late to use.
- **The listener runs inside the container**, while the browser runs on the host,
  so `docker-compose.yml` publishes the redirect port. The port is derived from
  `AZURE_REDIRECT_URI`, so the two cannot drift apart.
- **No token user directory.** Both identities are pre-defined by
  `init-clickhouse.sh` — `CH_JWT_USER` (the `upn`) and `CH_JWT_APP_USER` (the
  service principal's `oid`) — so `jwt_processors.xml` needs only
  `enable_token_auth` plus the processors. `enable_token_auth` is what accepts
  the token; the `<user_directories><token>` section only ever *provisioned*
  users the local storage lacked, and there is nothing left for it to provision.
  Dropping it also sidesteps its one-per-server limit (a second `<token>` section
  fails startup with `Code: 318 ... Only one 'token' section can be defined`),
  which is why both processors can now name pre-defined users.
- **Two processors, and their names decide precedence.** A delegated token
  carries both `upn` and `oid`, so the first processor whose claim is present
  names the ClickHouse user — following the processor **name's sort order**, not
  the order in the file. `azure` sorts before `azure_app`, so delegated tokens
  land on the `upn` user; app-only tokens have no `upn` and reach `azure_app`.
- **There is no fall-through.** If the winning processor names a user that does
  not exist, authentication fails outright — the server does not try the next
  processor. Without auto-provisioning that turns a precedence mistake into a
  loud `AUTHENTICATION_FAILED` naming the user it looked for, rather than a
  silently wrong identity.

- **No token is persisted.** Every invocation mints a fresh one: another
  client-credentials grant for the headless script, another browser sign-in for
  the interactive one. Since `clickhouse-client` re-runs `--jwt-command` on
  reconnect, a dropped connection means signing in again. The interactive script
  therefore asks for no `offline_access`: with nothing stored between
  invocations, a refresh token could never be used.

`init-clickhouse.sh` runs only on first init, so `docker compose down -v` is how
you re-provision after changing `CH_JWT_USER` or `CH_JWT_APP_USER`.

Every `azure/` directory in this repo would otherwise default to the Compose
project name `azure` and share containers and volumes, so this one sets `name:`
explicitly. The published ports still collide, so run one stack at a time.

## Related

- [`../../jwt_command/azure/`](../../jwt_command/azure/) — device-code flow with
  an `id_token`, auto-provisioning from `email`, plus a bad-JWT test that proves
  the `entra` processor really verifies signatures.
- [`../../clickhouse_connect/azure/`](../../clickhouse_connect/azure/) — the same
  scenarios through the Python driver's `token_provider`, plus the web-app one.
