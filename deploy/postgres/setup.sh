#!/usr/bin/env bash
# One-time PlanSpan setup for a native PostgreSQL 17 cluster (Ubuntu, systemd).
# Run on the VPS as a user with sudo. Idempotent-ish; safe to re-run.
set -euo pipefail

PG_VER=17
CONF_D=/etc/postgresql/${PG_VER}/main/conf.d
APP_DB=${PG_DB:-shop}
APP_USER=${PG_USER:-planspan}
APP_PASS=${PG_PASSWORD:-changeme}

here=$(cd "$(dirname "$0")" && pwd)

echo ">> installing hypopg"
sudo apt-get update -qq
sudo apt-get install -y postgresql-${PG_VER}-hypopg

echo ">> dropping planspan.conf into conf.d"
sudo mkdir -p "$CONF_D"
sudo cp "$here/planspan.conf" "$CONF_D/planspan.conf"

echo ">> restarting postgres (shared_preload_libraries needs a full restart)"
sudo systemctl restart postgresql

echo ">> creating app db + role"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${APP_USER}') THEN
    CREATE ROLE ${APP_USER} LOGIN PASSWORD '${APP_PASS}';
  END IF;
END
\$\$;
SELECT 'CREATE DATABASE ${APP_DB} OWNER ${APP_USER}'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '${APP_DB}')\gexec
SQL

echo ">> extensions + grants in ${APP_DB}"
sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$APP_DB" <<SQL
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS hypopg;
GRANT pg_monitor TO ${APP_USER};
-- pg_monitor alone does NOT grant table reads (learned the hard way); grant explicitly
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ${APP_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ${APP_USER};
SQL

echo ">> verifying auto_explain loaded"
sudo -u postgres psql -tAc "SHOW shared_preload_libraries;"

echo "done. tail plans at: /var/log/postgresql/postgresql-${PG_VER}-main.log"
