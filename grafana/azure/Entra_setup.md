# Wiring Microsoft Entra ID into ClickHouse: a Working Recipe

Microsoft Entra ID (formerly Azure AD) makes a perfectly good OIDC identity provider for ClickHouse — once you get the configuration right. The first attempt usually doesn't: the token validates fine against Microsoft Graph but ClickHouse can't verify it locally, no key in the JWKS matches, and the `groups` claim is mysteriously absent. The problem isn't the integration — it's that Entra has half a dozen knobs that all need to be in the right position, and the defaults are wrong for this use case.

This article walks through the specific path through those knobs. By the end you'll have ClickHouse authenticating users from Entra without ever calling Microsoft Graph, validating tokens locally against the published JWKS, and (optionally) mapping Entra security-group memberships straight to ClickHouse roles.

Throughout, replace `<TENANT_ID>` with your tenant GUID (find it under **Entra admin center → Overview**) and `<CLIENT_ID>` with the **Application (client) ID** of your app registration.

## What we're aiming for

A typical sign-in looks like this:

1. The user (or a service on their behalf) obtains an OAuth access token from Entra.
2. They hit ClickHouse with `Authorization: Bearer <token>`.
3. ClickHouse decodes the JWT header, fetches Entra's published JWKS, finds the matching key by `kid`, verifies the signature, checks `iss` / `aud` / `exp`.
4. ClickHouse reads the user's identity from `preferred_username`, and — if you've configured it — their Entra group memberships from the `groups` claim.
5. Those group GUIDs are translated to ClickHouse roles via a small mapping in `config.xml`.

No call to Microsoft Graph. No userinfo round trip. No introspection endpoint. Just standard OIDC.

The catch — and it's a non-trivial one — is that *by default* the access tokens Entra issues for client applications are bound to Microsoft Graph (audience `00000003-0000-0000-c000-000000000000`), signed with Graph's internal key, and carry a `nonce` header that breaks third-party JWKS validation. To escape that, the client has to ask for a token whose audience is **your own app**. That requires three things: exposing the app as a resource (so it *has* an audience), forcing v2.0 token format, and crucially, asking for the right OAuth scope.

## Step 1 — Find or register the app

In the Entra admin center: **Applications → App registrations**.

If a registration for ClickHouse already exists in your tenant, open it and skip to Step 2 — just copy the **Application (client) ID** from the overview page, that's `<CLIENT_ID>` from here on.

Otherwise, click **+ New registration**, give it a name (`ClickHouse` is fine), pick a single-tenant account type (multi-tenant introduces extra issuer-validation complexity we don't need here), leave the redirect URI blank for now, and click **Register**. Copy the **Application (client) ID** from the resulting overview.

## Step 2 — Expose the app as an API

This is the step that lets Entra issue tokens with *your app* as the audience. Without it, you'll only ever get Graph-bound tokens.

In the app registration, open **Expose an API**. If you already see an **Application ID URI** set and at least one scope defined (Microsoft's `user_impersonation` is the common default), you're done — write down the URI (e.g. `api://<CLIENT_ID>` or `api://clickhouse`), it'll be `expected_audience` in the ClickHouse config later. The scope name itself doesn't matter; any existing scope under that URI works.

If you don't have a URI or scope yet:

1. Next to **Application ID URI**, click **Add**. Entra suggests `api://<CLIENT_ID>` as the default; accept it or replace it with something friendlier like `api://clickhouse`. Save.
2. Click **+ Add a scope**. The scope name is essentially a label — `user_impersonation` is the Microsoft default and what we'll assume in examples below. Set **Who can consent** to *Admins and users*, leave the state *Enabled*, and click **Add scope**.

The full scope string (e.g. `api://clickhouse/user_impersonation`) is what clients pass in the `scope=` parameter when acquiring tokens. We'll come back to that.

## Step 3 — Switch to v2.0 access tokens

In the same app registration, open **Manifest**. Find the field `requestedAccessTokenVersion` (it's probably `null` or `1`) and set:

```json
"requestedAccessTokenVersion": 2,
```

Save.

This makes a real difference. v1.0 tokens have `iss = https://sts.windows.net/<TENANT_ID>/`, a `nonce` header, and a sprinkling of Microsoft-internal claims that prevent JWKS validation. v2.0 tokens have the canonical issuer `https://login.microsoftonline.com/<TENANT_ID>/v2.0`, no `nonce`, and signing keys that *are* in the tenant JWKS at `https://login.microsoftonline.com/<TENANT_ID>/discovery/v2.0/keys`.

---

The next three steps are all about getting useful claims into the access token. Each is independently optional:

- **Step 4** is needed if you want anything beyond the defaults (`sub` and `email`). Most setups will want at least `preferred_username` here.
- **Steps 5 and 6** are a pair: only do them if you want group-based authorization in ClickHouse. If you're happy assigning ClickHouse roles based on something else (per-user `common_roles`, App Roles, or just letting users in without role mapping), skip both.

## Step 4 — Make the claims you care about appear in the *access* token

By default, the access token Entra issues for a custom-API audience carries `sub`, `aud`, `iss`, `exp`, and not much else. To get `preferred_username` (a friendly username), `upn`, or `groups`, you have to opt them in via `optionalClaims`.

In **Manifest**, look for an existing `optionalClaims` object. If there's already one defined for this app, merge into its `accessToken` array; if not, add the whole thing:

```json
"optionalClaims": {
    "accessToken": [
        { "name": "preferred_username", "source": null, "essential": false, "additionalProperties": [] },
        { "name": "groups",             "source": null, "essential": false, "additionalProperties": [] }
    ],
    "idToken": [],
    "saml2Token": []
}
```

Save.

The two claims here serve different purposes. `preferred_username` is the user-friendly identifier you'll point ClickHouse's `username_claim` at (the alternative is the default `sub`, which is an opaque pairwise pseudonymous ID — fine for stable identity, awful for `SHOW USERS`). `groups` is what makes group-based role mapping possible — but on its own this entry doesn't do anything; the next step controls what actually lands in there.

If you don't need groups, drop the `groups` entry and skip the next two steps.

## Step 5 — Turn on group emission (optional)

Skip this step if you're not using group-based authorization.

Still in **Manifest**, you have two reasonable choices:

```json
// Simple: every security group the user is a member of comes through automatically.
// Use this if you're sure no user in your tenant belongs to more than 200 groups.
"groupMembershipClaims": "SecurityGroup",
```

```json
// Scalable: only groups explicitly assigned to this app are emitted.
// Use this in larger tenants to sidestep the 200-group overage cliff.
"groupMembershipClaims": "ApplicationGroup",
```

Save.

Why the two choices matter: if any user in your tenant is a member of more than 200 groups, Entra silently drops the `groups` claim entirely and replaces it with a Graph URL pointer (`_claim_names` / `_claim_sources`) — which our processor doesn't follow, so the user effectively loses all group memberships. `SecurityGroup` counts every group in the tenant toward that limit; `ApplicationGroup` counts only the ones you explicitly assign in Step 6, so it's effectively impossible to overflow.

For a small or focused tenant, `SecurityGroup` is simpler and lets you skip Step 6 entirely. For a large or unpredictable tenant, `ApplicationGroup` plus Step 6 is the safer bet. (`All`, which adds distribution lists and directory roles, also exists but rarely buys anything useful.)

## Step 6 — Assign the groups you actually want emitted (optional)

Skip this step if you skipped Step 5, or if you used `SecurityGroup` in Step 5 — those paths already get you every group the user is in.

If you chose `ApplicationGroup`, this step is what makes any group actually appear in tokens: without an assignment, the `groups` claim will be empty.

Open **Entra ID → Enterprise applications → *your app* → Users and groups**. Click **+ Add user/group**, pick the security groups you want ClickHouse to recognize (e.g. `ch-admins`, `ch-analysts`, `ch-readonly`), and **Assign**.

For each assigned group, open **Entra ID → Groups → *the group*** and copy its **Object ID** (a GUID). Those GUIDs will be in the `groups` claim of every token issued to a member of that group, and they're what you'll map to ClickHouse roles in a moment.

## Acquiring a token the right way

This is where most first attempts fail. The OAuth `scope=` parameter has to point at your app's API URI, not at a Microsoft Graph permission. Get this wrong and Entra hands you back a Graph-audience token that ClickHouse can never validate.

For the auth-code flow (web apps, server-side):

```
GET https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/authorize
  ?client_id=<CLIENT_ID>
  &response_type=code
  &redirect_uri=<your-redirect>
  &scope=api%3A%2F%2Fclickhouse%2Fuser_impersonation%20offline_access
  &state=<random>
```

Followed by a `POST` to `/oauth2/v2.0/token` with `grant_type=authorization_code` and the same scope.

For interactive CLI sessions, the device-code flow is friendlier:

```bash
curl -s -X POST \
    "https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/devicecode" \
    -d "client_id=<CLIENT_ID>" \
    -d "scope=api://clickhouse/user_impersonation offline_access"
```

For service-to-service auth (no user identity, no `groups`):

```bash
curl -s -X POST \
    "https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token" \
    -d "grant_type=client_credentials" \
    -d "client_id=<CLIENT_ID>" \
    -d "client_secret=<secret>" \
    -d "scope=api://clickhouse/.default"
```

The scope-shape rule, simply: scopes starting with `api://...` ✅ — bare names like `User.Read`, `email`, `profile`, `openid`, or anything ending in `.Read.All` ❌. Those resolve to Microsoft Graph and yield the wrong audience.

## Verify the token before plugging in ClickHouse

Paste the access token at [jwt.ms](https://jwt.ms) and check the rows that apply to your setup:

| Field | Expected |
|---|---|
| Header `kid` | one of the values at `https://login.microsoftonline.com/<TENANT_ID>/discovery/v2.0/keys` |
| Header `nonce` | **absent** — its presence means you got a Graph-format token |
| Header `typ` | `JWT` |
| Payload `iss` | `https://login.microsoftonline.com/<TENANT_ID>/v2.0` |
| Payload `aud` | `api://clickhouse` (or `api://<CLIENT_ID>`) |
| Payload `ver` | `"2.0"` |
| Payload `preferred_username` | populated (if you added it in Step 4) |
| Payload `groups` | array of GUIDs (if you did Steps 4–6) |
| Payload `exp` | a future Unix timestamp |

If anything's off, here's the troubleshooting shortlist:

| Symptom | Fix |
|---|---|
| `aud` = `00000003-0000-0000-c000-000000000000` | Scope used a Graph permission. Use `api://...` instead. |
| `iss` starts with `https://sts.windows.net/` | App still on v1.0 tokens — recheck Step 3. |
| Header has `nonce` | Same as above. |
| No `preferred_username` | Step 4. |
| No `groups` claim | One of Steps 4, 5, 6 is missing or skipped. |
| `groups` replaced by `_claim_names` | Overage (>200 groups) — Step 5 needs `ApplicationGroup`, not `SecurityGroup`. |

It's worth getting this right at jwt.ms before touching ClickHouse — almost all "ClickHouse won't accept my token" reports trace back to one of those rows.

## ClickHouse-side configuration

Add the `entra` token processor to `config.xml`:

```xml
<clickhouse>
    <token_processors>
        <entra_prod>
            <type>entra</type>
            <tenant_id><TENANT_ID></tenant_id>
            <expected_audience>api://clickhouse</expected_audience>
            <username_claim>preferred_username</username_claim>
            <groups_claim>groups</groups_claim>
        </entra_prod>
    </token_processors>
</clickhouse>
```

Only `tenant_id` is mandatory — the JWKS URL and the expected issuer are auto-derived from it. `username_claim` defaults to `sub` and `groups_claim` defaults to `groups`, so both are technically optional. `expected_audience` is optional too but recommended; without it ClickHouse will accept any signature-valid token from your tenant regardless of which app it was issued for, and it'll log a warning at startup to make that gap visible.

If you skipped Steps 4–6 (no groups), drop `groups_claim` and rely on ClickHouse's per-user `common_roles` or `default_profile` instead of group-based mapping. The processor itself still validates tokens just fine.

Then add a user directory that pulls users from Entra and maps their groups:

```xml
<clickhouse>
    <user_directories>
        <token>
            <processor>entra_prod</processor>
            <default_profile>default</default_profile>
            <roles_mapping>
                <map>
                    <from>aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee</from>  <!-- ch-admins -->
                    <to>ch_admin</to>
                </map>
                <map>
                    <from>11111111-2222-3333-4444-555555555555</from>  <!-- ch-analysts -->
                    <to>ch_analyst</to>
                </map>
            </roles_mapping>
            <roles_filter>^ch_[a-z_]+$</roles_filter>
        </token>
    </user_directories>
</clickhouse>
```

Each `<map>` takes the Entra group's Object ID and translates it to a ClickHouse role name. `<roles_filter>` is a safety net: any unmapped GUID falls through, doesn't match the regex (GUIDs never start with `ch_`), and is dropped — so accidentally-emitted groups don't accidentally become role names.

Make sure the target roles exist in ClickHouse:

```sql
CREATE ROLE ch_admin;
CREATE ROLE ch_analyst;
GRANT ALL ON *.* TO ch_admin;
GRANT SELECT ON *.* TO ch_analyst;
```

Now smoke-test:

```bash
TOKEN=<paste-access-token>
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://<clickhouse-host>:8123/?query=SELECT%20currentUser()%2C%20arraySort(currentRoles())"
```

You should see something like `alice@example.com  ['ch_admin']`.

## Day-2 operations

Adding a new group → role mapping is a five-step ritual: assign the group to the app in Entra (Step 6), copy its Object ID, add a `<map>` entry in `<roles_mapping>`, `CREATE ROLE` in ClickHouse if needed, and `SYSTEM RELOAD CONFIG`. No restart required.

Group renames in Entra don't affect anything — you map by GUID, which is stable. Group deletion does — recreate the group, grab the new GUID, update the mapping.

If your tenant runs on a sovereign cloud, override the auto-derived URLs. For Azure Government:

```xml
<jwks_uri>https://login.microsoftonline.us/<TENANT_ID>/discovery/v2.0/keys</jwks_uri>
<expected_issuer>https://login.microsoftonline.us/<TENANT_ID>/v2.0</expected_issuer>
```

(Azure China uses `login.partner.microsoftonline.cn`.)

For production, lock down outbound auth-subsystem calls so ClickHouse won't reach anything but Microsoft:

```xml
<remote_url_allow_hosts>
    <host_regexp>^login\.microsoftonline\.com$</host_regexp>
</remote_url_allow_hosts>
```

This is belt-and-suspenders against configuration mistakes or supply-chain attacks redirecting auth traffic somewhere it shouldn't go.

## A note on App Roles

This guide uses Entra security-group memberships as the source of truth for authorization. An alternative is **App Roles** — role names declared in the app's manifest, with users or groups assigned to them in Enterprise applications. The token then carries `"roles": ["clickhouse_admin"]` (strings you chose) instead of `"groups": ["<guid>"]`.

The trade-offs are pretty clean. App Roles give you human-readable strings instead of GUIDs, dodge the 200-group overage entirely, and live in the app manifest (portable across tenants). Group claims keep authorization data in the Entra directory where you presumably already manage it. Many setups use both: define a small set of app roles (`clickhouse_admin`, `clickhouse_analyst`, `clickhouse_readonly`) and assign existing security groups to those roles in Enterprise apps. The token then carries `roles` with the strings ClickHouse understands, while the group → role mapping lives in Entra rather than `config.xml`.

To switch, set `<groups_claim>roles</groups_claim>` in the processor config and either drop `<roles_mapping>` (if your app-role values match your ClickHouse role names 1:1) or keep it for an extra translation layer.

## Wrap-up

The whole exercise is roughly:

1. Find or register the app, note its client ID.
2. Expose the app as an API, set the Application ID URI, define at least one scope.
3. Force v2.0 tokens.
4. (optional) Put `preferred_username` and/or `groups` into the access token via `optionalClaims`.
5. (optional, if doing groups) Set `groupMembershipClaims` — `"SecurityGroup"` for small tenants, `"ApplicationGroup"` for large ones.
6. (only if you chose `ApplicationGroup` in Step 5) Assign the relevant security groups to the app.
7. Request tokens with `scope=api://your-app/...`, never with Graph permissions.
8. Configure ClickHouse with the tenant ID, your app's audience, and (if doing groups) a GUID → role map.

The pieces individually are all well-documented by Microsoft, but Entra's defaults push you toward Graph-bound tokens and ID-token-only group claims, neither of which work for a third-party API like ClickHouse. With the configuration above, you end up with standard local-OIDC validation — JWKS signature check, issuer pin, audience pin, claim extraction — and the rest of ClickHouse's existing RBAC takes over from there.

## References

- [Access tokens in the Microsoft identity platform](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens)
- [Configure group claims for applications](https://learn.microsoft.com/en-us/entra/identity/hybrid/connect/how-to-connect-fed-group-claims)
- [Configure optional claims](https://learn.microsoft.com/en-us/entra/identity-platform/optional-claims)
- [Add app roles and get them from a token](https://learn.microsoft.com/en-us/entra/identity-platform/howto-add-app-roles-in-apps)
- [OpenID Connect on the Microsoft identity platform](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc)

