#!/usr/bin/env bash
# PlanSpan compatibility check: probe what the target Postgres allows before you
# commit to a deploy. Read-only — creates nothing, changes nothing.
#
# Managed Postgres varies by provider, engine version, and admin policy, so the
# honest answer to "will PlanSpan work here?" is: run this and see.
#
#   PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD from the environment, or:
#   PGPASSWORD=... ./compat_check.sh "host=... dbname=... user=..."
set -uo pipefail

CONN="${1:-}"
psql_q() { psql ${CONN:+"$CONN"} -tAqc "$1" 2>/dev/null; }

ok()   { printf '  [ ok ]  %s\n' "$1"; }
warn() { printf '  [warn]  %s\n' "$1"; }
bad()  { printf '  [ NO ]  %s\n' "$1"; }

echo "PlanSpan compatibility check"
echo "============================"

# 0. can we even connect?
if ! psql_q "SELECT 1" >/dev/null; then
  bad "cannot connect — set PGHOST/PGUSER/PGPASSWORD or pass a conn string"
  exit 1
fi
ver=$(psql_q "SHOW server_version")
ok "connected — Postgres $ver"

echo
echo "-- required: auto_explain (plan capture) --"
# auto_explain is a preload-only contrib module — it has no CREATE EXTENSION and
# never shows in pg_available_extensions. The real signal is whether it's loaded.
spl=$(psql_q "SHOW shared_preload_libraries")
if echo "$spl" | grep -q auto_explain; then
  ok "auto_explain preloaded (plan logging active)"
elif psql_q "LOAD 'auto_explain'" >/dev/null 2>&1; then
  warn "auto_explain loadable but not preloaded — add to shared_preload_libraries + restart"
else
  bad "auto_explain not loaded and not loadable — this is the core dependency"
  warn "  managed providers may expose it as a parameter-group setting, or not at all"
fi

echo
echo "-- required: pg_stat_activity + pg_blocking_pids (lock forensics) --"
if psql_q "SELECT 1 FROM pg_proc WHERE proname='pg_blocking_pids'" | grep -q 1; then
  ok "pg_blocking_pids() present (built-in)"
else
  bad "pg_blocking_pids() missing (unexpected on modern Postgres)"
fi
if psql_q "SELECT count(*) FROM pg_stat_activity" >/dev/null; then
  ok "pg_stat_activity readable"
else
  warn "pg_stat_activity limited — you may only see your own sessions (need pg_monitor)"
fi

echo
echo "-- recommended: pg_stat_statements (query billing) --"
if psql_q "SELECT 1 FROM pg_available_extensions WHERE name='pg_stat_statements'" | grep -q 1; then
  ok "pg_stat_statements available"
  if psql_q "SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements'" | grep -q 1; then
    ok "pg_stat_statements enabled"
  else
    warn "pg_stat_statements available but not enabled (CREATE EXTENSION + preload)"
  fi
else
  warn "pg_stat_statements unavailable — billing act degrades (no call-rate pricing)"
fi

echo
echo "-- optional: hypopg (what-if plans) --"
if psql_q "SELECT 1 FROM pg_available_extensions WHERE name='hypopg'" | grep -q 1; then
  ok "hypopg available"
else
  warn "hypopg unavailable — what-if act is skipped (it is a third-party extension,"
  warn "  not core/contrib; availability on managed Postgres varies by provider)"
fi

echo
echo "Summary: [ ok ] fine · [warn] degraded feature · [ NO ] blocks core capability."
echo "auto_explain is the only hard requirement; everything else degrades gracefully."
