"""Run a what-if: how would this query plan if the candidate index existed?

Uses hypopg to create a hypothetical (in-memory, never built) index, then
EXPLAIN (no ANALYZE — the query is not executed) to see the plan the planner
would pick. The ratio is a PLANNER COST estimate, not a measured wall-clock
speedup — named and labeled as such everywhere it surfaces.
"""
import json
from dataclasses import dataclass

from .candidate import IndexCandidate


@dataclass
class WhatIf:
    candidate: IndexCandidate
    baseline_cost: float
    hypo_cost: float
    hypo_plan: dict          # raw EXPLAIN json Plan node
    used_hypo_index: bool

    @property
    def est_cost_reduction(self) -> float:
        """baseline planner cost / hypothetical planner cost. An estimate the
        planner makes, NOT a measured latency improvement."""
        return self.baseline_cost / max(self.hypo_cost, 0.01)


def _total_cost(plan_json: dict) -> float:
    return float(plan_json["Plan"]["Total Cost"])


def run_whatif(conn, query: str, candidate: IndexCandidate) -> WhatIf | None:
    """conn: a psycopg connection. query: the SQL to test (comments stripped is
    fine). Returns None if the planner wouldn't use the hypothetical index."""
    with conn.cursor() as cur:
        # baseline plan
        cur.execute(f"EXPLAIN (FORMAT JSON) {query}")
        baseline = cur.fetchone()[0]
        baseline = baseline[0] if isinstance(baseline, list) else baseline
        baseline_cost = _total_cost(baseline)

        # create hypothetical index, re-plan
        cur.execute("SELECT hypopg_reset()")
        cur.execute(
            "SELECT indexname FROM hypopg_create_index(%s)",
            (candidate.explain_index,),
        )
        hypo_index_name = cur.fetchone()[0]

        cur.execute(f"EXPLAIN (FORMAT JSON) {query}")
        hypo = cur.fetchone()[0]
        hypo = hypo[0] if isinstance(hypo, list) else hypo
        hypo_cost = _total_cost(hypo)

        cur.execute("SELECT hypopg_reset()")

    # hypopg names its indexes like "<13630>btree_orders_email"; when the
    # planner picks it, that name shows up in the plan's Index Name fields
    used = hypo_index_name in json.dumps(hypo)
    if not used or hypo_cost >= baseline_cost:
        return None

    return WhatIf(
        candidate=candidate,
        baseline_cost=baseline_cost,
        hypo_cost=hypo_cost,
        hypo_plan=hypo,
        used_hypo_index=True,
    )
