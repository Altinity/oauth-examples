"""
Keycloak ROPC -> clickhouse-connect with access_token.

Fetches a JWT from the local Keycloak via OAuth2 Resource Owner Password
Credentials, then hands it to clickhouse-connect via `access_token=`,
which forwards it to ClickHouse as `Authorization: Bearer <JWT>`. ROPC is
deprecated by OAuth2 BCP (no MFA, no consent) — used here only for a
zero-interaction local demo.
"""
import os
import sys

import clickhouse_connect
import requests


KEYCLOAK_BASE_URL = os.environ.get("KEYCLOAK_BASE_URL", "http://localhost:8080")
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "ch-demo")
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "clickhouse-client")
KEYCLOAK_USERNAME = os.environ["KEYCLOAK_USERNAME"]
KEYCLOAK_PASSWORD = os.environ["KEYCLOAK_PASSWORD"]

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CH_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))


def fetch_id_token() -> str:
    token_url = (
        f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
    )
    resp = requests.post(
        token_url,
        data={
            "grant_type": "password",
            "client_id": KEYCLOAK_CLIENT_ID,
            "username": KEYCLOAK_USERNAME,
            "password": KEYCLOAK_PASSWORD,
            "scope": "openid email profile",
        },
        timeout=10,
    )
    resp.raise_for_status()
    # id_token (not access_token): the 'email' claim that jwt_processors.xml
    # uses as username_claim lives in id_token by default in Keycloak.
    return resp.json()["id_token"]


def main() -> int:
    token = fetch_id_token()

    # cannot be combined with username/password; rotate via set_access_token()
    client = clickhouse_connect.get_client(
        host=CH_HOST,
        port=CH_PORT,
        access_token=token,
    )

    user = client.query("SELECT currentUser()").result_rows[0][0]
    print(f"user: {user}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
