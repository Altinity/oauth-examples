#!/bin/bash
# The jwt user is named after the token's `upn` — an email, so SQL, not XML.
set -eu

chc() { clickhouse client --query "$1"; }

chc "CREATE ROLE IF NOT EXISTS azure_jwt_role"
chc "GRANT SELECT ON default.* TO azure_jwt_role"

# escape \ then ` so the value can't alter the quoted identifier
user=${CH_JWT_USER//\\/\\\\}
user=${user//\`/\`\`}
chc "CREATE USER IF NOT EXISTS \`${user}\` IDENTIFIED WITH jwt"
chc "GRANT azure_jwt_role TO \`${user}\`"
chc "ALTER USER \`${user}\` DEFAULT ROLE azure_jwt_role"  # DEFAULT ROLE: else no active roles
echo "created pre-defined jwt user '${CH_JWT_USER}'"
