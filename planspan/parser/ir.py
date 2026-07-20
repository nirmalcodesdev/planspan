"""Intermediate representation for a parsed auto_explain plan.

Pure data — no OTel, no I/O. The emitter turns this into spans.
"""
from dataclasses import dataclass, field


@dataclass
class PlanNode:
    node_type: str
    # inclusive wall time for this node across all loops, ms
    total_ms: float
    # exclusive time (total minus children), computed after the tree is built
    self_ms: float = 0.0
    loops: int = 1
    est_rows: float = 0.0
    actual_rows: float = 0.0
    skew_ratio: float = 1.0
    # 8KB pages
    buffers_hit: int = 0
    buffers_read: int = 0
    relation: str | None = None
    index_name: str | None = None
    filter_clause: str | None = None
    join_type: str | None = None
    parallel_aware: bool = False
    children: list["PlanNode"] = field(default_factory=list)


@dataclass
class ParsedPlan:
    query_text: str
    duration_ms: float
    root: PlanNode
    traceparent: str | None = None
    query_id: int | None = None
    # log line timestamp, epoch seconds; emitter backdates spans from this
    log_time: float | None = None
