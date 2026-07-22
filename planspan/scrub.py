"""Optional literal-value scrubbing for span attributes.

Filter clauses and captured query text can carry real user data (emails, ids)
straight into span attributes. Off by default since the demo wants to show real
values; set SCRUB_LITERALS=true to redact before emitting.
"""
import os
import re

_QUOTED = re.compile(r"'[^']*'")


def enabled() -> bool:
    return os.environ.get("SCRUB_LITERALS", "false").lower() in ("1", "true", "on", "yes")


def scrub(text: str | None) -> str | None:
    if text is None or not enabled():
        return text
    return _QUOTED.sub("'***'", text)
