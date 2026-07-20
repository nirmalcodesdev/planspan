import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "planspan"))

from parser import parse

GOLDEN = Path(__file__).parent / "golden"


def load(name):
    return json.loads((GOLDEN / name).read_text())


def test_search_agg_tree_shape():
    plan = parse(load("search_agg.json"))
    root = plan.root
    assert root.node_type == "Aggregate"
    assert root.children[0].node_type == "Gather"
    seq = root.children[0].children[0].children[0]
    assert seq.node_type == "Seq Scan"
    assert seq.relation == "orders"
    assert seq.parallel_aware


def test_per_loop_math():
    plan = parse(load("search_agg.json"))
    seq = plan.root.children[0].children[0].children[0]
    # 3 loops x 685.51ms per loop
    assert seq.loops == 3
    assert abs(seq.total_ms - 685.51 * 3) < 0.01
    # rows also scale by loops: 166667 x 3
    assert seq.actual_rows == 166667 * 3


def test_skew_ratio():
    plan = parse(load("search_agg.json"))
    seq = plan.root.children[0].children[0].children[0]
    assert abs(seq.skew_ratio - (166667 * 3) / 208333) < 0.01


def test_traceparent_extracted():
    plan = parse(load("search_agg.json"))
    assert plan.traceparent == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_query_id():
    plan = parse(load("search_agg.json"))
    assert plan.query_id == 5736429471893401520


def test_index_scan_attrs():
    plan = parse(load("orders_index_scan.json"))
    idx = plan.root.children[0].children[0]
    assert idx.node_type == "Index Scan"
    assert idx.index_name == "ix_orders_email"
    assert idx.relation == "orders"
    assert idx.buffers_read == 34


def test_self_time_excludes_children():
    plan = parse(load("orders_index_scan.json"))
    sort = plan.root.children[0]
    idx = sort.children[0]
    assert abs(sort.self_ms - (sort.total_ms - idx.total_ms)) < 0.001
    assert sort.self_ms >= 0


def test_no_traceparent_is_none():
    entry = load("search_agg.json")
    entry["Query Text"] = "SELECT 1"
    plan = parse(entry)
    assert plan.traceparent is None


def test_duration_falls_back_to_root_total():
    # real auto_explain entries have no Duration key — it lives in the log
    # line prefix. parser should fall back to the root node's total.
    entry = load("search_agg.json")
    del entry["Duration"]
    plan = parse(entry)
    assert plan.duration_ms == plan.root.total_ms
    assert plan.duration_ms > 0


def test_real_vps_plan():
    # captured from the VPS auto_explain log — full PG17 shape with per-worker
    # arrays and no Duration key. guards against format drift.
    plan = parse(load("search_real_vps.json"))
    assert plan.root.node_type == "Aggregate"
    assert plan.traceparent == "00-d9346a69a669b8421cc2b24943c7b74e-59b516c25dc188af-01"
    assert plan.query_id == 4812255339592286563
    # walk down to the parallel seq scan
    seq = plan.root.children[0].children[0].children[0].children[0]
    assert seq.node_type == "Seq Scan"
    assert seq.relation == "orders"
    assert seq.parallel_aware
    assert seq.loops == 3
    # duration falls back to root total (no Duration key in real logs)
    assert plan.duration_ms == plan.root.total_ms
    assert plan.duration_ms > 0
