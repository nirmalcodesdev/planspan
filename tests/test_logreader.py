import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "planspan"))

from logreader import iter_entries

SAMPLE = """\
2026-07-20 04:14:33.921 UTC [504302] planspan@shop LOG:  duration: 0.054 ms  statement: BEGIN
2026-07-20 04:14:33.987 UTC [504302] planspan@shop LOG:  duration: 474.080 ms  plan:
\t{
\t  "Query Text": "SELECT orders.status FROM orders GROUP BY orders.status /*traceparent='00-d9346a69a669b8421cc2b24943c7b74e-59b516c25dc188af-01'*/",
\t  "Plan": {
\t    "Node Type": "Aggregate",
\t    "Actual Total Time": 474.080,
\t    "Actual Rows": 2,
\t    "Actual Loops": 1,
\t    "Plans": [
\t      {
\t        "Node Type": "Seq Scan",
\t        "Relation Name": "orders",
\t        "Actual Total Time": 224.486,
\t        "Actual Rows": 166667,
\t        "Actual Loops": 3
\t      }
\t    ]
\t  }
\t}
2026-07-20 04:14:33.988 UTC [504302] planspan@shop LOG:  duration: 0.037 ms  statement: ROLLBACK
"""


def test_extracts_single_plan():
    entries = list(iter_entries(SAMPLE.splitlines(keepends=True)))
    assert len(entries) == 1
    e = entries[0]
    assert e.duration_ms == 474.080
    assert e.entry["Plan"]["Node Type"] == "Aggregate"
    assert "traceparent" in e.entry["Query Text"]


def test_log_time_parsed():
    e = list(iter_entries(SAMPLE.splitlines(keepends=True)))[0]
    # 2026-07-20 04:14:33.987 UTC as epoch
    assert e.log_time > 0
    # fractional seconds preserved
    assert abs(e.log_time - int(e.log_time) - 0.987) < 0.001


def test_ignores_non_plan_lines():
    lines = [
        "2026-07-20 04:14:33.921 UTC [1] planspan@shop LOG:  duration: 0.05 ms  statement: BEGIN\n",
        "random noise line\n",
    ]
    assert list(iter_entries(lines)) == []


def test_two_plans_back_to_back():
    doubled = SAMPLE + SAMPLE
    entries = list(iter_entries(doubled.splitlines(keepends=True)))
    assert len(entries) == 2
