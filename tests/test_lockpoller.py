import sys
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).parent.parent / "planspan"))

from lockpoller import LockEmitter, LockTracker, detect_blocks

VICTIM_TP = "00-11111111111111111111111111111111-2222222222222222-01"
BLOCKER_TP = "00-33333333333333333333333333333333-4444444444444444-01"


def make_row(vpid, bpid, vq, bq):
    return {
        "victim_pid": vpid,
        "blocker_pid": bpid,
        "victim_query": vq,
        "blocker_query": bq,
        "wait_event": "relation",
        "wait_event_type": "Lock",
    }


def test_detect_blocks_extracts_traceparents():
    rows = [make_row(
        100, 200,
        f"UPDATE orders SET status='x' /*traceparent='{VICTIM_TP}'*/",
        f"UPDATE orders SET total=1 /*traceparent='{BLOCKER_TP}'*/",
    )]
    blocks = detect_blocks(rows)
    assert len(blocks) == 1
    b = blocks[0]
    assert b.victim_pid == 100
    assert b.blocker_pid == 200
    assert b.victim_traceparent == VICTIM_TP
    assert b.blocker_traceparent == BLOCKER_TP


def test_tracker_holds_open_then_resolves():
    tracker = LockTracker()
    rows = [make_row(100, 200, f"q /*traceparent='{VICTIM_TP}'*/", f"q /*traceparent='{BLOCKER_TP}'*/")]
    blocks = detect_blocks(rows)

    # first poll: block appears, nothing resolved yet
    resolved = tracker.update(blocks, now=1000.0)
    assert resolved == []

    # second poll: still blocked, still nothing resolved
    resolved = tracker.update(blocks, now=1000.5)
    assert resolved == []

    # third poll: block gone -> episode resolves with full duration
    resolved = tracker.update([], now=1002.0)
    assert len(resolved) == 1
    assert resolved[0].duration_s == 0.5  # last_seen(1000.5) - first_seen(1000.0)


def build_emitter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return LockEmitter(provider.get_tracer("test")), exporter


def test_lock_span_emitted_into_victim_trace():
    tracker = LockTracker()
    rows = [make_row(100, 200, f"q /*traceparent='{VICTIM_TP}'*/", f"q /*traceparent='{BLOCKER_TP}'*/")]
    blocks = detect_blocks(rows)
    tracker.update(blocks, now=1000.0)
    resolved = tracker.update([], now=1001.0)

    emitter, exporter = build_emitter()
    assert emitter.emit(resolved[0]) is True
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    # parented into the victim's trace
    assert s.context.trace_id == int("11111111111111111111111111111111", 16)
    # links to the culprit's trace
    assert s.links[0].context.trace_id == int("33333333333333333333333333333333", 16)
    assert s.attributes["db.blocked_by.trace_id"] == "33333333333333333333333333333333"
    assert s.attributes["db.blocked_by.pid"] == 200


def test_no_victim_traceparent_skips():
    tracker = LockTracker()
    rows = [make_row(100, 200, "UPDATE orders SET x=1", f"q /*traceparent='{BLOCKER_TP}'*/")]
    blocks = detect_blocks(rows)
    tracker.update(blocks, now=1000.0)
    resolved = tracker.update([], now=1001.0)

    emitter, exporter = build_emitter()
    # no victim traceparent -> nothing to hang the span under
    assert emitter.emit(resolved[0]) is False
    assert exporter.get_finished_spans() == ()
