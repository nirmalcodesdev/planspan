import sys
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).parent.parent / "planspan"))

from billing import bill, io_amplification, dollars_per_month
from whatif import find_candidate, WhatIfEmitter
from whatif.run import WhatIf
from whatif.candidate import IndexCandidate
from parser import PlanNode
from parser.ir import ParsedPlan


# ---- billing ----

def test_io_amplification():
    # 250000 pages * 8KB / 12 rows = big number
    amp = io_amplification(buffers_read=250000, rows_returned=12)
    assert amp == 250000 * 8192 / 12


def test_io_amplification_floors_rows_at_one():
    assert io_amplification(100, 0) == 100 * 8192


def test_dollars_per_month_scales():
    d1 = dollars_per_month(total_ms=1000, calls_per_hour=100)
    d2 = dollars_per_month(total_ms=1000, calls_per_hour=200)
    assert abs(d2 - 2 * d1) < 1e-6


def test_bill_bundles():
    b = bill(buffers_read=1000, rows_returned=10, total_ms=500, calls_per_hour=60)
    assert b.bytes_read == 1000 * 8192
    assert b.rows_returned == 10
    assert b.dollars_per_month > 0


# ---- candidate heuristic ----

def _seq_scan_plan(filter_clause):
    seq = PlanNode(node_type="Seq Scan", total_ms=700, relation="orders",
                   actual_rows=10_000_000, filter_clause=filter_clause)
    return ParsedPlan(query_text="...", duration_ms=700, root=seq)


def test_candidate_from_filter():
    plan = _seq_scan_plan("(email = 'x@y.com'::text)")
    c = find_candidate(plan)
    assert c is not None
    assert c.relation == "orders"
    assert c.columns == ("email",)
    assert "CREATE INDEX CONCURRENTLY" in c.ddl
    assert "orders (email)" in c.ddl


def test_candidate_from_real_qualified_filter():
    # the shape real auto_explain emits: table-qualified, cast-wrapped
    plan = _seq_scan_plan("((orders.email)::text = 'user9000@example.com'::text)")
    c = find_candidate(plan)
    assert c is not None
    assert c.columns == ("email",)
    assert "orders (email)" in c.ddl


def test_candidate_none_without_filter():
    plan = _seq_scan_plan(None)
    assert find_candidate(plan) is None


def test_candidate_skips_fast_scans():
    seq = PlanNode(node_type="Seq Scan", total_ms=5, relation="orders",
                   filter_clause="(email = 'x')")
    plan = ParsedPlan(query_text="...", duration_ms=5, root=seq)
    assert find_candidate(plan) is None


# ---- what-if span emission ----

FAKE_HYPO = {
    "Plan": {
        "Node Type": "Index Scan",
        "Relation Name": "orders",
        "Index Name": "<12345>btree_orders_email",
        "Total Cost": 8.5,
        "Plan Rows": 100,
        "Plans": [],
    }
}

TRACEPARENT = "00-abcdef00000000000000000000000000-1111111111111111-01"


def build_emitter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return WhatIfEmitter(provider.get_tracer("test")), exporter


def test_whatif_emits_simulated_sibling():
    cand = IndexCandidate(relation="orders", columns=("email",), seq_scan_ms=700, rows_scanned=1e7)
    wi = WhatIf(candidate=cand, baseline_cost=442.0, hypo_cost=8.5,
                hypo_plan=FAKE_HYPO, used_hypo_index=True)
    emitter, exporter = build_emitter()
    n = emitter.emit(wi, TRACEPARENT, real_duration_ms=800, start_ns=1_000_000_000_000)
    assert n == 1
    span = exporter.get_finished_spans()[0]
    assert span.attributes["db.postgresql.plan.simulated"] is True
    assert span.attributes["whatif.speedup"] == round(442.0 / 8.5, 1)
    assert "CREATE INDEX CONCURRENTLY" in span.attributes["whatif.ddl"]
    # parented into the request's trace
    assert span.context.trace_id == int("abcdef00000000000000000000000000", 16)


def test_whatif_no_parent_without_traceparent():
    cand = IndexCandidate(relation="orders", columns=("email",), seq_scan_ms=700, rows_scanned=1e7)
    wi = WhatIf(candidate=cand, baseline_cost=442.0, hypo_cost=8.5,
                hypo_plan=FAKE_HYPO, used_hypo_index=True)
    emitter, exporter = build_emitter()
    n = emitter.emit(wi, None, real_duration_ms=800, start_ns=1_000_000_000_000)
    assert n == 0
