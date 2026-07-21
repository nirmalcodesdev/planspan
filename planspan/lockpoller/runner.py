"""Background lock-poller loop: its own PG connection, polls on an interval,
emits a span per resolved lock episode.
"""
import os
import time

import psycopg

from .emit import LockEmitter
from .poll import BLOCKED_QUERY, LockTracker, detect_blocks


def _dsn() -> str:
    from urllib.parse import quote_plus

    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    db = os.environ.get("PG_DB", "shop")
    user = quote_plus(os.environ.get("PG_USER", "planspan"))
    pw = quote_plus(os.environ.get("PG_PASSWORD", "changeme"))
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def run(tracer=None, stop=None):
    interval = float(os.environ.get("LOCK_POLL_INTERVAL", "0.5"))
    tracker = LockTracker()
    emitter = LockEmitter(tracer)
    print(f"lock poller up. interval={interval}s", flush=True)

    while stop is None or not stop.is_set():
        try:
            with psycopg.connect(_dsn(), connect_timeout=5) as conn:
                conn.autocommit = True
                while stop is None or not stop.is_set():
                    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                        cur.execute(BLOCKED_QUERY)
                        rows = cur.fetchall()
                    blocks = detect_blocks(rows)
                    for ep in tracker.update(blocks, time.time()):
                        if emitter.emit(ep):
                            print(
                                f"lock span: victim {ep.block.victim_pid} "
                                f"blocked_by {ep.block.blocker_pid} "
                                f"{ep.duration_s*1000:.0f}ms",
                                flush=True,
                            )
                    time.sleep(interval)
        except Exception as e:  # noqa: BLE001 - keep the poller alive across DB blips
            print(f"lock poller error: {e}; retrying in 3s", flush=True)
            time.sleep(3)
