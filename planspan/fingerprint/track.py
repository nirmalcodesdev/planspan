"""Track plan fingerprints per queryid and flag when one flips.

Keeps the last good fingerprint (and the trace that produced it) so a regression
can point one click back to the before.
"""
from dataclasses import dataclass

from parser import ParsedPlan

from .hash import fingerprint, shape_string


@dataclass
class PlanFlip:
    query_id: int
    old_fingerprint: str
    new_fingerprint: str
    old_shape: str
    new_shape: str
    last_good_trace_id: str | None
    diff: str


@dataclass
class _Seen:
    fingerprint: str
    shape: str
    trace_id: str | None


def _diff(old_shape: str, new_shape: str) -> str:
    """Human-readable one-liner. Picks out the node types that changed so the
    alert body reads like 'Index Scan -> Seq Scan'."""
    old_nodes = _node_set(old_shape)
    new_nodes = _node_set(new_shape)
    gone = old_nodes - new_nodes
    added = new_nodes - old_nodes
    if gone and added:
        return f"{', '.join(sorted(gone))} -> {', '.join(sorted(added))}"
    if added:
        return f"added {', '.join(sorted(added))}"
    if gone:
        return f"removed {', '.join(sorted(gone))}"
    return "plan shape changed"


def _node_set(shape: str) -> set[str]:
    # split the shape string back into node labels (drop punctuation)
    import re

    return set(re.findall(r"[A-Z][a-zA-Z ]*(?:\[[^\]]*\])?", shape))


class FlipTracker:
    def __init__(self):
        self._seen: dict[int, _Seen] = {}

    def observe(self, plan: ParsedPlan) -> PlanFlip | None:
        """Record this plan. Returns a PlanFlip if the shape changed vs the last
        time we saw this queryid, else None."""
        qid = plan.query_id
        if qid is None:
            return None

        fp = fingerprint(plan)
        shape = shape_string(plan)
        prev = self._seen.get(qid)
        self._seen[qid] = _Seen(fp, shape, _trace_id(plan))

        if prev is None or prev.fingerprint == fp:
            return None

        return PlanFlip(
            query_id=qid,
            old_fingerprint=prev.fingerprint,
            new_fingerprint=fp,
            old_shape=prev.shape,
            new_shape=shape,
            last_good_trace_id=prev.trace_id,
            diff=_diff(prev.shape, shape),
        )

    def last_good_trace(self, query_id: int | None) -> str | None:
        if query_id is None:
            return None
        seen = self._seen.get(query_id)
        return seen.trace_id if seen else None


def _trace_id(plan: ParsedPlan) -> str | None:
    if not plan.traceparent:
        return None
    parts = plan.traceparent.split("-")
    return parts[1] if len(parts) == 4 else None
