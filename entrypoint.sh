#!/bin/sh
# Container entrypoint.
#
# Waits for the database, applies migrations exactly once per rollout, then execs the real
# process so signals reach it directly (PID 1 semantics matter for graceful shutdown).
set -eu

: "${DJANGO_SETTINGS_MODULE:=config.settings}"
export DJANGO_SETTINGS_MODULE

wait_for_db() {
  echo "entrypoint: waiting for the database..."
  attempt=0
  until python - <<'PY'
import sys

import django

django.setup()
from django.db import connection

try:
    connection.ensure_connection()
except Exception as exc:  # noqa: BLE001
    print(f"  not ready: {type(exc).__name__}")
    sys.exit(1)
PY
  do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
      echo "entrypoint: database did not become available in time" >&2
      exit 1
    fi
    sleep 2
  done
  echo "entrypoint: database is ready"
}

wait_for_db

# RUN_MIGRATIONS is set only on the one-shot migrate service, so N app replicas starting
# together do not race each other applying the same migration.
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "entrypoint: applying migrations"
  python manage.py migrate --noinput
  if [ "${ENABLE_RLS_ON_MIGRATE:-0}" = "1" ] || [ "${ENABLE_POSTGRES_RLS:-0}" = "1" ]; then
    echo "entrypoint: enabling Postgres row-level security policies"
    python manage.py enable_rls || echo "entrypoint: enable_rls skipped (non-Postgres or already applied)"
  fi
fi

exec "$@"
