## Demo setup for Grafana <-> Keycloak <-> ClickHouse OAuth integration

This setup runs Grafana, ClickHouse and Keycloak in Docker (locally). Keycloak is used as an external authenticator and external user directory by both Grafana and ClickHouse.

In this set-up, everything is already pre-configured and dockerized.

in Keycloak, a user with name `demo` is created. This user belongs to group `grafana-admins`, which is translated to Admin group in Grafana itself. Also, this role will be passed down to ClickHouse.

### How to run

1. Clone this repository and navigate to this example's directory:
```bash
git clone https://github.com/Altinity/oauth-examples.git
cd oauth-examples/grafana/keycloak
```

2. Start Docker containers
```bash
docker compose up
```

3. Wait for this message in Compose logs:
```
grafana-1     | ✔ Downloaded and extracted vertamedia-clickhouse-datasource v3.3.0 zip successfully to /var/lib/grafana/plugins/vertamedia-clickhouse-datasource
```

4. In your web browser, go to `http://localhost:3000`.

5. Click "Sign in with Keycloak" on the login page:

6. If prompted, use `demo` as both username and password.

7. Go to "Connections" -> "Data Sources" tab (in the left vertical menu), press "add data source" and search for **Altinity plugin for ClickHouse** ![Screenshot from 2024-12-16 15-41-05](https://github.com/user-attachments/assets/fe2ce8d1-ea4a-488b-9cc7-c44c270de5b0)

8. In this view ![Screenshot from 2024-12-16 15-52-02](https://github.com/user-attachments/assets/e2f3ebf9-c88b-460d-934c-f1bf045d511b)
   * Enter URL `http://clickhouse:8123`
   * Toggle "Forward OAuth Identity" switch

9. Click "Save & Test" at the bottom of this page: ![Screenshot from 2024-12-16 15-54-48](https://github.com/user-attachments/assets/b6612aab-a632-4097-b43c-55188a6a73be).


#### Try it

1. Go to "Explore" tab:

![Screenshot from 2024-12-16 15-56-05](https://github.com/user-attachments/assets/ddf1fbe4-3341-41df-b935-00cc064ffb74)

2. In this view:
    * Switch to "SQL Editor" mode
    * Switch "Format As" to "table"
  
![Screenshot from 2024-12-16 15-57-17](https://github.com/user-attachments/assets/5fbeac22-8a20-432e-b779-b07c459bf15e)

3. Enter the query: `select currentUser()`, and press "Run Query", the username shall be printed. It is the Keycloak user's `sub` claim (a UUID), because `username_claim` defaults to `sub`. Set `username_claim` to `email` or `preferred_username` in `clickhouse/processor_directory.xml` for human-readable names.

By default, ClickHouse does not grant any roles or privileges to external users. All the user can do is basically view his own name. That is why the roles `general_role` and `can_read` are created on start-up by `clickhouse/startup_scripts.xml`.

* ClickHouse is configured to assign role `general_role` to __all__ users defined in Keycloak. This role allows to read `can_read_general` table in `default` database. This table is pre-defined and has one row.
* Also, in Keycloak your user belongs to a group `can-read`. Keycloak group names are passed to ClickHouse in the token's `groups` claim, and `roles_transform` (`s/-/_/g` in `clickhouse/processor_directory.xml`) rewrites the dashes, so the group `can-read` maps to the ClickHouse role `can_read`. That role allows the user to read `default.can_read_specific` table. This table is also pre-defined and has one row.

You can verify that user is actually able to read the tables:

4. Enter the query: `select * from default.can_read_specific` or `select * from default.can_read_general`, and press "Run Query". No exceptions shall be thrown, and table contents will be printed.


### Debugging

You can obtain a token directly from your Keycloak instance. The realm shipped
here already has Direct Access Grants enabled on `grafana-client`, so this works
as-is:

```bash
ACCESS_TOKEN=$(curl -s --request POST \
  http://localhost:8080/realms/grafana/protocol/openid-connect/token \
  --header "Content-Type: application/x-www-form-urlencoded" \
  --data "client_id=grafana-client" --data "client_secret=grafana-secret" \
  --data "username=demo" --data "password=demo" \
  --data "grant_type=password" --data "scope=openid profile email" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
  "http://localhost:8123/?query=SELECT%20currentUser()%2C%20currentRoles()"
```

For a realm of your own, the request needs:

1. Navigate to your realm → Clients → select your client app.
2. Enable Direct Access Grants.
3. If "Client authentication" is on (confidential), pass `client_secret` as above;
   if it is off (public), drop the `client_secret` parameter.

### Troubleshooting

**`Code: 36. DB::Exception: Token authentication is disabled`** while
`select * from system.server_settings where name = 'enable_token_auth'` still
reports `1`. A token processor that fails to parse disables token
authentication for the whole server, and the login error does not say which
one or why. The reason is only in the server log:

```bash
docker compose logs clickhouse | grep -iE "token processor|token authentication"
```

The usual cause is a key that is not valid for the processor `type` — for
example `jwks_uri` on an `openid` processor, or `token_introspection_endpoint`
without `introspection_client_id` and `introspection_client_secret`.