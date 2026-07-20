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

_NS_PER_MS = 1_000_000


def _parent_context(traceparent: str | None):
    if not traceparent:
        return None
    try:
        _, trace_id, span_id, flags = traceparent.split("-")
        ctx = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(int(flags, 16)),
        )
    except (ValueError, AttributeError):
        return None
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

    def emit(self, plan: ParsedPlan, now_ns: int) -> int:
        """Emit the plan tree as spans. Returns number of spans emitted.

        now_ns is the wall clock used when the log has no timestamp; normally
        the sidecar passes the log line's epoch-ns so spans backdate correctly.
        """
        start_ns = self._start_ns(plan, now_ns)
        parent = _parent_context(plan.traceparent)
        count = self._emit_node(plan.root, start_ns, parent)
        return count

    def _start_ns(self, plan: ParsedPlan, now_ns: int) -> int:
        if plan.log_time is not None:
            end_ns = int(plan.log_time * 1_000_000_000)
        else:
            end_ns = now_ns
        return end_ns - int(plan.duration_ms * _NS_PER_MS)

    def _emit_node(self, node: PlanNode, start_ns: int, parent_ctx) -> int:
        end_ns = start_ns + int(node.total_ms * _NS_PER_MS)
        span = self._tracer.start_span(
            name=self._span_name(node),
            context=parent_ctx,
            start_time=start_ns,
            attributes=_node_attrs(node),
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
