#!/usr/bin/env bash
# Azure Entra interactive sign-in (authorization code + PKCE) -> user access
# token. Intended for use as
# `clickhouse-client --jwt-command /usr/local/bin/azure-interactive.sh`.
#
# Prints an authorize URL, waits for the browser redirect on a one-shot local
# listener, exchanges the code for tokens, and emits the ACCESS token. Delegated
# tokens carry `upn`, so ClickHouse resolves them with the `azure_user`
# processor. Later runs reuse the cache, then the refresh token, and only fall
# back to a new sign-in when both are spent.
#
# PKCE alone is enough for a public client (no secret). Set AZURE_CLIENT_SECRET
# only if the redirect URI is registered under the "Web" platform, which makes
# Azure demand client authentication on the code exchange (AADSTS7000218).
#
# Pure bash + wget + openssl + nc so the script runs unmodified inside the stock
# CH image, which has no curl, jq or python3.
#
# The script listens on the redirect URI's port. Running inside the container
# means that port must be published to the host, since the browser is out there.
#
# Env:
#   AZURE_TENANT_ID       tenant GUID                                 (required)
#   AZURE_CLIENT_ID       app registration client ID                  (required)
#   AZURE_CLIENT_SECRET   client secret; used only with the flag below
#   AZURE_INTERACTIVE_USE_SECRET
#                         1 to authenticate the client on the code/refresh
#                         exchange. Needed only when the redirect URI is
#                         registered under the "Web" platform. Default 0.
#   AZURE_SCOPE           OAuth scope
#                         (default: <CLIENT_ID>/.default offline_access)
#   AZURE_REDIRECT_URI    default http://localhost:8400/ — must be registered
#                         under "Mobile and desktop applications"
#   AZURE_SIGNIN_TIMEOUT  seconds to wait for the redirect (default 300)
#   AZURE_TOKEN_CACHE     cache path (default: ~/.cache/ch-azure-pkce-jwt.json)
#
# stdout: access_token (single line).
# stderr: diagnostics. /dev/tty: the sign-in prompt.

set -euo pipefail

: "${AZURE_TENANT_ID:?AZURE_TENANT_ID is required}"
: "${AZURE_CLIENT_ID:?AZURE_CLIENT_ID is required}"
# Opt-in, not inherited: this container also carries AZURE_CLIENT_SECRET for the
# client-credentials script, and sending a secret for a redirect URI registered
# under "Mobile and desktop applications" is rejected with 401 AADSTS700025
# ("client is public"). Set AZURE_INTERACTIVE_USE_SECRET=1 only when the
# redirect URI lives under the "Web" platform.
CLIENT_SECRET=""
if [[ "${AZURE_INTERACTIVE_USE_SECRET:-0}" == "1" ]]; then
    CLIENT_SECRET="${AZURE_CLIENT_SECRET:?AZURE_INTERACTIVE_USE_SECRET=1 needs AZURE_CLIENT_SECRET}"
fi
# Bare-GUID scope, not api://<client_id>: an app requesting a token for itself
# must use the GUID form or Azure returns AADSTS90009. offline_access asks for
# the refresh token that lets later runs skip the browser.
SCOPE="${AZURE_SCOPE:-${AZURE_CLIENT_ID}/.default offline_access}"
REDIRECT_URI="${AZURE_REDIRECT_URI:-http://localhost:8400/}"
SIGNIN_TIMEOUT="${AZURE_SIGNIN_TIMEOUT:-300}"
CACHE="${AZURE_TOKEN_CACHE:-${HOME:-/tmp}/.cache/ch-azure-pkce-jwt.json}"

base="https://login.microsoftonline.com/${AZURE_TENANT_ID}/oauth2/v2.0"

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

urldecode() { local s=${1//+/ }; printf '%b' "${s//%/\\x}"; }

# Extract a flat JSON field. sed -E so the regex works under GNU and BSD sed.
json_str() { sed -nE 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' | head -n1; }
json_num() { sed -nE 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' | head -n1; }

# Prefer /dev/tty: clickhouse-client forwards the child's stderr through a
# buffered WriteBuffer that only flushes once the script exits, which would hide
# the sign-in prompt until after the user was meant to have used it.
notify() {
    if { : > /dev/tty; } 2>/dev/null; then
        printf '%s\n' "$*" > /dev/tty
    else
        printf '%s\n' "$*" >&2
    fi
}

# wget's stderr lands here so a failed post can be diagnosed. A fixed path, not
# a return value: post() is called as $(post ...) to capture the body, and
# variables set in that subshell don't reach the parent.
WGET_ERR_FILE=$(mktemp)
trap 'rm -f "$WGET_ERR_FILE"' EXIT

post() {
    # wget exits non-zero on HTTP 4xx, but --content-on-error still writes the
    # body; ignore the exit so callers can parse the error response.
    wget -O- --content-on-error \
        --header="Content-Type: application/x-www-form-urlencoded" \
        --post-data="$2" "$1" 2>"$WGET_ERR_FILE" || true
}

# Explain an empty/unparseable token response using wget's own complaint.
post_failed() {
    echo "$1: ${2:-<empty response>}" >&2
    if [[ -s "$WGET_ERR_FILE" ]]; then
        echo "wget stderr:" >&2
        sed 's/^/  /' "$WGET_ERR_FILE" >&2
    fi
    # wget treats 401 as an HTTP auth challenge and discards the body, so
    # Azure's JSON never arrives. The token endpoint only 401s on
    # invalid_client, which here means the secret and the redirect URI's
    # platform disagree.
    if grep -q '401 Unauthorized' "$WGET_ERR_FILE" 2>/dev/null; then
        if [[ -n "$CLIENT_SECRET" ]]; then
            echo "hint: 401 invalid_client while sending a secret — likely AADSTS700025," \
                 "the redirect URI is a public 'Mobile and desktop applications' one." \
                 "Unset AZURE_INTERACTIVE_USE_SECRET." >&2
        else
            echo "hint: 401 invalid_client with no secret — likely AADSTS7000218, the" \
                 "redirect URI is registered under 'Web'. Set" \
                 "AZURE_INTERACTIVE_USE_SECRET=1." >&2
        fi
    fi
}

b64url() { base64 | tr -d '\n' | tr '+/' '-_' | tr -d '='; }

# The token endpoint needs client auth only for a confidential app.
secret_field() {
    if [[ -n "$CLIENT_SECRET" ]]; then
        printf '&client_secret=%s' "$(urlencode "$CLIENT_SECRET")"
    fi
}

save_tokens() {
    local access=$1 refresh=$2 expires_in=$3
    mkdir -p "$(dirname "$CACHE")"
    umask 077
    printf '{"access_token":"%s","refresh_token":"%s","exp_unix":%d}\n' \
        "$access" "$refresh" "$(( $(date +%s) + expires_in ))" > "$CACHE"
}

# Emit the access token from a token-endpoint response, or return 1.
emit() {
    local resp=$1 old_refresh=${2:-}
    local access refresh expires_in
    access=$(printf '%s' "$resp" | json_str access_token)
    [[ -n "$access" ]] || return 1
    refresh=$(printf '%s' "$resp" | json_str refresh_token)
    # RFC 6749 6 lets the IdP omit refresh_token on a refresh; keep the old one
    [[ -n "$refresh" ]] || refresh=$old_refresh
    expires_in=$(printf '%s' "$resp" | json_num expires_in); expires_in=${expires_in:-3600}
    save_tokens "$access" "$refresh" "$expires_in"
    printf '%s\n' "$access"
}

# ---- 1. cached access token -------------------------------------------------
cached_refresh=
if [[ -f "$CACHE" ]]; then
    exp=$(json_num exp_unix < "$CACHE" || true)
    cached_refresh=$(json_str refresh_token < "$CACHE" || true)
    if [[ -n "$exp" ]] && (( exp > $(date +%s) + 30 )); then
        json_str access_token < "$CACHE"
        exit 0
    fi
fi

# ---- 2. refresh token -------------------------------------------------------
if [[ -n "$cached_refresh" ]]; then
    resp=$(post "${base}/token" \
        "grant_type=refresh_token&client_id=$(urlencode "$AZURE_CLIENT_ID")\
&refresh_token=$(urlencode "$cached_refresh")&scope=$(urlencode "$SCOPE")$(secret_field)")
    if emit "$resp" "$cached_refresh"; then
        exit 0
    fi
    # A dead token, or one minted under the other client-auth mode
    # (invalid_client), needs a fresh sign-in; report anything else and stop.
    err=$(printf '%s' "$resp" | json_str error)
    case "$err" in
        invalid_grant|interaction_required|invalid_client) ;;
        *) post_failed "token refresh failed ($err)" "$resp"; exit 1 ;;
    esac
    notify "Cached sign-in is no longer usable ($err); signing in again."
fi

# ---- 3. interactive authorization code + PKCE -------------------------------
verifier=$(openssl rand 32 | b64url)
challenge=$(printf '%s' "$verifier" | openssl dgst -sha256 -binary | b64url)
state=$(openssl rand 16 | b64url)

authorize="${base}/authorize?client_id=$(urlencode "$AZURE_CLIENT_ID")"
authorize+="&response_type=code&response_mode=query"
authorize+="&redirect_uri=$(urlencode "$REDIRECT_URI")"
authorize+="&scope=$(urlencode "$SCOPE")"
authorize+="&state=${state}&code_challenge=${challenge}&code_challenge_method=S256"
authorize+="&prompt=select_account"

# Listener port comes from the redirect URI, so the two cannot drift apart.
hostport=${REDIRECT_URI#*://}; hostport=${hostport%%/*}
port=${hostport##*:}
[[ "$port" == "$hostport" ]] && port=80

notify "Open this URL in a browser on the host, then sign in:"
notify ""
notify "  $authorize"
notify ""
notify "Waiting up to ${SIGNIN_TIMEOUT}s for the redirect to ${REDIRECT_URI} ..."

body='<html><body><h1>Sign-in complete</h1><p>You can close this window and return to the terminal.</p></body></html>'
http_response=$(printf 'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s' \
    "${#body}" "$body")

# Browsers open speculative connections that carry no query string, so keep
# listening until a request actually brings code= or error=.
deadline=$(( $(date +%s) + SIGNIN_TIMEOUT ))
request=
while (( $(date +%s) < deadline )); do
    remaining=$(( deadline - $(date +%s) ))
    # sleep keeps nc's stdin open long enough for it to read the request and
    # write it to stdout before EOF tears the connection down
    raw=$( { printf '%s' "$http_response"; sleep 2; } \
        | timeout "$remaining" nc -l -p "$port" 2>/dev/null || true )
    line=$(printf '%s' "$raw" | sed -n '1p' | tr -d '\r')
    case "$line" in
        *"code="*|*"error="*) request=$line; break ;;
    esac
done

if [[ -z "$request" ]]; then
    echo "timed out waiting for the browser redirect on port ${port}" >&2
    exit 1
fi

query=${request#*\?}; query=${query%% *}
code=$(printf '%s' "$query" | tr '&' '\n' | sed -n 's/^code=//p')
got_state=$(printf '%s' "$query" | tr '&' '\n' | sed -n 's/^state=//p')
oauth_err=$(printf '%s' "$query" | tr '&' '\n' | sed -n 's/^error=//p')

if [[ -n "$oauth_err" ]]; then
    desc=$(printf '%s' "$query" | tr '&' '\n' | sed -n 's/^error_description=//p')
    echo "sign-in failed: $(urldecode "$oauth_err"): $(urldecode "${desc:-}")" >&2
    exit 1
fi
if [[ "$got_state" != "$state" ]]; then
    echo "state mismatch on the redirect — discarding the code" >&2
    exit 1
fi

token_body="grant_type=authorization_code&client_id=$(urlencode "$AZURE_CLIENT_ID")"
token_body+="&code=$(urlencode "$(urldecode "$code")")"
token_body+="&redirect_uri=$(urlencode "$REDIRECT_URI")"
token_body+="&code_verifier=${verifier}"
token_body+="&scope=$(urlencode "$SCOPE")$(secret_field)"

resp=$(post "${base}/token" "$token_body")
# The code is single-use, so an empty reply (transport failure, no OAuth error to
# read) is worth one retry rather than another trip through the browser.
if [[ -z "$resp" ]]; then
    notify "Empty reply from the token endpoint; retrying once."
    sleep 2
    resp=$(post "${base}/token" "$token_body")
fi

if ! emit "$resp"; then
    post_failed "code exchange failed" "$resp"
    exit 1
fi
