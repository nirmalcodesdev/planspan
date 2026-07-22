# PlanSpan: the trace that goes inside the database

*Track 3, Agents of SigNoz Hackathon. Built on a bare Hostinger VPS in about two
days. Repo: [github.com/nirmalcodesdev/planspan](https://github.com/nirmalcodesdev/planspan).*

A few weeks back I wrote about [self-hosting SigNoz on a bare VPS](https://medium.com/@codernirmalbam/self-hosting-signoz-on-a-bare-vps-from-a-foundry-deploy-to-tracing-my-own-n-1-query-0e1a569656cd) —
Foundry deploy, a Postgres monitoring role, an OTel-instrumented FastAPI app, a
manufactured N+1. By the end I had real distributed traces of my own app. I could
see the HTTP request, the SQL statement it issued, the duration. What I couldn't
see was *why* the SQL statement took 900ms instead of 9. The trace stopped exactly
where the interesting part started: `SELECT ... — 900ms`, one gray bar, no detail.

That gray bar is where every APM on earth gives up on the database. PlanSpan is
what happens if you don't.

## The idea

Postgres already computes the answer to "why was this slow" every time it runs a
query — that's what `EXPLAIN ANALYZE` is. The information exists. It just lives in
a place no tracing tool looks: the query planner's internal node tree, printed to
a log file, thrown away after you `grep` it once.

PlanSpan reads that log, turns the plan into a tree of real OpenTelemetry spans —
one per `Seq Scan`, `Hash Join`, `Sort` — and grafts that tree under the exact HTTP
request that triggered it, using the same `traceparent` trick sqlcommenter uses.
The trace doesn't stop at the database anymore. It goes inside.

Then it does three things no query-plan tool at any price does:

- shows you the plan you **could have had**, as a sibling trace, with the DDL
- links your stuck request to the **other request** that was holding the lock
- reads all of it back through an **agent**, and hands you a migration file

## Act 1 — the plan becomes a trace

`auto_explain` is a contrib module every managed Postgres allows — no superuser,
no extension you're not permitted to install. Turn it on, and every query slower
than a threshold gets its full JSON plan written to the log:

```
auto_explain.log_min_duration = 200ms
auto_explain.log_format = json
auto_explain.log_analyze = on
auto_explain.log_buffers = on
```

A sidecar tails that log, parses each plan tree, and emits it as OTel spans —
backdated to when the query actually ran, so it lands in the right place on the
timeline. The stitch that makes it a *trace* and not just floating spans: the app
injects a `/*traceparent='00-...'*/` SQL comment via a SQLAlchemy hook, and that
comment survives verbatim into the auto_explain log line. The sidecar reads it
back out and reparents the plan subtree under the live request.

Hit a slow endpoint on my demo shop and the SigNoz waterfall shows (real capture,
committed as a test fixture in the repo):

```
GET /search
└─ Aggregate            474ms   (finalize)
   └─ Gather Merge      474ms   (2 parallel workers)
      └─ Sort           450ms   (per worker, quicksort)
         └─ Aggregate   449ms   (partial, hashed)
            └─ Seq Scan orders   224ms   rows=166,667/worker   buffers_hit=5185
```

Every node carries real numbers: rows estimated vs. rows actual (skew), buffers
hit vs. read, whether it ran in a parallel worker. And because these are just
spans with attributes, SigNoz's Trace Explorer becomes a plan search engine —
filter `db.postgresql.plan.node_type = Seq Scan AND relation = orders` and you get
every full-table scan on your biggest table, ranked by cost, for the whole
retention window. No `pg_stat_statements`, no manual `EXPLAIN`, no tool I know of
does this today.

## Act 2 — who blocked you

A slow request is annoying. A request stuck behind someone else's lock is a
mystery — `pg_stat_activity` tells you *a* pid is blocking you, but not what that
other request was, or who was making it.

A background poller watches `pg_stat_activity` for sessions in `wait_event_type =
'Lock'`, calls `pg_blocking_pids()`, and — because the blocking session's last
statement carries its own `traceparent` — emits a `Lock wait` span into the
*victim's* trace that links straight to the *blocker's* trace.

I reproduced it with two endpoints, one holding a row lock idle-in-transaction,
one trying to grab it:

```
lock span: victim 768857 blocked_by 768854 6023ms
```

In SigNoz, the victim's trace carries `db.blocked_by.trace_id` pointing at the
blocker's actual trace — click through and you're looking at the other user's
request, not a bare pid. One caveat I found the hard way: the blocker has to be
sitting idle-in-transaction for its *last* statement to still be the one that took
the lock. If it's mid-`pg_sleep()` or another query, `pg_stat_activity` shows that
instead and you only get the pid, not the trace link. Real Postgres behavior,
worth knowing before you build a demo around it.

## Act 3 — what changed

Every plan gets fingerprinted by its *shape* — node types, relations, indexes
touched — hashed, ignoring timings entirely. Two runs of the same query with the
same plan hash identically even if one took 2ms and the other took 2s. When the
hash changes, something changed underneath the query: stale statistics, a dropped
index, a vacuum that never ran.

I dropped `ix_orders_email` live and watched:

```
PLAN FLIP qid=7616482227047151458: Index Scan[ix_orders_email] -> Gather, Seq Scan[orders]
```

That line becomes a `Plan flip` span with the human-readable diff and a
`last_good_trace_id` pointing at the last time this query ran the fast way — one
click from "it's broken now" to "here's what it looked like before." Wired to a
SigNoz alert on `db.postgresql.plan.flipped = true`, and it fires within a minute
of the regression, not when someone notices `/checkout` is slow.

## Act 4 — the fix, verified before you apply it

This is the one I think doesn't exist anywhere else. On a slow `Seq Scan` with a
filter, the sidecar asks `hypopg` — a real Postgres extension, allowlisted on
every managed Postgres I've checked — to create a *hypothetical* index, entirely
in memory, and re-runs `EXPLAIN` (never `ANALYZE`; nothing executes). If the
planner would actually pick that index, PlanSpan emits the hypothetical plan as a
**second, simulated trace under the same request**:

```
what-if: orders(email) speedup=38426.1x
```

One waterfall, two universes, side by side. The simulated spans carry
`db.postgresql.plan.simulated=true` and, on the root:

```
whatif.speedup = 38426.1
whatif.ddl = "CREATE INDEX CONCURRENTLY orders_email_idx ON orders (email);"
```

That DDL isn't a suggestion an LLM made up. It's the exact statement `hypopg`
verified against the real plan, sitting on the span, ready to copy into a
migration.

## Act 5 — an agent that reads the trace

I ran the official SigNoz MCP server against my own instance and pointed a real
Claude Code session at it — asked it, with no other context, *"why is /orders
slow?"* It searched traces, read the plan spans, and came back with this:

> The root cause is **PostgreSQL query-plan instability**: the planner
> intermittently abandons the `ix_orders_email` index in favor of a full parallel
> sequential scan on `orders`... Directly alongside it, the same trace also
> contains a `[what-if] Index Scan orders` span — 284,644 ns. That's roughly a
> **40,000× gap**... This isn't a missing-index problem — `ix_orders_email` exists
> and is fast. The problem is **plan choice instability**.

It cited the `Plan flip` spans in both directions, the what-if simulation, and the
exact p99s, and correctly told the difference between "no index" and "index
exists but the planner won't use it" — a distinction that matters and that a
naive "add an index" bot would get wrong.

For the zero-risk path — no LLM anywhere near the data plane — `diagnose.py`
queries the same MCP server, pulls the biggest recent what-if win, and
deterministically writes `migrations/add_index_orders.sql` from the verified
`whatif.ddl`. Wire it to the plan-flip alert's webhook and the migration is
sitting in a PR before the on-call engineer's phone buzzes.

## What it costs

Nothing here is free, and I measured it instead of guessing. On the VPS, running
the aggregate query 25 times with `auto_explain`'s `log_analyze` + `log_buffers` +
`log_timing` on versus off: **~35% added latency on that specific heavy query** —
real per-node instrumentation cost, not noise. Governed two ways: only queries
past `log_min_duration` (200ms here) pay it at all, and `auto_explain.sample_rate`
exists to dial further down under real load. The sidecar itself idles at ~0.3% CPU
and 31MB resident — it's the logging, not the tailer, that costs something.

The waterfall is also, honestly, a *cost-map* and not a strict timeline — Postgres
executes nodes in an interleaved iterator model and `EXPLAIN` gives inclusive
durations, not start offsets. PlanSpan lays parent and child spans starting
together with duration = `actual_time × loops`; "the widest bar is the most
expensive node" still holds, it's a stated layout decision, not an accident.

## Try it

```bash
git clone https://github.com/nirmalcodesdev/planspan
cd planspan
cp .env.example .env
sudo ./deploy/postgres/setup.sh
cd deploy && docker compose up --build -d
```

Everything — the demo shop, the sidecar, the lock poller, the what-if runner, the
fingerprinting, the dashboard and alert JSON, the MCP client — is in that repo.
Built on nothing but `auto_explain`, `hypopg`, and `pg_stat_activity`: contrib
modules and system views every managed Postgres already allows. It works on the
RDS instance you're not allowed to `sudo` into.
