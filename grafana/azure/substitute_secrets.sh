#!/bin/bash

set -a
source .env
set +a

INPUT_XML="/clickhouse-config/processor_directory.xml.template"

OUTPUT_XML="/clickhouse-config/processor_directory.xml"

envsubst < "$INPUT_XML" > "$OUTPUT_XML"
