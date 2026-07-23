"""Sidecar-side glue: turn a parsed plan + its pg_stat_statements row into span
attributes. Holds its own PG connection, same pattern as whatif/runner.py.
"""
import psycopg

from parser import ParsedPlan

from .bill import bill
from .query import _dsn, fetch_call_stats


class BillingRunner:
    def __init__(self, dollars_per_cpu_hour: float = 0.12):
        self._conn = None
        self._rate = dollars_per_cpu_hour

    def _connection(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(_dsn(), connect_timeout=5, autocommit=True)
        return self._conn

    def bill_attrs(self, plan: ParsedPlan) -> dict:
        """Returns billing.* attrs for the plan root, or {} if we can't price it
        (no queryid, or pg_stat_statements hasn't seen a repeat yet)."""
        if plan.query_id is None:
            return {}
        try:
            stats = fetch_call_stats(self._connection(), plan.query_id)
        except Exception as e:  # noqa: BLE001 - keep the sidecar alive
            print(f"billing error: {e}", flush=True)
            return {}
        if stats is None or stats.calls_per_hour <= 0:
            return {}

        b = bill(
            buffers_read=_total_buffers_read(plan),
            rows_returned=max(plan.root.actual_rows, 1.0),
            total_ms=plan.duration_ms,
            calls_per_hour=stats.calls_per_hour,
            dollars_per_cpu_hour=self._rate,
        )
        attrs = {
            "billing.io_amplification_bytes_per_row": round(b.io_amplification, 1),
            "billing.dollars_per_month": round(b.dollars_per_month, 4),
            "billing.calls_per_hour": round(stats.calls_per_hour, 2),
        }
        relation = _primary_relation(plan)
        if relation:
            attrs["billing.relation"] = relation
        return attrs


def _total_buffers_read(plan: ParsedPlan) -> int:
    """Sum shared buffers read across the whole plan tree — the real disk cost
    of the request, not just the root node's."""
    total = 0

    def walk(node):
        nonlocal total
        total += node.buffers_read
        for c in node.children:
            walk(c)

    walk(plan.root)
    return total


def _primary_relation(plan: ParsedPlan) -> str | None:
    """Billing attrs sit on the root span, but the root (Aggregate, Limit, ...)
    rarely has its own relation. Grab the costliest table-touching node's
    relation so the billing dashboard reads as something more than a trace id."""
    best = None
    best_ms = -1.0

    def walk(node):
        nonlocal best, best_ms
        if node.relation and node.total_ms > best_ms:
            best, best_ms = node.relation, node.total_ms
        for c in node.children:
            walk(c)

    walk(plan.root)
    return best
