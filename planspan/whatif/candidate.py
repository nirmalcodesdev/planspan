"""Suggest an index from a slow plan.

Heuristic, deliberately narrow: a Seq Scan on a real table with a filter on a
column is the classic missing-index smell. Pull the table and the filtered
column(s) out and propose a btree. hypopg then tells us if it would actually help.
"""
import re
from dataclasses import dataclass

from parser import ParsedPlan, PlanNode

# pull the column out of filter clauses like:
#   "((orders.email)::text = 'x'::text)"   -> email
#   "(email = 5)"                            -> email
#   "(orders.total > 10)"                    -> total
# match an optional "table." qualifier then the column, before a comparison op
_COL_RE = re.compile(
    r"\(?([a-z_][a-z0-9_]*)\.?([a-z0-9_]*)\)?(?:::[a-z ]+)?\s*(?:=|>|<|>=|<=|~~|<>)"
)


@dataclass
class IndexCandidate:
    relation: str
    columns: tuple[str, ...]
    seq_scan_ms: float
    rows_scanned: float
    filter_clause: str | None = None

    @property
    def ddl(self) -> str:
        cols = ", ".join(self.columns)
        idx = f"{self.relation}_{'_'.join(self.columns)}_idx"
        return f"CREATE INDEX CONCURRENTLY {idx} ON {self.relation} ({cols});"

    @property
    def explain_index(self) -> str:
        # form hypopg understands (no CONCURRENTLY, no name)
        cols = ", ".join(self.columns)
        return f"CREATE INDEX ON {self.relation} ({cols})"

    @property
    def probe_query(self) -> str:
        """A self-contained query to EXPLAIN. The app's own query uses bind
        params ($1) we can't re-plan, but the Seq Scan filter carries real
        literals, so we probe with it directly."""
        where = self.filter_clause or "true"
        return f"SELECT * FROM {self.relation} WHERE {where}"


def _columns_from_filter(filter_clause: str | None) -> tuple[str, ...]:
    if not filter_clause:
        return ()
    cols = []
    for m in _COL_RE.finditer(filter_clause):
        # group2 is the column when qualified (table.column); else group1
        col = m.group(2) or m.group(1)
        if col and col not in cols:
            cols.append(col)
    return tuple(cols)


def _walk(node: PlanNode):
    yield node
    for c in node.children:
        yield from _walk(c)


def find_candidate(plan: ParsedPlan, min_ms: float = 100.0) -> IndexCandidate | None:
    """Return the most expensive seq-scan-with-filter candidate, or None."""
    best = None
    for node in _walk(plan.root):
        if node.node_type != "Seq Scan" or not node.relation:
            continue
        if node.total_ms < min_ms:
            continue
        cols = _columns_from_filter(node.filter_clause)
        if not cols:
            continue
        if best is None or node.total_ms > best.seq_scan_ms:
            best = IndexCandidate(
                relation=node.relation,
                columns=cols,
                seq_scan_ms=node.total_ms,
                rows_scanned=node.actual_rows,
                filter_clause=node.filter_clause,
            )
    return best
