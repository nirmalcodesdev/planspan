"""PlanSpan sidecar: tail auto_explain log -> plan spans -> SigNoz OTLP.

Also runs the lock poller in a background thread.
"""
import os
import threading
import time

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from billing import BillingRunner
from emitter import PlanEmitter
from fingerprint import FlipTracker, fingerprint
from logreader import iter_entries
from parser import parse

import lockpoller.runner as lock_runner
from whatif import WhatIfRunner


def _setup_tracer():
    service = os.environ.get("SIDECAR_SERVICE_NAME", "planspan")
    endpoint = os.environ.get("OTLP_ENDPOINT", "localhost:4317")
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
    return trace.get_tracer("planspan")


def follow(path):
    """Yield lines from a growing file, tolerating rotation and late creation."""
    while not os.path.exists(path):
        print(f"waiting for {path}", flush=True)
        time.sleep(2)
    f = open(path, "r", errors="replace")
    # start at end — we only care about plans logged from now on
    f.seek(0, os.SEEK_END)
    inode = os.fstat(f.fileno()).st_ino
    while True:
        line = f.readline()
        if line:
            yield line
            continue
        time.sleep(0.5)
        # detect rotation: same path, new inode
        try:
            if os.stat(path).st_ino != inode:
                f.close()
                f = open(path, "r", errors="replace")
                inode = os.fstat(f.fileno()).st_ino
        except FileNotFoundError:
            pass


def main():
    log_path = os.environ.get("PG_LOG_PATH", "/var/log/postgresql/postgresql-17-main.log")
    tracer = _setup_tracer()
    emitter = PlanEmitter(tracer)

    if os.environ.get("LOCK_POLLER", "on") != "off":
        t = threading.Thread(target=lock_runner.run, args=(tracer,), daemon=True)
        t.start()

    whatif = WhatIfRunner(tracer) if os.environ.get("WHATIF", "on") != "off" else None
    billing = (
        BillingRunner(dollars_per_cpu_hour=float(os.environ.get("DOLLARS_PER_CPU_HOUR", "0.12")))
        if os.environ.get("BILLING", "on") != "off"
        else None
    )
    flips = FlipTracker()

    print(f"planspan sidecar up. tailing {log_path}", flush=True)

    for item in iter_entries(follow(log_path)):
        plan = parse(item.entry, log_time=item.log_time)
        plan.duration_ms = item.duration_ms or plan.duration_ms
        now_ns = time.time_ns()

        root_attrs = {"db.postgresql.plan.fingerprint": fingerprint(plan)}
        if plan.query_id is not None:
            root_attrs["db.postgresql.plan.query_id"] = str(plan.query_id)
        last_good = flips.last_good_trace(plan.query_id)

        flip = flips.observe(plan)
        if flip is not None:
            root_attrs["db.postgresql.plan.flipped"] = True
            root_attrs["db.postgresql.plan.flip_diff"] = flip.diff
            if flip.last_good_trace_id:
                root_attrs["db.postgresql.plan.last_good_trace_id"] = flip.last_good_trace_id
            _emit_flip_event(tracer, plan, flip, now_ns)
            print(f"PLAN FLIP qid={flip.query_id}: {flip.diff}", flush=True)
        elif last_good:
            root_attrs["db.postgresql.plan.last_good_trace_id"] = last_good

        if billing is not None:
            root_attrs.update(billing.bill_attrs(plan))

        n = emitter.emit(plan, now_ns=now_ns, root_attrs=root_attrs)
        tp = plan.traceparent or "no-parent"
        print(f"emitted {n} spans  dur={plan.duration_ms:.1f}ms  tp={tp}", flush=True)

        if whatif is not None:
            start_ns = _plan_start_ns(plan, now_ns)
            whatif.maybe_emit(plan, start_ns)


def _emit_flip_event(tracer, plan, flip, now_ns):
    """A short standalone span marking a plan regression — alertable in SigNoz
    by filtering on db.postgresql.plan.flipped = true."""
    from emitter import parent_context_from_traceparent

    parent = parent_context_from_traceparent(plan.traceparent)
    span = tracer.start_span(
        name=f"Plan flip: {flip.diff}",
        context=parent,
        start_time=now_ns,
        attributes={
            "db.postgresql.plan.flipped": True,
            "db.postgresql.plan.flip_diff": flip.diff,
            "db.postgresql.plan.query_id": str(flip.query_id),
            "db.postgresql.plan.old_fingerprint": flip.old_fingerprint,
            "db.postgresql.plan.new_fingerprint": flip.new_fingerprint,
            "db.postgresql.plan.last_good_trace_id": flip.last_good_trace_id or "",
        },
    )
    span.end(end_time=now_ns + 1_000_000)


def _plan_start_ns(plan, now_ns):
    if plan.log_time is not None:
        end_ns = int(plan.log_time * 1_000_000_000)
    else:
        end_ns = now_ns
    return end_ns - int(plan.duration_ms * 1_000_000)


if __name__ == "__main__":
    main()
