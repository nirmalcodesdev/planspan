"""Watch pg_stat_activity for sessions blocked on locks, and link the victim to
the request that blocked it.

A blocked session waits on a lock held by another backend. pg_blocking_pids()
gives us the blockers. Both the victim and the blocker carry their own
traceparent SQL comment, so we can point the victim's trace at the culprit's.
"""
from dataclasses import dataclass, field

from traceparent import extract_traceparent

# one row per (victim, blocker) pair currently waiting on a lock
BLOCKED_QUERY = """
SELECT
  a.pid              AS victim_pid,
  a.query            AS victim_query,
  a.wait_event_type  AS wait_event_type,
  a.wait_event       AS wait_event,
  bl.pid             AS blocker_pid,
  bl.query           AS blocker_query,
  now() - a.query_start AS waited
FROM pg_stat_activity a
JOIN LATERAL unnest(pg_blocking_pids(a.pid)) AS blocker(pid) ON true
JOIN pg_stat_activity bl ON bl.pid = blocker.pid
WHERE a.wait_event_type = 'Lock'
"""


@dataclass
class Block:
    victim_pid: int
    blocker_pid: int
    victim_traceparent: str | None
    blocker_traceparent: str | None
    wait_event: str
    victim_query: str
    blocker_query: str


@dataclass
class LockEpisode:
    key: tuple[int, int]          # (victim_pid, blocker_pid)
    block: Block
    first_seen: float             # epoch seconds
    last_seen: float

    @property
    def duration_s(self) -> float:
        return max(self.last_seen - self.first_seen, 0.0)


def detect_blocks(rows) -> list[Block]:
    """Pure: turn query rows (dict-like) into Block records."""
    blocks = []
    for r in rows:
        blocks.append(
            Block(
                victim_pid=r["victim_pid"],
                blocker_pid=r["blocker_pid"],
                victim_traceparent=extract_traceparent(r.get("victim_query")),
                blocker_traceparent=extract_traceparent(r.get("blocker_query")),
                wait_event=r.get("wait_event") or "Lock",
                victim_query=r.get("victim_query") or "",
                blocker_query=r.get("blocker_query") or "",
            )
        )
    return blocks


class LockTracker:
    """Stitches per-poll snapshots into episodes so we emit one span per lock
    wait, stamped with its real start and total duration.
    """

    def __init__(self):
        self._open: dict[tuple[int, int], LockEpisode] = {}

    def update(self, blocks: list[Block], now: float) -> list[LockEpisode]:
        """Feed the current snapshot. Returns episodes that just RESOLVED
        (were open last time, gone now) — those are ready to emit.
        """
        seen = set()
        for b in blocks:
            key = (b.victim_pid, b.blocker_pid)
            seen.add(key)
            ep = self._open.get(key)
            if ep is None:
                self._open[key] = LockEpisode(key, b, now, now)
            else:
                ep.last_seen = now
                ep.block = b  # refresh (query text may fill in)

        resolved = [ep for k, ep in self._open.items() if k not in seen]
        for ep in resolved:
            del self._open[ep.key]
        return resolved

    def drain(self, now: float) -> list[LockEpisode]:
        """Flush all open episodes (e.g. on shutdown)."""
        for ep in self._open.values():
            ep.last_seen = now
        out = list(self._open.values())
        self._open.clear()
        return out
