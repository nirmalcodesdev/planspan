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
    """Most recent what-if spans, richest estimated cost reduction first."""
    res = mcp.raw_traces(
        filter_expr="db.postgresql.plan.simulated = true",
        select_fields=WHATIF_FIELDS,
        minutes=minutes,
        limit=20,
    )
    rows = res["data"]["data"]["results"][0].get("rows") or []
    facts = [r["data"] for r in rows if r["data"].get("whatif.ddl")]
    facts.sort(key=lambda d: float(d.get("whatif.est_cost_reduction", 0)), reverse=True)
    return facts


def build_migration(ddl: str, relation: str, cost_reduction: float, trace_id: str) -> str:
    return f"""-- PlanSpan auto-diagnosis — GENERATED FOR REVIEW
-- relation: {relation}
-- estimated planner cost reduction: {cost_reduction:.0f}x (hypopg what-if,
--   planner-only EXPLAIN — NOT a measured latency improvement)
-- derived from trace: {trace_id}
--
-- Before applying: validate index size, write/maintenance cost, workload fit,
-- and your migration policy. CREATE INDEX CONCURRENTLY has its own operational
-- caveats (cannot run in a txn block, can leave an invalid index on failure).

{ddl}
"""


def narrate(relation, ddl, cost_reduction, trace_id):
    """Optional Claude prose. Falls back to a template if no key / SDK."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return (
            f"The query on `{relation}` runs a sequential scan. PlanSpan's what-if "
            f"(trace {trace_id}) estimates a matching index would cut the planner's "
            f"cost ~{cost_reduction:.0f}x (planner estimate, not measured latency). "
            f"The proposed DDL is below — review before applying."
        )
    try:
        import anthropic
    except ImportError:
        return f"(pip install anthropic for narrative) — review the {relation} index proposal below."

    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                f"You are an on-call DBA. PlanSpan detected a sequential scan on "
                f"`{relation}` (trace {trace_id}). Its hypopg what-if (EXPLAIN-only) "
                f"estimates a {cost_reduction:.0f}x planner-cost reduction (planner "
                f"estimate, not measured latency) from this index:\n{ddl}\n\n"
                f"Write a 3-4 sentence diagnosis for the incident channel. Cite the "
                f"what-if as a planner estimate to validate, not a proven fix. Do not "
                f"invent numbers beyond these."
            ),
        }],
    )
    return msg.content[0].text


def run_diagnosis(minutes: int = 60, out: str = "migrations"):
    """Callable core, shared by the CLI and the alert webhook listener.

    Returns a dict describing what was written, or None if there was nothing
    to diagnose (no MCP data, or MCP unreachable).
    """
    mcp = MCP()
    try:
        facts = find_worst_whatif(mcp, minutes)
    except MCPError as e:
        print(f"MCP query failed: {e}", file=sys.stderr)
        return None

    if not facts:
        print("no what-if candidates in window — nothing to diagnose", file=sys.stderr)
        return None

    best = facts[0]
    ddl = best["whatif.ddl"]
    relation = best.get("db.postgresql.plan.relation", "unknown")
    cost_reduction = float(best.get("whatif.est_cost_reduction", 0))
    trace_id = best.get("trace_id", "")

    os.makedirs(out, exist_ok=True)
    fname = os.path.join(out, f"add_index_{relation}.sql")
    with open(fname, "w") as f:
        f.write(build_migration(ddl, relation, cost_reduction, trace_id))

    text = narrate(relation, ddl, cost_reduction, trace_id)
    print(text)
    print(f"\nwrote migration: {fname}")
    return {"file": fname, "narrative": text, "relation": relation,
            "est_cost_reduction": cost_reduction, "trace_id": trace_id}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--out", default="migrations")
    args = ap.parse_args()
    return 0 if run_diagnosis(args.minutes, args.out) else 1


if __name__ == "__main__":
    sys.exit(main())
