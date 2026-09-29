#!/bin/bash
# The Glue catalog is created once, by the admin (`default`), with no AWS
# credentials of its own: each query assumes AWS_ROLE_ARN with the caller's token.
set -eu

chc() { clickhouse client --query "$1"; }

# SQL string literal: escape \ then '
sq() {
    local v=${1//\\/\\\\}
    printf "'%s'" "${v//\'/\\\'}"
}

chc "CREATE ROLE IF NOT EXISTS glue_users"

# warehouse is required but unused by Glue; namespaces limits the catalog to
# the one Glue database
chc "CREATE DATABASE IF NOT EXISTS glue
ENGINE = DataLakeCatalog
SETTINGS
    catalog_type = 'glue',
    region = $(sq "${GLUE_REGION}"),
    warehouse = $(sq "${GLUE_DATABASE}"),
    namespaces = $(sq "${GLUE_DATABASE}"),
    aws_role_arn = $(sq "${AWS_ROLE_ARN}"),
    oauth_forward_user_token = 1"

chc "GRANT SHOW, SELECT ON glue.* TO glue_users"
echo "created Glue catalog 'glue' -> ${GLUE_DATABASE} (${GLUE_REGION}) as ${AWS_ROLE_ARN}"
