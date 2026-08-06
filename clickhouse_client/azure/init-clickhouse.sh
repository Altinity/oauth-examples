#!/bin/bash
# Both jwt users are pre-defined here, so jwt_processors.xml needs no token user
# directory. Names come from the token claims, and one is an email, so SQL not XML.
# No grants: these scenarios only check which user a token authenticates as.
set -eu

mkuser() {
    # escape \ then ` so the value can't alter the quoted identifier
    local u=${1//\\/\\\\}
    u=${u//\`/\`\`}
    clickhouse client --query "CREATE USER IF NOT EXISTS \`${u}\` IDENTIFIED WITH jwt"
    echo "created pre-defined jwt user '${1}'"
}

mkuser "$CH_JWT_USER"      # azure-interactive.sh, = the token's upn
mkuser "$CH_JWT_APP_USER"  # azure-client-credentials.sh, = the service principal's oid
