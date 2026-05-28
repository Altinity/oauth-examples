#!/usr/bin/env bash
# Azure Entra device-code flow -> id_token. Intended for use as
# `clickhouse-client --jwt-command ./azure-device-flow.sh`.
#
# Pure bash + wget so the script runs unmodified inside the stock CH image.
#
# Env:
#   AZURE_TENANT_ID       tenant GUID                                 (required)
#   AZURE_CLIENT_ID       app registration client ID                  (required)
#   AZURE_CLIENT_SECRET   client secret                               (optional)
#                         set this only if the app is a confidential client
#                         (i.e. "Allow public client flows" is OFF); for a
#                         public client, leave unset.
#   AZURE_SCOPE           OAuth scope (default: openid email profile offline_access)
#   AZURE_TOKEN_CACHE     cache path  (default: ~/.cache/ch-azure-jwt.json)
#
# stdout: id_token (single line).
# stderr: diagnostics.
# /dev/tty (if available): device-code prompt for the user. We write the prompt
# to the controlling terminal rather than stderr so it shows up immediately
# when invoked as `clickhouse-client --jwt-command ...` — the client forwards
# the child's stderr through a buffered WriteBuffer that only flushes after
# the script exits, which would hide the prompt until after the user has
# (somehow) already signed in.

set -euo pipefail

: "${AZURE_TENANT_ID:?AZURE_TENANT_ID is required}"
: "${AZURE_CLIENT_ID:?AZURE_CLIENT_ID is required}"
CLIENT_SECRET="${AZURE_CLIENT_SECRET:-}"
SCOPE="${AZURE_SCOPE:-openid email profile offline_access}"
CACHE="${AZURE_TOKEN_CACHE:-${HOME:-/tmp}/.cache/ch-azure-jwt.json}"

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

# Extract a flat JSON string field's value. $1 = field name, JSON on stdin.
# Uses sed -E so the regex works under both GNU sed and BSD sed (macOS):
# BSD sed's BRE doesn't recognize \+, but ERE + works in both.
json_str() { sed -nE 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' | head -n1; }
json_num() { sed -nE 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' | head -n1; }

# Write a user-facing message. Prefer /dev/tty so the prompt isn't hidden by a
# parent that buffers stderr (e.g. clickhouse-client --jwt-command); fall back
# to stderr when no controlling tty is available.
notify() {
    if { : > /dev/tty; } 2>/dev/null; then
        printf '%s\n' "$*" > /dev/tty
    else
        printf '%s\n' "$*" >&2
    fi
}

# wget's stderr lands here so callers can read it after a failed post. Using a
# fixed path (not a $(post) return value) because post() is invoked as
# $(post ...) to capture the body — variables set inside that subshell don't
# propagate to the parent.
WGET_ERR_FILE=$(mktemp)
trap 'rm -f "$WGET_ERR_FILE"' EXIT

post() {
    # wget exits non-zero on HTTP 4xx, but --content-on-error still writes the
    # body; ignore the exit so callers can parse the error response.
    wget -O- --content-on-error \
        --header="Content-Type: application/x-www-form-urlencoded" \
        --post-data="$2" "$1" 2>"$WGET_ERR_FILE" || true
}

# Serve from cache if still valid.
if [[ -f "$CACHE" ]]; then
    exp=$(json_num exp_unix < "$CACHE" || true)
    if [[ -n "$exp" ]] && (( exp > $(date +%s) + 30 )); then
        json_str id_token < "$CACHE"
        exit 0
    fi
fi

base="https://login.microsoftonline.com/${AZURE_TENANT_ID}/oauth2/v2.0"

dev=$(post "${base}/devicecode" "client_id=${AZURE_CLIENT_ID}&scope=${SCOPE// /+}")
device_code=$(printf '%s' "$dev" | json_str device_code)
if [[ -z "$device_code" ]]; then
    echo "devicecode init failed: $dev" >&2
    exit 1
fi

# Prefer Microsoft's pre-formatted message; fall back to constructing one.
msg=$(printf '%s' "$dev" | json_str message)
if [[ -n "$msg" ]]; then
    notify "$msg"
else
    uri=$(printf '%s' "$dev" | json_str verification_uri)
    code=$(printf '%s' "$dev" | json_str user_code)
    notify "To sign in, visit $uri and enter code $code"
fi

interval=$(printf '%s' "$dev" | json_num interval); interval=${interval:-5}
expires_in=$(printf '%s' "$dev" | json_num expires_in); expires_in=${expires_in:-900}
deadline=$(( $(date +%s) + expires_in ))

# Bail after this many consecutive non-OAuth-protocol failures (empty body,
# unparseable JSON, etc). Prevents an infinite loop when something between us
# and Microsoft consistently swallows the response.
MAX_CONSECUTIVE_BAD=3
consecutive_bad=0

dump_diag() {
    local label=$1 resp=$2
    {
        echo "--- $label ---"
        echo "URL:  ${base}/token"
        echo "wget stderr:"
        sed 's/^/  /' "$WGET_ERR_FILE"
        echo "response body (${#resp} bytes):"
        printf '  %s\n' "${resp:-<empty>}"
        echo "--- end ---"
    } > /dev/tty 2>&1 || {
        echo "--- $label ---"
        echo "URL:  ${base}/token"
        echo "wget stderr:"; sed 's/^/  /' "$WGET_ERR_FILE"
        echo "response body (${#resp} bytes):"
        printf '  %s\n' "${resp:-<empty>}"
        echo "--- end ---"
    } >&2
}

token_body="grant_type=urn:ietf:params:oauth:grant-type:device_code"
token_body+="&client_id=${AZURE_CLIENT_ID}"
token_body+="&device_code=${device_code}"
# Confidential-client apps require the secret on the token endpoint; public
# clients (Allow public client flows = ON) must omit it.
if [[ -n "$CLIENT_SECRET" ]]; then
    token_body+="&client_secret=$(urlencode "$CLIENT_SECRET")"
fi

while (( $(date +%s) < deadline )); do
    sleep "$interval"
    resp=$(post "${base}/token" "$token_body")

    id_token=$(printf '%s' "$resp" | json_str id_token)
    if [[ -n "$id_token" ]]; then
        expires_in=$(printf '%s' "$resp" | json_num expires_in); expires_in=${expires_in:-3600}
        mkdir -p "$(dirname "$CACHE")"
        umask 077
        printf '{"id_token":"%s","exp_unix":%d}\n' "$id_token" \
            "$(( $(date +%s) + expires_in ))" > "$CACHE"
        printf '%s\n' "$id_token"
        exit 0
    fi

    err=$(printf '%s' "$resp" | json_str error)
    case "$err" in
        authorization_pending) consecutive_bad=0 ;;
        slow_down) consecutive_bad=0; interval=$(( interval + 5 )) ;;
        "")
            consecutive_bad=$(( consecutive_bad + 1 ))
            dump_diag "poll #${consecutive_bad}: no OAuth error field" "$resp"
            if (( consecutive_bad >= MAX_CONSECUTIVE_BAD )); then
                echo "giving up after ${consecutive_bad} consecutive bad responses — see diagnostics above" >&2
                exit 1
            fi
            ;;
        *)
            dump_diag "fatal OAuth error: $err" "$resp"
            echo "device flow error ($err): $resp" >&2
            exit 1
            ;;
    esac
done

echo "device flow timed out before user completed sign-in" >&2
exit 1
