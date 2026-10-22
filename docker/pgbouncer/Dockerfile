# PgBouncer in transaction mode, standing in for Neon's pooled DATABASE_URL in the local
# Vercel-style stack (docker-compose.vercel.yml). Build context: docker/pgbouncer/.
FROM alpine:3.24.2

# Alpine's package creates no user: the server runs as an unprivileged one made here.
RUN apk add --no-cache "pgbouncer~1.25" \
    && pgbouncer --version \
    && addgroup -S -g 10002 pgbouncer \
    && adduser -S -D -H -u 10002 -G pgbouncer pgbouncer

COPY pgbouncer.ini /etc/pgbouncer/pgbouncer.ini
COPY --chmod=0755 entrypoint.sh /usr/local/bin/pgbouncer-entrypoint

USER pgbouncer
EXPOSE 6432
ENTRYPOINT ["/usr/local/bin/pgbouncer-entrypoint"]
