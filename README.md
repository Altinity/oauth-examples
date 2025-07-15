## Demo setup for Grafana <-> ClickHouse OAuth integration

This setup runs Grafana and ClickHouse in Docker (locally). Grafana uses Azure AD (Entra ID) as external authenticator, and ClickHouse uses it as an external user directory (IdP) as well as external authenticator.

### How to run

#### Configure Azure AD

To run this setup, you must have Azure already configured. You need to know two values:


* Your Tenant ID. You can find it in your Home page (Go to [entra.microsoft.com](https://entra.microsoft.com/) and select Overview):
  
  ![image](https://github.com/user-attachments/assets/47b72126-2da6-4c65-96f1-0049a945e82d)

* Your application's Client ID (Application ID). You can find it in your app overview:

  ![image](https://github.com/user-attachments/assets/44896038-d549-4e69-bf8a-aa8bc00d309c)

* One of your application's Client Secrets, which you can find at in [Entra dashboard](https://entra.microsoft.com/) under App Registrations->Certificates & secrets.

#### Run in Docker

0. Clone this repository:
```bash
git clone https://github.com/zvonand/grafana-oauth.git
```

1. Inside it, create a file called `.env` with the following contents (use your corresponding values):
```
CLIENT_ID=<Client ID (Application ID)>
TENANT_ID=<Tenant ID>
CLIENT_SECRET=<Client Secret>
```

You can create this file with this command and after that insert your values there:
```bash
printf "CLIENT_ID=<Client ID (Application ID)>\nTENANT_ID=<Tenant ID>\nCLIENT_SECRET=<Client Secret>\n" > .env
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

5. Click "Sign in with Azure AD" on the login page:

   ![Screenshot from 2025-02-19 13-12-23](https://github.com/user-attachments/assets/ebd9d3fa-0048-49a7-bec2-948253c1f8ee)

6. Login to your Microsoft / Azure account if prompted.

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

3. Enter the query: `select currentUser()`, and press "Run Query", the username shall be printed. The username is the same as `sub` value in your Azure AD. 

By default, ClickHouse does not grant any roles or privileges to external users. All the user can do is basically view his own name. That is why a role `token_test_role_1` is created on start-up, and and ClickHouse is configured to assign this role to __all__ users coming from Azure. This role allows to read all tables in `default` database. There is also a pre-defined table `default.test_table_1`.

You can verify that user is actually able to read the table:

4. Enter the query: `select * from default.test_table_1`, and press "Run Query". No exceptions shall be thrown, and table contents will be printed.
