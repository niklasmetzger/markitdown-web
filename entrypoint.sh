#!/bin/sh
# Entrypoint: ensure /app/data exists, then exec the CMD.
# Container runs as root (see Dockerfile note) so chown is best-effort and
# not required for write access.
set -e

mkdir -p /app/data

exec "$@"
