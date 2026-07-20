import json
import sys
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).parent.parent / "planspan"))

from emitter import PlanEmitter
from parser import parse

GOLDEN = Path(__file__).parent / "golden"


def load(name):
    return json.loads((GOLDEN / name).read_text())


def build_emitter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return PlanEmitter(tracer), exporter


def test_emits_one_span_per_node():
    plan = parse(load("orders_index_scan.json"))
    emitter, exporter = build_emitter()
    n = emitter.emit(plan, now_ns=1_000_000_000_000)
    spans = exporter.get_finished_spans()
    # Limit -> Sort -> Index Scan = 3 nodes
    assert n == 3
    assert len(spans) == 3


def test_spans_parented_under_traceparent_trace():
    plan = parse(load("orders_index_scan.json"))
    emitter, exporter = build_emitter()
    emitter.emit(plan, now_ns=1_000_000_000_000)
    spans = exporter.get_finished_spans()
    # traceparent trace id from the fixture comment
    expected_trace_id = int("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", 16)
    for s in spans:
        assert s.context.trace_id == expected_trace_id


def test_backdated_start():
    plan = parse(load("orders_index_scan.json"))
    plan.log_time = 2000.0  # epoch seconds
    emitter, exporter = build_emitter()
    emitter.emit(plan, now_ns=0)
    spans = {s.name: s for s in exporter.get_finished_spans()}
    root = spans["Limit"]
    end_ns = 2000 * 1_000_000_000
    # start is backdated by the log-prefix duration (incl planning time)
    start_ns = end_ns - int(plan.duration_ms * 1_000_000)
    assert root.start_time == start_ns
    # root span covers its own executor total; planning overhead means it can
    # end just shy of log_time
    assert root.end_time == start_ns + int(plan.root.total_ms * 1_000_000)


def test_plan_attributes_present():
    plan = parse(load("orders_index_scan.json"))
    emitter, exporter = build_emitter()
    emitter.emit(plan, now_ns=1_000_000_000_000)
    idx = next(
        s for s in exporter.get_finished_spans()
        if s.attributes.get("db.postgresql.plan.node_type") == "Index Scan"
    )
    assert idx.attributes["db.postgresql.plan.index_name"] == "ix_orders_email"
    assert idx.attributes["db.postgresql.plan.relation"] == "orders"
    assert idx.name == "Index Scan orders"


def test_no_traceparent_makes_new_root_trace():
    entry = load("orders_index_scan.json")
    entry["Query Text"] = "SELECT 1"  # strip traceparent
    plan = parse(entry)
    emitter, exporter = build_emitter()
    emitter.emit(plan, now_ns=1_000_000_000_000)
    spans = exporter.get_finished_spans()
    # all share one trace id, but it's freshly generated (not the fixture's)
    trace_ids = {s.context.trace_id for s in spans}
    assert len(trace_ids) == 1
    assert int("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", 16) not in trace_ids


def test_real_vps_plan_emits():
    plan = parse(load("search_real_vps.json"))
    emitter, exporter = build_emitter()
    n = emitter.emit(plan, now_ns=1_000_000_000_000)
    assert n == 5  # Aggregate/GatherMerge/Sort/PartialAgg/SeqScan
    seq = next(
        s for s in exporter.get_finished_spans()
        if s.attributes.get("db.postgresql.plan.node_type") == "Seq Scan"
    )
    assert seq.attributes["db.postgresql.plan.parallel_aware"] is True
