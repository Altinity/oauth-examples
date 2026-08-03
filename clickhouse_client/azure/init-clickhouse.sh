#!/bin/bash
# Runs once on first container init: role, demo table, and a pre-defined jwt user
# named after the token's `upn` (an email, so created via SQL, not XML). The
# client-credentials flow needs no counterpart here — its user is
# auto-provisioned from the service principal's `oid`.
set -eu

chc() { clickhouse client --query "$1"; }

chc "CREATE ROLE IF NOT EXISTS azure_jwt_role"
chc "GRANT SELECT ON default.* TO azure_jwt_role"
chc "CREATE TABLE IF NOT EXISTS default.test_table_1 ENGINE = TinyLog AS SELECT toUInt64(123) AS id"

# escape \ then ` so the value can't alter the quoted identifier
user=${CH_JWT_USER//\\/\\\\}
user=${user//\`/\`\`}
chc "CREATE USER IF NOT EXISTS \`${user}\` IDENTIFIED WITH jwt"
chc "GRANT azure_jwt_role TO \`${user}\`"
chc "ALTER USER \`${user}\` DEFAULT ROLE azure_jwt_role"  # DEFAULT ROLE: else no active roles
echo "created pre-defined jwt user '${CH_JWT_USER}'"
