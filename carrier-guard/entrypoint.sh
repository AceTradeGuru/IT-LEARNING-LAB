#!/bin/sh
# CarrierGuard container entrypoint.
#
# Migrate, then serve. Migration is guarded by a Postgres advisory lock
# (see db.migrate), so running this on every container start is safe even
# when a rolling deploy brings several up at once.
#
# `set -e` matters here: without it, a failed migration would be followed by
# gunicorn cheerfully serving traffic against a schema that doesn't match the
# code. Better to fail the deploy.
set -e

echo "Running migrations..."
python db.py

echo "Starting gunicorn on :${PORT:-8000}"
exec gunicorn app:app \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-3}" \
    --timeout 30 \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile - \
    --forwarded-allow-ips '*'
