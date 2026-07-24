"""Emit the hypothetical plan as a sibling span subtree under the same request.

The what-if plan is EXPLAIN-only (no ANALYZE) so it has planner costs, not real
timings. We scale the real query's duration by the cost ratio to give the sibling
subtree a plausible width, and mark every span simulated=true so nobody mistakes it
for a measured run.
"""
from opentelemetry import trace

from emitter import parent_context_from_traceparent
from opentelemetry.trace import set_span_in_context

from .run import WhatIf

_NS_PER_MS = 1_000_000


class WhatIfEmitter:
    def __init__(self, tracer=None):
        self._tracer = tracer or trace.get_tracer("planspan.whatif")

    def emit(self, whatif: WhatIf, traceparent: str, real_duration_ms: float, start_ns: int) -> int:
        """Emit the simulated plan tree. Returns span count, 0 if no parent."""
        parent = parent_context_from_traceparent(traceparent)
        if parent is None:
            return 0

        # scale the simulated subtree to the projected (faster) duration
        projected_ms = real_duration_ms * (whatif.hypo_cost / max(whatif.baseline_cost, 0.01))
        root = whatif.hypo_plan["Plan"]
        root_cost = float(root["Total Cost"]) or 1.0

        count = self._emit_node(
            root, start_ns, parent, projected_ms, root_cost, whatif, is_root=True
        )
        return count

    def _emit_node(self, node, start_ns, parent_ctx, projected_ms, root_cost, whatif, is_root):
        node_cost = float(node.get("Total Cost", 0.0))
        dur_ms = projected_ms * (node_cost / root_cost)
        end_ns = start_ns + int(dur_ms * _NS_PER_MS)

        attrs = {
            "db.postgresql.plan.node_type": node.get("Node Type", "Unknown"),
            "db.postgresql.plan.simulated": True,
            "db.postgresql.plan.est_cost": node_cost,
            "db.postgresql.plan.rows_estimated": float(node.get("Plan Rows", 0)),
        }
        if node.get("Relation Name"):
            attrs["db.postgresql.plan.relation"] = node["Relation Name"]
        if node.get("Index Name"):
            attrs["db.postgresql.plan.index_name"] = node["Index Name"]
        if is_root:
            attrs["whatif.est_cost_reduction"] = round(whatif.est_cost_reduction, 1)
            attrs["whatif.ddl"] = whatif.candidate.ddl
            attrs["whatif.baseline_cost"] = whatif.baseline_cost
            attrs["whatif.hypo_cost"] = whatif.hypo_cost

        name = node.get("Node Type", "Unknown")
        if node.get("Relation Name"):
            name = f"{name} {node['Relation Name']}"
        name = f"[what-if] {name}" if is_root else name

        span = self._tracer.start_span(
            name=name,
            context=parent_ctx,
            start_time=start_ns,
            attributes=attrs,
        )
        child_ctx = set_span_in_context(span)
        count = 1
        for child in node.get("Plans", []):
            count += self._emit_node(
                child, start_ns, child_ctx, projected_ms, root_cost, whatif, is_root=False
            )
        span.end(end_time=end_ns)
        return count
