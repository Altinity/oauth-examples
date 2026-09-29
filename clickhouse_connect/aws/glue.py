"""
Azure Entra interactive browser sign-in (MSAL: auth code + PKCE) ->
clickhouse-connect via a refreshable token_provider -> ClickHouse verifies the
access token with the `entra` processor and forwards it to AWS STS
(AssumeRoleWithWebIdentity) -> temporary role credentials sign the Glue calls.

The token only ever goes to ClickHouse; ClickHouse is what talks to AWS.

Env:
  AZURE_TENANT_ID   required
  AZURE_CLIENT_ID   required; the public test client
  AZURE_SCOPE       required; the API's delegated scope,
                    e.g. api://<API_CLIENT_ID>/access_as_user
"""
import os

import clickhouse_connect
import msal

TENANT = os.environ["AZURE_TENANT_ID"]
CLIENT = os.environ["AZURE_CLIENT_ID"]
SCOPES = os.environ["AZURE_SCOPE"].split()

CH_HOST = os.environ.get("CLICKHOUSE_HOST") or "localhost"
CH_PORT = int(os.environ.get("CLICKHOUSE_PORT") or "8123")


def make_token_provider():
    """The token_provider: called at client init and on each ClickHouse rejection.

    Both cases need a token the driver does not already hold, so after the
    first sign-in every call force-refreshes instead of reading MSAL's cache.
    """
    app = msal.PublicClientApplication(
        CLIENT, authority=f"https://login.microsoftonline.com/{TENANT}")

    def token_provider():
        result = None
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent_with_error(
                SCOPES, accounts[0], force_refresh=True)
        if not result or "access_token" not in result:
            result = app.acquire_token_interactive(SCOPES, prompt="select_account")
        if "access_token" not in result:
            raise RuntimeError(
                f"sign-in failed: {result.get('error')}: {result.get('error_description')}")
        return result["access_token"]

    return token_provider


def main():
    client = clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, token_provider=make_token_provider())
    user = client.query("SELECT currentUser()").result_rows[0][0]
    print(f"currentUser(): {user}")

    # Errors from STS or Glue fail this query rather than returning an empty list
    tables = client.query("SHOW TABLES FROM glue").result_rows
    print("SHOW TABLES FROM glue:")
    for (name,) in tables:
        print(f"  {name}")
    if not tables:
        print("  (no tables)")


if __name__ == "__main__":
    main()
