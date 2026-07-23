"""Pull call-rate facts for a query from pg_stat_statements.

Each row is scoped per-queryid with its own `stats_since`, so the observed rate
is exact for that query, not a global-reset approximation. A role only sees the
stats for queries it issued, so no extra grants are needed beyond running the
queries themselves (verified against pg_stat_statements on PG17).
"""
import os
from dataclasses import dataclass
from datetime import datetime, timezone

_QUERY = """
SELECT calls, total_exec_time, stats_since
FROM pg_stat_statements
WHERE queryid = %s
"""


@dataclass
class CallStats:
    calls: int
    total_exec_ms: float
    calls_per_hour: float


def calls_per_hour(calls: int, stats_since: datetime, now: datetime) -> float:
    """Pure: mean call rate since pg_stat_statements started tracking this query."""
    elapsed_hours = max((now - stats_since).total_seconds() / 3600.0, 1 / 60)
    return calls / elapsed_hours


def fetch_call_stats(conn, query_id: int, now: datetime | None = None) -> CallStats | None:
    """conn: a psycopg connection. Returns None if the queryid has no stats yet
    (pg_stat_statements not caught up, or the query hasn't repeated)."""
    now = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(_QUERY, (query_id,))
        row = cur.fetchone()
    if row is None:
        return None
    calls, total_exec_ms, stats_since = row
    return CallStats(
        calls=calls,
        total_exec_ms=float(total_exec_ms),
        calls_per_hour=calls_per_hour(calls, stats_since, now),
    )


def _dsn() -> str:
    from urllib.parse import quote_plus

    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    db = os.environ.get("PG_DB", "shop")
    user = quote_plus(os.environ.get("PG_USER", "planspan"))
    pw = quote_plus(os.environ.get("PG_PASSWORD", "changeme"))
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"
