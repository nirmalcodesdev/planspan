"""Fingerprint a plan by its shape, not its timings.

Two runs of the same query with the same plan hash to the same fingerprint even
if the rows and milliseconds differ. When the planner flips (Index Scan -> Seq
Scan), the hash changes — that's the signal.
"""
import hashlib

from parser import ParsedPlan, PlanNode


def _shape(node: PlanNode) -> str:
    """A compact, stable string of the node and its subtree.

    Includes node type and the relation/index it touches (so an Index Scan on
    orders_email is distinct from a Seq Scan on orders), excludes all timings,
    row counts, and costs.
    """
    label = node.node_type
    if node.index_name:
        label += f"[{node.index_name}]"
    elif node.relation:
        label += f"[{node.relation}]"
    if node.children:
        inner = ",".join(_shape(c) for c in node.children)
        return f"{label}({inner})"
    return label


def shape_string(plan: ParsedPlan) -> str:
    return _shape(plan.root)


def fingerprint(plan: ParsedPlan) -> str:
    """12-hex-char stable hash of the plan shape."""
    return hashlib.sha1(shape_string(plan).encode()).hexdigest()[:12]
