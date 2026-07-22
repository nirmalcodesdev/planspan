"""Emit a lock-wait span into the victim's trace, pointing at the culprit.

The span hangs under the victim's request (parent = victim traceparent) and
carries db.blocked_by.trace_id so a click takes you to the blocking request's
trace — the other user who held the lock.
"""
from opentelemetry import trace
from opentelemetry.trace import Link, SpanContext, TraceFlags

from emitter import parent_context_from_traceparent
from scrub import scrub
from traceparent import parse_traceparent

from .poll import LockEpisode

_NS_PER_S = 1_000_000_000


class LockEmitter:
    def __init__(self, tracer=None):
        self._tracer = tracer or trace.get_tracer("planspan.lock")

    def emit(self, ep: LockEpisode) -> bool:
        """Emit one span for a resolved lock episode. Returns True if emitted."""
        block = ep.block
        parent = parent_context_from_traceparent(block.victim_traceparent)
        if parent is None:
            # victim wasn't an app request (no traceparent) — nothing to hang under
            return False

        links = []
        blocker = parse_traceparent(block.blocker_traceparent)
        blocker_trace_hex = None
        if blocker:
            b_trace, b_span, b_flags = blocker
            blocker_trace_hex = f"{b_trace:032x}"
            links.append(
                Link(
                    SpanContext(
                        trace_id=b_trace,
                        span_id=b_span,
                        is_remote=True,
                        trace_flags=TraceFlags(b_flags),
                    )
                )
            )

        start_ns = int(ep.first_seen * _NS_PER_S)
        end_ns = int(ep.last_seen * _NS_PER_S)

        attrs = {
            "db.lock.wait_event": block.wait_event,
            "db.lock.wait_ms": round(ep.duration_s * 1000, 1),
            "db.blocked_by.pid": block.blocker_pid,
            "db.victim.pid": block.victim_pid,
        }
        if blocker_trace_hex:
            attrs["db.blocked_by.trace_id"] = blocker_trace_hex
        if block.blocker_query:
            attrs["db.blocked_by.query"] = scrub(block.blocker_query[:500])

        span = self._tracer.start_span(
            name=f"Lock wait ({block.wait_event})",
            context=parent,
            start_time=start_ns,
            links=links,
            attributes=attrs,
        )
        span.end(end_time=end_ns)
        return True
