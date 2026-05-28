#!/usr/bin/env bash
# Emits a JWT that LOOKS like an Azure-issued token for $CH_USERNAME but has a
# junk signature. Use as `clickhouse-client --jwt-command ./azure-bad-jwt.sh`
# to confirm that ClickHouse's Entra JWT validator actually verifies signatures
# and rejects forged tokens even when the username_claim matches a real user.
#
# Env:
#   CH_USERNAME       value placed in the "email" claim         (required)
#   AZURE_TENANT_ID   value placed in the "iss" claim           (optional)
#   AZURE_CLIENT_ID   value placed in the "aud" claim           (optional)

set -euo pipefail

: "${CH_USERNAME:?CH_USERNAME is required}"
tenant="${AZURE_TENANT_ID:-00000000-0000-0000-0000-000000000000}"
client="${AZURE_CLIENT_ID:-00000000-0000-0000-0000-000000000000}"

b64url() { base64 -w0 | tr '+/' '-_' | tr -d '='; }

now=$(date +%s)
exp=$(( now + 3600 ))

header='{"alg":"RS256","typ":"JWT","kid":"fake-key-id"}'
printf -v payload \
    '{"iss":"https://login.microsoftonline.com/%s/v2.0","aud":"%s","email":"%s","exp":%d,"iat":%d,"sub":"fake-sub"}' \
    "$tenant" "$client" "$CH_USERNAME" "$exp" "$now"

h=$(printf '%s' "$header"  | b64url)
p=$(printf '%s' "$payload" | b64url)
s=$(printf 'not-a-real-signature' | b64url)

printf '%s.%s.%s\n' "$h" "$p" "$s"
