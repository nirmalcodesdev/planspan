"""Alert-triggered auto-diagnosis: turn PlanSpan trace data into a migration.

The idea.md thesis, made literal and safe: instead of an LLM narrating "you should
add an index", PlanSpan already ran the what-if in Act 4 and put the *verified* DDL
on the span (whatif.ddl). This script reads that back through the SigNoz MCP server
— the same interface an AI agent uses — and emits a ready-to-review migration file.
The fix is deterministic; an LLM (optional) only writes the prose around it.

Run on demand or from the plan-flip alert's webhook.

  MCP_URL (default http://localhost:8009/mcp)   -> data, via the SigNoz MCP server
  ANTHROPIC_API_KEY (optional)                   -> prose

Usage:
  python diagnose.py                 # find the biggest recent what-if win, emit migration
  python diagnose.py --minutes 30
"""
import argparse
import os
import sys

from signoz_client import MCP, MCPError, WHATIF_FIELDS


def find_worst_whatif(mcp: MCP, minutes: int):
    """Most recent what-if spans, richest speedup first."""
    res = mcp.raw_traces(
        filter_expr="db.postgresql.plan.simulated = true",
        select_fields=WHATIF_FIELDS,
        minutes=minutes,
        limit=20,
    )
    rows = res["data"]["data"]["results"][0].get("rows") or []
    facts = [r["data"] for r in rows if r["data"].get("whatif.ddl")]
    facts.sort(key=lambda d: float(d.get("whatif.speedup", 0)), reverse=True)
    return facts


def build_migration(ddl: str, relation: str, speedup: float, trace_id: str) -> str:
    return f"""-- PlanSpan auto-diagnosis migration
-- relation: {relation}
-- projected speedup: {speedup:.0f}x (hypopg what-if, EXPLAIN-only, not measured)
-- verified against trace: {trace_id}
-- review before applying.

{ddl}
"""


def narrate(relation, ddl, speedup, trace_id):
    """Optional Claude prose. Falls back to a template if no key / SDK."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return (
            f"The query on `{relation}` runs a sequential scan. PlanSpan's what-if "
            f"(trace {trace_id}) shows a matching index would cut planner cost "
            f"~{speedup:.0f}x. Apply the migration below — it is the exact DDL "
            f"PlanSpan already verified against the plan."
        )
    try:
        import anthropic
    except ImportError:
        return f"(pip install anthropic for narrative) — apply the {relation} index below."

    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                f"You are an on-call DBA. PlanSpan detected a sequential scan on "
                f"`{relation}` (trace {trace_id}). Its hypopg what-if (EXPLAIN-only) "
                f"projects a {speedup:.0f}x planner-cost reduction from this index:\n{ddl}\n\n"
                f"Write a 3-4 sentence diagnosis for the incident channel. Cite the "
                f"what-if as verification. Do not invent numbers beyond these."
            ),
        }],
    )
    return msg.content[0].text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--out", default="migrations")
    args = ap.parse_args()

    mcp = MCP()
    try:
        facts = find_worst_whatif(mcp, args.minutes)
    except MCPError as e:
        print(f"MCP query failed: {e}", file=sys.stderr)
        return 1

    if not facts:
        print("no what-if candidates in window — nothing to diagnose", file=sys.stderr)
        return 1

    best = facts[0]
    ddl = best["whatif.ddl"]
    relation = best.get("db.postgresql.plan.relation", "unknown")
    speedup = float(best.get("whatif.speedup", 0))
    trace_id = best.get("trace_id", "")

    os.makedirs(args.out, exist_ok=True)
    fname = os.path.join(args.out, f"add_index_{relation}.sql")
    with open(fname, "w") as f:
        f.write(build_migration(ddl, relation, speedup, trace_id))

    print(narrate(relation, ddl, speedup, trace_id))
    print(f"\nwrote migration: {fname}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
