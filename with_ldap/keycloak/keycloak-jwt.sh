#!/usr/bin/env bash
# Keycloak Resource Owner Password Credentials grant -> id_token. Intended
# for `clickhouse-client --jwt-command ./keycloak-jwt.sh`.
#
# ROPC is deprecated by OAuth2 BCP (no MFA, no consent screen, password is
# handled by the client). We use it here intentionally because the goal is a
# zero-interaction local demo. For a real device-code flow, see the Azure
# example in ../../jwt_command/azure/.
#
# Pure bash + wget so the script runs unmodified inside the stock CH image.
#
# Env:
#   KEYCLOAK_BASE_URL   Keycloak base URL  (default: http://keycloak:8080)
#                       From inside the compose network this is the service
#                       hostname; from your host shell, override to
#                       http://localhost:8080.
#   KEYCLOAK_REALM      realm name         (default: ch-demo)
#   KEYCLOAK_CLIENT_ID  public client ID   (default: clickhouse-client)
#   KEYCLOAK_SCOPE      OAuth scope        (default: openid email profile)
#   KEYCLOAK_USERNAME   end-user username  (required)
#   KEYCLOAK_PASSWORD   end-user password  (required)
#   KC_TOKEN_CACHE      cache path         (default: ~/.cache/ch-keycloak-jwt.json)
#
# stdout: id_token (single line).
# stderr: diagnostics on failure only.

set -euo pipefail

BASE="${KEYCLOAK_BASE_URL:-http://keycloak:8080}"
REALM="${KEYCLOAK_REALM:-ch-demo}"
CLIENT_ID="${KEYCLOAK_CLIENT_ID:-clickhouse-client}"
SCOPE="${KEYCLOAK_SCOPE:-openid email profile}"
USERNAME="${KEYCLOAK_USERNAME:?KEYCLOAK_USERNAME is required}"
PASSWORD="${KEYCLOAK_PASSWORD:?KEYCLOAK_PASSWORD is required}"
CACHE="${KC_TOKEN_CACHE:-${HOME:-/tmp}/.cache/ch-keycloak-jwt.json}"

# Flat-JSON field extractors. sed -E for GNU/BSD compat.
json_str() { sed -nE 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' | head -n1; }
json_num() { sed -nE 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' | head -n1; }

urlencode() {
    local s=$1 out= c
    for (( i=0; i<${#s}; i++ )); do
        c=${s:i:1}
        case "$c" in
            [a-zA-Z0-9._~-]) out+=$c ;;
            *) out+=$(printf '%%%02X' "'$c") ;;
        esac
    done
    printf '%s' "$out"
}

# Serve from cache if still valid.
if [[ -f "$CACHE" ]]; then
    exp=$(json_num exp_unix < "$CACHE" || true)
    if [[ -n "$exp" ]] && (( exp > $(date +%s) + 30 )); then
        json_str id_token < "$CACHE"
        exit 0
    fi
fi

body="grant_type=password"
body+="&client_id=$(urlencode "$CLIENT_ID")"
body+="&username=$(urlencode "$USERNAME")"
body+="&password=$(urlencode "$PASSWORD")"
body+="&scope=$(urlencode "$SCOPE")"

WGET_ERR_FILE=$(mktemp)
trap 'rm -f "$WGET_ERR_FILE"' EXIT

token_url="${BASE}/realms/${REALM}/protocol/openid-connect/token"
resp=$(wget -O- --content-on-error \
    --header="Content-Type: application/x-www-form-urlencoded" \
    --post-data="$body" "$token_url" 2>"$WGET_ERR_FILE" || true)

id_token=$(printf '%s' "$resp" | json_str id_token)
if [[ -z "$id_token" ]]; then
    {
        echo "Keycloak token request failed."
        echo "URL: $token_url"
        echo "wget stderr:"; sed 's/^/  /' "$WGET_ERR_FILE"
        echo "response body (${#resp} bytes):"; printf '  %s\n' "${resp:-<empty>}"
    } >&2
    exit 1
fi

expires_in=$(printf '%s' "$resp" | json_num expires_in); expires_in=${expires_in:-3600}
mkdir -p "$(dirname "$CACHE")"
umask 077
printf '{"id_token":"%s","exp_unix":%d}\n' "$id_token" \
    "$(( $(date +%s) + expires_in ))" > "$CACHE"
printf '%s\n' "$id_token"
