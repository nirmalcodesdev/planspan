"""Turn a ParsedPlan into OTel spans, parented under the request that ran it.

Layout follows idea.md: the waterfall is a cost-map, not a true timeline.
Postgres's iterator model interleaves node execution and EXPLAIN gives inclusive
durations, not start offsets. So:
  - parent span start == child span start (all backdated to execution time)
  - each node's span duration == its inclusive total_ms
  - "widest bar == most expensive node" holds; stated as a design decision

The plan subtree is stitched under the live app trace by reconstructing a parent
SpanContext from the traceparent comment we injected in the demo app.
"""
from opentelemetry import trace
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    set_span_in_context,
)

from parser import ParsedPlan, PlanNode
from traceparent import parse_traceparent

_NS_PER_MS = 1_000_000


def parent_context_from_traceparent(traceparent: str | None):
    """Build an OTel context whose current span is the (remote) traceparent span.

    Used to hang emitted spans under a trace we only know by its id string —
    the plan subtree under the app request, or a lock-wait span under the victim.
    """
    parsed = parse_traceparent(traceparent)
    if not parsed:
        return None
    trace_id, span_id, flags = parsed
    ctx = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(flags),
    )
    return set_span_in_context(NonRecordingSpan(ctx))


def _node_attrs(node: PlanNode) -> dict:
    attrs = {
        "db.postgresql.plan.node_type": node.node_type,
        "db.postgresql.plan.total_ms": round(node.total_ms, 3),
        "db.postgresql.plan.self_ms": round(node.self_ms, 3),
        "db.postgresql.plan.loops": node.loops,
        "db.postgresql.plan.rows_estimated": node.est_rows,
        "db.postgresql.plan.rows_actual": node.actual_rows,
        "db.postgresql.plan.skew_ratio": round(node.skew_ratio, 2),
        "db.postgresql.plan.buffers_hit": node.buffers_hit,
        "db.postgresql.plan.buffers_read": node.buffers_read,
        "db.postgresql.plan.parallel_aware": node.parallel_aware,
    }
    if node.relation:
        attrs["db.postgresql.plan.relation"] = node.relation
    if node.index_name:
        attrs["db.postgresql.plan.index_name"] = node.index_name
    if node.filter_clause:
        attrs["db.postgresql.plan.filter"] = node.filter_clause
    if node.join_type:
        attrs["db.postgresql.plan.join_type"] = node.join_type
    return attrs


class PlanEmitter:
    def __init__(self, tracer=None):
        self._tracer = tracer or trace.get_tracer("planspan")

    def emit(self, plan: ParsedPlan, now_ns: int, root_attrs: dict | None = None) -> int:
        """Emit the plan tree as spans. Returns number of spans emitted.

        now_ns is the wall clock used when the log has no timestamp; normally
        the sidecar passes the log line's epoch-ns so spans backdate correctly.
        root_attrs are merged onto the root span only (fingerprint, last-good).
        """
        start_ns = self._start_ns(plan, now_ns)
        parent = parent_context_from_traceparent(plan.traceparent)
        count = self._emit_node(plan.root, start_ns, parent, root_attrs)
        return count

    def _start_ns(self, plan: ParsedPlan, now_ns: int) -> int:
        if plan.log_time is not None:
            end_ns = int(plan.log_time * 1_000_000_000)
        else:
            end_ns = now_ns
        return end_ns - int(plan.duration_ms * _NS_PER_MS)

    def _emit_node(self, node: PlanNode, start_ns: int, parent_ctx, root_attrs=None) -> int:
        end_ns = start_ns + int(node.total_ms * _NS_PER_MS)
        attrs = _node_attrs(node)
        if root_attrs:
            attrs.update(root_attrs)
        span = self._tracer.start_span(
            name=self._span_name(node),
            context=parent_ctx,
            start_time=start_ns,
            attributes=attrs,
        )
        child_ctx = set_span_in_context(span)
        count = 1
        # cost-map layout: children share the parent's start
        for child in node.children:
            count += self._emit_node(child, start_ns, child_ctx)
        span.end(end_time=end_ns)
        return count

    @staticmethod
    def _span_name(node: PlanNode) -> str:
        if node.relation:
            return f"{node.node_type} {node.relation}"
        return node.node_type
