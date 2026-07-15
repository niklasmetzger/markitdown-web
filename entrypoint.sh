#!/bin/sh
# Entrypoint: ensure /app/data is writable by the app user, then drop privileges
# and exec the CMD. Host-mounted volumes often have root ownership which would
# otherwise block writes from the unprivileged container user.
set -e

mkdir -p /app/data
# Best-effort: chown may fail if the volume is read-only at the filesystem level
# (e.g. some NFS/CIFS exports). In that case the user can either fix the host
# perms or run the container as root.
chown -R app:app /app/data 2>/dev/null || {
    echo "[entrypoint] WARNING: could not chown /app/data — container must run as a user that can write here" >&2
}

# Hand off to CMD as the unprivileged `app` user
exec gosu app "$@"
