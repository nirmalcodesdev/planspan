import os
from urllib.parse import quote_plus

from opentelemetry import trace
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def _dsn() -> str:
    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    db = os.environ.get("PG_DB", "shop")
    user = quote_plus(os.environ.get("PG_USER", "planspan"))
    pw = quote_plus(os.environ.get("PG_PASSWORD", "changeme"))
    return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{db}"


engine = create_engine(_dsn(), pool_size=10, max_overflow=20, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def _traceparent() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return (
        f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{ctx.trace_flags:02x}"
    )


@event.listens_for(engine, "before_cursor_execute", retval=True)
def _inject_traceparent(conn, cursor, statement, parameters, context, executemany):
    # sqlcommenter-style: append the active trace context as a trailing SQL
    # comment. it survives verbatim into the auto_explain log, which is how the
    # sidecar re-parents the plan subtree under this request.
    tp = _traceparent()
    if tp and "traceparent=" not in statement:
        statement = f"{statement} /*traceparent='{tp}'*/"
    return statement, parameters
