from .bill import QueryBill, bill, dollars_per_month, io_amplification
from .query import CallStats, calls_per_hour, fetch_call_stats

__all__ = [
    "QueryBill", "bill", "dollars_per_month", "io_amplification",
    "CallStats", "calls_per_hour", "fetch_call_stats", "BillingRunner",
]


def __getattr__(name):
    if name == "BillingRunner":
        from .runner import BillingRunner

        return BillingRunner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
