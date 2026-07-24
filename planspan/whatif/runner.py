"""Sidecar-side glue: given a freshly parsed slow plan, try a what-if and emit
the simulated sibling subtree. Holds its own PG connection for EXPLAIN calls.
"""
import os

import psycopg

from .candidate import find_candidate
from .emit import WhatIfEmitter
from .run import run_whatif


def _dsn() -> str:
    from urllib.parse import quote_plus

    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    db = os.environ.get("PG_DB", "shop")
    user = quote_plus(os.environ.get("PG_USER", "planspan"))
    pw = quote_plus(os.environ.get("PG_PASSWORD", "changeme"))
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


class WhatIfRunner:
    def __init__(self, tracer=None):
        self._emitter = WhatIfEmitter(tracer)
        self._conn = None

    def _connection(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(_dsn(), connect_timeout=5, autocommit=True)
        return self._conn

    def maybe_emit(self, plan, start_ns: int) -> int:
        """Returns number of simulated spans emitted (0 if no candidate/no win)."""
        if not plan.traceparent:
            return 0
        candidate = find_candidate(plan)
        if candidate is None:
            return 0
        # the app query uses bind params we can't re-plan; probe with the
        # candidate's own filter (which carries real literals) instead
        query = candidate.probe_query
        try:
            whatif = run_whatif(self._connection(), query, candidate)
        except Exception as e:  # noqa: BLE001
            print(f"whatif error: {e}", flush=True)
            return 0
        if whatif is None:
            return 0
        n = self._emitter.emit(whatif, plan.traceparent, plan.duration_ms, start_ns)
        if n:
            print(
                f"what-if: {candidate.relation}({','.join(candidate.columns)}) "
                f"est_cost_reduction={whatif.est_cost_reduction:.1f}x (planner estimate)",
                flush=True,
            )
        return n
