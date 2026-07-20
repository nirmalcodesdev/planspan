"""auto_explain JSON -> ParsedPlan.

Input is the dict auto_explain logs (the object holding "Query Text" and "Plan").
Pure function; the sidecar owns log tailing and line assembly.
"""
import re

from .ir import ParsedPlan, PlanNode

_TRACEPARENT_RE = re.compile(r"/\*traceparent='([0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2})'\*/")


def _node(raw: dict) -> PlanNode:
    loops = int(raw.get("Actual Loops", 1)) or 1
    # auto_explain reports per-loop averages; scale to totals
    total_ms = float(raw.get("Actual Total Time", 0.0)) * loops
    actual_rows = float(raw.get("Actual Rows", 0.0)) * loops
    est_rows = float(raw.get("Plan Rows", 0.0))

    node = PlanNode(
        node_type=raw.get("Node Type", "Unknown"),
        total_ms=total_ms,
        loops=loops,
        est_rows=est_rows,
        actual_rows=actual_rows,
        skew_ratio=max(actual_rows, 1.0) / max(est_rows, 1.0),
        buffers_hit=int(raw.get("Shared Hit Blocks", 0)),
        buffers_read=int(raw.get("Shared Read Blocks", 0)),
        relation=raw.get("Relation Name"),
        index_name=raw.get("Index Name"),
        filter_clause=raw.get("Filter"),
        join_type=raw.get("Join Type"),
        parallel_aware=bool(raw.get("Parallel Aware", False)),
    )
    for child in raw.get("Plans", []):
        node.children.append(_node(child))

    child_total = sum(c.total_ms for c in node.children)
    node.self_ms = max(total_ms - child_total, 0.0)
    return node


def parse(entry: dict, log_time: float | None = None) -> ParsedPlan:
    query = entry.get("Query Text", "")
    m = _TRACEPARENT_RE.search(query)
    root = _node(entry["Plan"])

    # real auto_explain puts duration in the log-line prefix, not the JSON;
    # the sidecar passes it via entry["Duration"] when it has it
    duration = float(entry.get("Duration", 0.0)) or root.total_ms

    return ParsedPlan(
        query_text=query.strip(),
        duration_ms=duration,
        root=root,
        traceparent=m.group(1) if m else None,
        query_id=entry.get("Query Identifier"),
        log_time=log_time,
    )
