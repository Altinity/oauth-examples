FROM altinityinfra/clickhouse-server:1078-25.8.9.20504.altinityantalya

# Replace a file inside the image
COPY clickhouse /usr/bin/clickhouse
COPY clickhouse /usr/share/clickhouse
RUN chmod +x /usr/bin/clickhouse
RUN chmod +x /usr/share/clickhouse

