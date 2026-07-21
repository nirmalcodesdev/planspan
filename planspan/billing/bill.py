"""Query cost math: the IO amplification and the dollar figure.

Both are deliberately simple and labeled estimates. The point is to put query cost
in a unit an engineering manager reads without translation.
"""
from dataclasses import dataclass

_PAGE_BYTES = 8192
_HOURS_PER_MONTH = 730


@dataclass
class QueryBill:
    io_amplification: float      # bytes read per row returned
    bytes_read: int
    rows_returned: float
    dollars_per_month: float


def io_amplification(buffers_read: int, rows_returned: float) -> float:
    """Bytes read from disk per row actually returned. High = reading a lot to
    show a little (the "1.9 GB to return 12 rows" story)."""
    bytes_read = buffers_read * _PAGE_BYTES
    return bytes_read / max(rows_returned, 1.0)


def dollars_per_month(
    total_ms: float,
    calls_per_hour: float,
    dollars_per_cpu_hour: float = 0.12,
) -> float:
    """Rough monthly cost of a query's CPU time at its observed call rate.

    total_ms is per-call execution time; calls_per_hour from pg_stat_statements.
    dollars_per_cpu_hour defaults to a mid-range managed-Postgres vCPU price;
    tune to your provider.
    """
    cpu_hours_per_month = (total_ms / 1000 / 3600) * calls_per_hour * _HOURS_PER_MONTH
    return cpu_hours_per_month * dollars_per_cpu_hour


def bill(
    buffers_read: int,
    rows_returned: float,
    total_ms: float,
    calls_per_hour: float,
    dollars_per_cpu_hour: float = 0.12,
) -> QueryBill:
    return QueryBill(
        io_amplification=io_amplification(buffers_read, rows_returned),
        bytes_read=buffers_read * _PAGE_BYTES,
        rows_returned=rows_returned,
        dollars_per_month=dollars_per_month(total_ms, calls_per_hour, dollars_per_cpu_hour),
    )
