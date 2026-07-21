from .candidate import IndexCandidate, find_candidate
from .emit import WhatIfEmitter
from .run import WhatIf, run_whatif

# WhatIfRunner pulls in psycopg; import lazily so pure-logic tests don't need it.
__all__ = [
    "IndexCandidate",
    "find_candidate",
    "WhatIf",
    "run_whatif",
    "WhatIfEmitter",
    "WhatIfRunner",
]


def __getattr__(name):
    if name == "WhatIfRunner":
        from .runner import WhatIfRunner

        return WhatIfRunner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
