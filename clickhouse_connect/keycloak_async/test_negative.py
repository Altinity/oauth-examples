"""
Negative-path tests: what ClickHouse's token validation rejects.

Sends deliberately bad tokens straight to the HTTP interface (raw requests,
so status codes and the X-ClickHouse-Exception-Code header are observable)
plus one driver-level check that clickhouse-connect surfaces rejection as a
clean error. Covers malformed tokens, forged/tampered signatures, wrong
issuer keys, unknown principals, per-user CLAIMS (audience) checks, and
clock-skew/expiry behavior with verifier_leeway=0 + token_cache_lifetime=10.

Run: python test_negative.py  (stack must be up; runtime ~45s)
"""
import base64
import json
import os
import time

import clickhouse_connect
import requests
from clickhouse_connect.driver.exceptions import ClickHouseError

from app import token_exp

KEYCLOAK_BASE_URL = os.environ.get(
    "KEYCLOAK_BASE_URL",
    f"http://localhost:{os.environ.get('KEYCLOAK_PORT', '8080')}",
)
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "ch-demo")
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "clickhouse-client")

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CH_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
CH_URL = f"http://{CH_HOST}:{CH_PORT}/"

TOKEN_CACHE_LIFETIME = 10  # matches jwt_processors.xml


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def fetch_token(username: str, realm: str = KEYCLOAK_REALM,
                client_id: str = KEYCLOAK_CLIENT_ID, password: str = "demo") -> str:
    resp = requests.post(
        f"{KEYCLOAK_BASE_URL}/realms/{realm}/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": client_id,
              "username": username, "password": password},
        timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def ch_auth(token: str) -> tuple[int, str, str]:
    """(http_status, clickhouse_exception_code, body) for a bearer token."""
    resp = requests.get(CH_URL, params={"query": "SELECT currentUser()"},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10)
    return resp.status_code, resp.headers.get("X-ClickHouse-Exception-Code", ""), resp.text.strip()


def expect_accepted(token: str, user: str, label: str) -> None:
    status, code, body = ch_auth(token)
    assert status == 200 and body == user, (label, status, code, body)
    print(f"OK accepted: {label} -> {body}")


def expect_rejected(token: str, label: str) -> None:
    status, code, body = ch_auth(token)
    assert status != 200 and body != "", (label, status, code)
    # 516 = AUTHENTICATION_FAILED; anything else would mean the request
    # failed for the wrong reason (e.g. a parse error we didn't intend)
    assert code == "516", (label, status, code, body[:120])
    print(f"OK rejected (516): {label}")


def test_malformed_tokens():
    for label, token in [
        ("empty token", ""),
        ("not a JWT", "garbage"),
        ("two segments", "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0"),
        ("junk base64 segments", "!!!.???.###"),
        ("random bytes as segments",
         f"{b64url(os.urandom(16))}.{b64url(os.urandom(32))}.{b64url(os.urandom(32))}"),
    ]:
        expect_rejected(token, f"malformed: {label}")


def test_forged_and_tampered_signatures(alice_token: str):
    # alg=none forgery: correct claims, no signature to verify
    header = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({
        "preferred_username": "alice@example.com",
        "exp": int(time.time()) + 300, "iat": int(time.time()),
        "iss": f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}",
    }).encode())
    expect_rejected(f"{header}.{payload}.", "alg=none forgery")

    # tampered payload, genuine signature: privilege escalation attempt
    h, p, s = alice_token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    claims["preferred_username"] = "bob@example.com"
    expect_rejected(f"{h}.{b64url(json.dumps(claims).encode())}.{s}",
                    "tampered payload (alice token rewritten to bob)")

    # signature swapped for random bytes
    expect_rejected(f"{h}.{p}.{b64url(os.urandom(len(s) // 2))}",
                    "signature replaced with random bytes")


def test_wrong_issuer_keys():
    # structurally perfect RS256 JWT, signed by the master realm's keys —
    # not the ch-demo JWKS the processor trusts
    admin_token = requests.post(
        f"{KEYCLOAK_BASE_URL}/realms/master/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "admin-cli",
              "username": os.environ.get("KEYCLOAK_ADMIN_USER", "admin"),
              "password": os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "admin")},
        timeout=10).json()["access_token"]
    expect_rejected(admin_token, "valid JWT signed by a different realm's keys")


def test_unknown_principal():
    # mallory exists in Keycloak but has no ClickHouse user and there is no
    # auto-provisioning user directory in this demo
    expect_rejected(fetch_token("mallory@example.com"),
                    "valid token for a principal ClickHouse doesn't know")


def test_claims_audience_checks():
    # both tokens are valid and identical in shape (azp=clickhouse-client);
    # only the per-user CLAIMS requirement differs
    expect_rejected(fetch_token("carol@example.com"),
                    "CLAIMS mismatch: carol requires azp=another-client")
    expect_accepted(fetch_token("dave@example.com"), "dave@example.com",
                    "CLAIMS match: dave requires azp=clickhouse-client")


def test_clock_skew_and_expiry():
    # verifier_leeway=0: a token presented for the FIRST time after exp is
    # rejected immediately — no grace window. +5s covers container clock lag.
    fresh = fetch_token("alice@example.com")
    wait = token_exp(fresh) - time.time() + 5
    print(f"    (waiting {max(wait, 0):.0f}s past exp; token never presented)")
    time.sleep(max(wait, 0))
    expect_rejected(fresh, "expired token, first presentation (leeway=0)")

    # token_cache_lifetime=10: a token CACHED while valid keeps working past
    # exp until the cache entry lapses, then is rejected (this Antalya
    # build's documented-behavior deviation — see README). The entry is
    # created 5s before exp so the overlap survives a few seconds of
    # container clock drift in either direction.
    cached = fetch_token("alice@example.com")
    exp = token_exp(cached)
    wait = exp - 5 - time.time()
    print(f"    (waiting {max(wait, 0):.0f}s to warm the cache 5s before exp)")
    time.sleep(max(wait, 0))
    expect_accepted(cached, "alice@example.com", "cache warm-up just before exp")
    time.sleep(7)  # now past exp, cache entry ~7s old (< 10s lifetime)
    expect_accepted(cached, "alice@example.com",
                    "expired token still inside token_cache_lifetime")
    print("    (waiting 5s more for the cache to lapse)")
    time.sleep(5)  # cache entry ~12s old, token ~7s past exp
    expect_rejected(cached, "expired token after the cache entry lapsed")


def test_driver_surfaces_rejection():
    try:
        clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT,
                                      access_token="not-a-real-token")
        raise AssertionError("driver accepted a garbage token")
    except ClickHouseError as exc:
        assert "516" in str(exc) or "AUTHENTICATION_FAILED" in str(exc), exc
        print("OK rejected: clickhouse-connect raises a clean auth error")


def main() -> None:
    alice_token = fetch_token("alice@example.com")
    expect_accepted(alice_token, "alice@example.com", "control: valid token")

    test_malformed_tokens()
    test_forged_and_tampered_signatures(alice_token)
    test_wrong_issuer_keys()
    test_unknown_principal()
    test_claims_audience_checks()
    test_driver_surfaces_rejection()
    test_clock_skew_and_expiry()
    print("\nall negative-path tests passed")


if __name__ == "__main__":
    main()
