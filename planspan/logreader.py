"""Read auto_explain entries out of a Postgres logfile.

auto_explain writes the plan as pretty-printed JSON spanning many lines, behind
a log-line prefix like:

    2026-07-20 04:14:33.987 UTC [504302] planspan@shop LOG:  duration: 474.080 ms  plan:
        {
          "Query Text": ...
          "Plan": {...}
        }

We pull out the duration + timestamp from the prefix and brace-count the JSON
body that follows.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime

_PREFIX_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ \w+).*?"
    r"duration:\s*(?P<dur>[\d.]+)\s*ms\s+plan:",
)


@dataclass
class LogEntry:
    entry: dict          # the parsed auto_explain JSON object
    duration_ms: float
    log_time: float      # epoch seconds


def _parse_ts(ts: str) -> float:
    # "2026-07-20 04:14:33.987 UTC" -> epoch seconds
    text = ts.rsplit(" ", 1)[0]  # drop tz label; postgres logs in UTC
    dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f")
    return dt.replace(tzinfo=None).timestamp()


def iter_entries(lines):
    """Yield LogEntry for each auto_explain plan found in an iterable of lines.

    Stateful across lines so it handles the multi-line JSON body. Lines should
    include their trailing newline (as from a file iterator); tabs in the
    continuation lines are stripped so json can parse.
    """
    prefix = None
    buf = []
    depth = 0
    collecting = False

    for line in lines:
        if not collecting:
            m = _PREFIX_RE.match(line)
            if not m:
                continue
            prefix = m
            # the '{' may be on this line after 'plan:' or on the next
            rest = line[m.end():]
            if "{" in rest:
                collecting = True
                buf = [rest]
                depth = rest.count("{") - rest.count("}")
            else:
                collecting = True
                buf = []
                depth = 0
            continue

        buf.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0 and buf:
            body = "".join(buf).replace("\t", "")
            start = body.find("{")
            end = body.rfind("}")
            try:
                entry = json.loads(body[start : end + 1])
                yield LogEntry(
                    entry=entry,
                    duration_ms=float(prefix.group("dur")),
                    log_time=_parse_ts(prefix.group("ts")),
                )
            except (json.JSONDecodeError, ValueError):
                pass
            collecting = False
            prefix = None
            buf = []
