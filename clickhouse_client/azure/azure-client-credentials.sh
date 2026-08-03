#!/usr/bin/env bash
# Azure Entra client-credentials -> app access token. Intended for use as
# `clickhouse-client --jwt-command /usr/local/bin/azure-client-credentials.sh`.
#
# The app authenticates with its own client_id + client_secret and gets an *app*
# access token: no user, no browser, no `upn`. ClickHouse resolves it with the
# `azure_app` processor (username_claim=oid) and auto-provisions a user named
# after the service principal's object id.
#
# Pure bash + wget so the script runs unmodified inside the stock CH image.
#
# Env:
#   AZURE_TENANT_ID       tenant GUID                                 (required)
#   AZURE_CLIENT_ID       app registration client ID                  (required)
#   AZURE_CLIENT_SECRET   client secret                               (required)
#   AZURE_SCOPE           OAuth scope (default: <CLIENT_ID>/.default)
#   AZURE_TOKEN_CACHE     cache path  (default: ~/.cache/ch-azure-cc-jwt.json)
#
# stdout: access_token (single line).
# stderr: diagnostics.

set -euo pipefail

: "${AZURE_TENANT_ID:?AZURE_TENANT_ID is required}"
: "${AZURE_CLIENT_ID:?AZURE_CLIENT_ID is required}"
: "${AZURE_CLIENT_SECRET:?AZURE_CLIENT_SECRET is required for client-credentials}"
# Bare-GUID /.default: an app requesting a token for itself must use this form
# or Azure returns AADSTS90009. No offline_access — client-credentials never
# issues a refresh token, so renewal is just another grant.
SCOPE="${AZURE_SCOPE:-${AZURE_CLIENT_ID}/.default}"
CACHE="${AZURE_TOKEN_CACHE:-${HOME:-/tmp}/.cache/ch-azure-cc-jwt.json}"

# x-www-form-urlencoded encoder for values that may contain reserved chars
# (Azure client secrets are often base64-ish: +, /, =, ~ all show up).
urlencode() {
    local s=$1 out= i c
    for (( i=0; i<${#s}; i++ )); do
        c=${s:i:1}
        case $c in
            [a-zA-Z0-9._~-]) out+=$c ;;
            *) out+=$(printf '%%%02X' "'$c") ;;
        esac
    done
    printf '%s' "$out"
}

# Extract a flat JSON field. sed -E so the regex works under GNU and BSD sed.
json_str() { sed -nE 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' | head -n1; }
json_num() { sed -nE 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' | head -n1; }

post() {
    # wget exits non-zero on HTTP 4xx, but --content-on-error still writes the
    # body; ignore the exit so callers can parse the error response.
    wget -O- --content-on-error \
        --header="Content-Type: application/x-www-form-urlencoded" \
        --post-data="$2" "$1" 2>/dev/null || true
}

# Serve from cache while it has more than 30s left.
if [[ -f "$CACHE" ]]; then
    exp=$(json_num exp_unix < "$CACHE" || true)
    if [[ -n "$exp" ]] && (( exp > $(date +%s) + 30 )); then
        json_str access_token < "$CACHE"
        exit 0
    fi
fi

base="https://login.microsoftonline.com/${AZURE_TENANT_ID}/oauth2/v2.0"

body="grant_type=client_credentials"
body+="&client_id=$(urlencode "$AZURE_CLIENT_ID")"
body+="&client_secret=$(urlencode "$AZURE_CLIENT_SECRET")"
body+="&scope=$(urlencode "$SCOPE")"

resp=$(post "${base}/token" "$body")
access_token=$(printf '%s' "$resp" | json_str access_token)
if [[ -z "$access_token" ]]; then
    echo "client-credentials grant failed: $resp" >&2
    exit 1
fi

expires_in=$(printf '%s' "$resp" | json_num expires_in); expires_in=${expires_in:-3600}
mkdir -p "$(dirname "$CACHE")"
umask 077
printf '{"access_token":"%s","exp_unix":%d}\n' \
    "$access_token" "$(( $(date +%s) + expires_in ))" > "$CACHE"

printf '%s\n' "$access_token"
