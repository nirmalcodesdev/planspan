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

from emitter import PlanEmitter
from logreader import iter_entries
from parser import parse

import lockpoller.runner as lock_runner


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

    print(f"planspan sidecar up. tailing {log_path}", flush=True)

    for item in iter_entries(follow(log_path)):
        plan = parse(item.entry, log_time=item.log_time)
        plan.duration_ms = item.duration_ms or plan.duration_ms
        n = emitter.emit(plan, now_ns=time.time_ns())
        tp = plan.traceparent or "no-parent"
        print(f"emitted {n} spans  dur={plan.duration_ms:.1f}ms  tp={tp}", flush=True)


if __name__ == "__main__":
    main()
