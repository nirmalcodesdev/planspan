import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "planspan"))

from fingerprint import FlipTracker, fingerprint, shape_string
from parser import PlanNode
from parser.ir import ParsedPlan


def make_plan(query_id, root):
    return ParsedPlan(query_text="q", duration_ms=100, root=root, query_id=query_id,
                      traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01")


def seq_scan_plan(qid=1, trace_a="a"):
    root = PlanNode(node_type="Aggregate", total_ms=100, children=[
        PlanNode(node_type="Seq Scan", total_ms=90, relation="orders"),
    ])
    return ParsedPlan(query_text="q", duration_ms=100, root=root, query_id=qid,
                      traceparent=f"00-{trace_a*32}-{'b'*16}-01")


def index_scan_plan(qid=1, trace_a="c"):
    root = PlanNode(node_type="Aggregate", total_ms=20, children=[
        PlanNode(node_type="Index Scan", total_ms=10, relation="orders",
                 index_name="ix_orders_email"),
    ])
    return ParsedPlan(query_text="q", duration_ms=20, root=root, query_id=qid,
                      traceparent=f"00-{trace_a*32}-{'b'*16}-01")


def test_fingerprint_stable_across_timings():
    # same shape, different timings -> same fingerprint
    p1 = seq_scan_plan()
    p2 = seq_scan_plan()
    p2.root.total_ms = 5000
    p2.root.children[0].total_ms = 4999
    assert fingerprint(p1) == fingerprint(p2)


def test_fingerprint_changes_on_shape_flip():
    assert fingerprint(seq_scan_plan()) != fingerprint(index_scan_plan())


def test_shape_string_includes_index():
    s = shape_string(index_scan_plan())
    assert "Index Scan[ix_orders_email]" in s
    assert "Aggregate" in s


def test_flip_tracker_first_sight_no_flip():
    t = FlipTracker()
    assert t.observe(index_scan_plan()) is None


def test_flip_tracker_detects_regression():
    t = FlipTracker()
    # good plan first (index scan), from trace 'cccc...'
    t.observe(index_scan_plan(qid=42, trace_a="c"))
    # then it flips to seq scan, trace 'aaaa...'
    flip = t.observe(seq_scan_plan(qid=42, trace_a="a"))
    assert flip is not None
    assert flip.query_id == 42
    assert "Index Scan" in flip.diff and "Seq Scan" in flip.diff
    # last good points back at the index-scan trace
    assert flip.last_good_trace_id == "c" * 32


def test_flip_tracker_no_flip_when_stable():
    t = FlipTracker()
    t.observe(seq_scan_plan(qid=7))
    assert t.observe(seq_scan_plan(qid=7)) is None


def test_different_queryids_independent():
    t = FlipTracker()
    t.observe(seq_scan_plan(qid=1))
    # different qid, different shape -> not a flip
    assert t.observe(index_scan_plan(qid=2)) is None
