#!/bin/sh
# Healthcheck untuk mode serve. /api/* dilindungi Bearer token bila
# BQ_SERVE_TOKEN di-set → tanpa header, healthcheck selalu 401 (unhealthy).
set -eu
url="http://127.0.0.1:${BQ_SERVE_PORT:-8888}/api/health"
if [ -n "${BQ_SERVE_TOKEN:-}" ]; then
    exec curl -fsS -o /dev/null --max-time 4 -H "Authorization: Bearer ${BQ_SERVE_TOKEN}" "$url"
fi
exec curl -fsS -o /dev/null --max-time 4 "$url"
