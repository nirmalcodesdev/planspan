"""Shared helpers for reading the traceparent SQL comment.

The demo app injects `/*traceparent='00-<trace>-<span>-<flags>'*/` as a trailing
comment. It survives into both the auto_explain log and pg_stat_activity.query,
so the parser and the lock poller both need to pull it back out.
"""
import re

_TRACEPARENT_RE = re.compile(
    r"/\*traceparent='([0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2})'\*/"
)


def extract_traceparent(sql: str | None) -> str | None:
    if not sql:
        return None
    m = _TRACEPARENT_RE.search(sql)
    return m.group(1) if m else None


def parse_traceparent(traceparent: str | None) -> tuple[int, int, int] | None:
    """Return (trace_id, span_id, flags) as ints, or None if malformed."""
    if not traceparent:
        return None
    try:
        _, trace_id, span_id, flags = traceparent.split("-")
        return int(trace_id, 16), int(span_id, 16), int(flags, 16)
    except (ValueError, AttributeError):
        return None
