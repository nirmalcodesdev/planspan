# PlanSpan

**Distributed tracing that doesn't stop at the database.**

In many application traces, Postgres appears as a single opaque query span: `SELECT ... — 4.2s`. PlanSpan continues the trace *inside* Postgres — each plan node (Seq Scan, Hash Join, Sort)
becomes a real OpenTelemetry span, parented under the HTTP request that ran it, and
rendered in SigNoz.

Built for the Agents of SigNoz hackathon (Track 3).

**Five acts:**
- **See** the plan as a trace
- **Explain** who blocked you (lock forensics)
- **Diagnose** what changed (plan fingerprinting)
- **Fix** it with a migration proposal validated by a hypothetical planner result
- **Automate** with an agent that reads the trace (AI diagnosis via SigNoz MCP)


## Table of contents

- [Prerequisites](#prerequisites)
- [Problem](#problem)
- [Solution / architecture](#solution--architecture)
- [SigNoz integration](#signoz-integration)
  - [SigNoz capability map](#signoz-capability-map)
  - [Traces: the plan as a waterfall](#traces-the-plan-as-a-waterfall)
  - [Trace Explorer as a plan search engine](#trace-explorer-as-a-plan-search-engine)
  - [Dashboard](#dashboard)
  - [What-if plans](#what-if-plans)
  - [Query billing](#query-billing)
  - [Plan fingerprinting + alerts](#plan-fingerprinting--alerts)
  - [AI diagnosis (SigNoz MCP)](#ai-diagnosis-signoz-mcp)
  - [Lock forensics](#lock-forensics)
- [Setup / usage](#setup--usage)
- [Demo path](#demo-path)
  - [Verify the demo](#verify-the-demo)
- [Honest engineering](#honest-engineering)
- [AI disclosure](#ai-disclosure)
- [Reference](#reference)

## Prerequisites

- Linux host with Docker and Docker Compose
- PostgreSQL 17 installed as a native systemd cluster
- Permission to run the Postgres setup script with `sudo`
- A VPS or host where ports required by the demo and SigNoz are reachable

PlanSpan is built around PostgreSQL's `auto_explain` (the one hard requirement) and
`pg_stat_activity`, with `hypopg` and `pg_stat_statements` where the provider allows
them. Availability differs across managed providers, engine versions, and admin
policy — run the compatibility check first to see what your Postgres permits:

```bash
PGPASSWORD=... ./deploy/postgres/compat_check.sh "host=... dbname=... user=..."
```

It's read-only, and reports each capability as ok / degraded / blocking. Every
feature except plan capture degrades gracefully if its extension is missing.


## Problem

In many application traces, Postgres appears as a single opaque query span: `SELECT ... — 4.2s`. Everything that happened inside that query — which plan node ran (a sequential scan, a hash join,
a sort), how its row estimates compared to what actually happened, whether it waited on
another session's lock — is invisible once it's collapsed into that one span. The
application trace stops at the database boundary.

## Solution / architecture

Three moving parts, deliberately separate:

- **SigNoz** — self-hosted via Foundry, runs as its own docker stack. We just target its
  OTLP endpoint (`localhost:4317`).
- **Postgres 17** — native systemd cluster (not a container). `auto_explain` logs slow
  query plans as JSON.
- **PlanSpan** (this repo) — a sidecar that tails the auto_explain log, turns each plan
  node into an OTel span, and stitches the subtree under the live app trace via a
  `traceparent` SQL comment. Plus a FastAPI demo shop to generate the traffic.

## SigNoz integration

### SigNoz capability map

| PlanSpan capability | SigNoz evidence |
|---|---|
| Per-node Postgres execution detail | Child spans nested under the application request trace |
| Recurring scan and plan analysis | Trace Explorer filters over `db.postgresql.plan.*` attributes |
| Query-cost visibility | Imported dashboard panels |
| Plan-regression detection | `Plan flip` spans and imported alert rules |
| Suggested index validation | Hypothetical sibling plan subtree in the same trace |
| Slow-query investigation assistance | MCP-based diagnosis over collected SigNoz telemetry |
| Blocking-session investigation | `Lock wait` span linked to the blocker trace |


### Traces: the plan as a waterfall

Hit a slow endpoint, then open SigNoz Traces (`http://<vps-ip>:8080`):

```bash
curl "http://localhost:8000/search"
```

Find the `shop-api` `/search` request and expand the waterfall. Under the HTTP span
you'll see the plan subtree — `Aggregate` → `Gather Merge` → `Sort` → `Aggregate`
(partial) → `Seq Scan orders` — each a real span carrying rows, buffers, loops, and
estimate-vs-actual skew. Exact shape depends on the plan the planner picks for your
data (real capture: `tests/golden/search_real_vps.json`).

The sidecar logs one line per captured plan:

```
emitted 5 spans  dur=882.0ms  tp=00-3272814e9e37...-d7b88a3d78f2...-01
```

`tp=00-...` is the trace it stitched under; `no-parent` means the query had no
traceparent (e.g. a background query, not an app request).

### Trace Explorer as a plan search engine

Because every plan node is a span with `db.postgresql.plan.*` attributes, Trace
Explorer lets us search plan behavior alongside the application requests that caused it.
For example, filter `db.postgresql.plan.node_type = Seq Scan` and `db.postgresql.plan.relation = orders`
to list every sequential scan on `orders` in the window, slowest first.

### Dashboard

Import `deploy/signoz/dashboards/query-plans.json` (Dashboards → Import JSON):
slowest plan nodes, seq scans by relation, node time by type, row skew, buffers read,
and top expensive queries ($/month).

### What-if plans

When a slow query does a Seq Scan with a filter, the sidecar asks `hypopg` what the
plan *would* be if the matching index existed. The candidate index is never built and
the query is never run — PlanSpan runs a **planner-only `EXPLAIN (FORMAT JSON)`**.
If the planner would use it, PlanSpan emits the hypothetical plan as a **sibling span
subtree under the same request**, marked `db.postgresql.plan.simulated=true`, with:

- `whatif.est_cost_reduction` — ratio of baseline to hypothetical **planner cost**.
  The planner's own estimate, **not** a measured wall-clock speedup.
- `whatif.ddl` — a proposed `CREATE INDEX CONCURRENTLY ...` for review

So one waterfall shows two universes: the plan you have and the plan the planner
estimates you could have. Reproduce by dropping the index and hitting the endpoint:

```bash
psql "$CONN" -c "DROP INDEX ix_orders_email"
curl "http://localhost:8000/orders?email=user4242@example.com"
```

The `/orders` trace gets a `[what-if] Index Scan orders` sibling with the estimated
cost reduction and the proposed DDL. Disable the runner with `WHATIF=off`.

### Query billing

When a captured plan has a `query_id` and `pg_stat_statements` already has a row for
it, the sidecar prices that plan. `planspan/billing` reads the query's real call rate
(scoped per-queryid via its own `stats_since`, no extra grants needed), then emits on
the plan root span:

- `billing.io_amplification_bytes_per_row` — total buffers read across the whole plan
  tree, per row actually returned (the "1.9 GB to return 12 rows" story)
- `billing.dollars_per_month` — CPU time × observed call rate × a tunable vCPU price
  (`DOLLARS_PER_CPU_HOUR`, default $0.12) — a labeled estimate, not a bill
- `billing.calls_per_hour` — observed rate used for the dollar math
- `billing.relation` — the costliest table-touching node's relation, so the number
  means something without opening the trace

If there's no `query_id` yet or stats aren't ready, billing attrs are simply omitted
(the plan spans still emit). Dashboard panel: **Top expensive queries ($/month)**,
sorted worst-first. Disable with `BILLING=off`.

### Plan fingerprinting + alerts

Every plan is hashed by its shape (node types + relations/indexes, ignoring timings).
When the same query's fingerprint changes — an Index Scan falling back to a Seq Scan —
the sidecar emits a `Plan flip` span with a human diff
(`Index Scan[ix_orders_email] -> Seq Scan[orders]`) and `last_good_trace_id` for the
before. Import the alerts in `deploy/signoz/alerts/` (plan flip, high-IO seq scan) to surface regressions earlier. The flip demo needs `auto_explain.log_min_duration = 0`
so the fast (indexed) baseline plan is also logged.

### AI diagnosis (SigNoz MCP)

Two ways in, same data, same fix — see `mcp/README.md` for setup:

- **A human types a question.** Claude connects to SigNoz via the official MCP
  server and reads the plan spans directly. Ask "why is /orders slow?" and it cites
  the what-if sibling as verification, distinguishing a real missing index from a
  planner just picking the wrong plan.
- **An alert fires.** `mcp/webhook.py` listens for SigNoz's alertmanager POST and
  runs the same diagnosis automatically — no LLM in the data path required.
  `mcp/diagnose.py` pulls the biggest recent what-if win through MCP and writes a
  ready-to-review `CREATE INDEX CONCURRENTLY` migration file: a migration proposal supported by a hypothetical planner result, ready for review before anyone asks.

### Lock forensics

A background poller watches `pg_stat_activity` for sessions blocked on locks and
calls `pg_blocking_pids()`. When a blocked request carries a traceparent, PlanSpan
emits a `Lock wait` span into the victim's trace with `db.blocked_by.trace_id`
pointing at the request that held the lock — click straight from a stuck request to
the culprit.

Reproduce it with the demo endpoints (blocker must hold the lock idle-in-transaction
so its last statement — the one that took the lock — is what the poller sees):

```bash
RID=$(psql "$CONN" -tAc "SELECT id FROM orders ORDER BY id LIMIT 1")
curl -X POST "http://localhost:8000/hold-lock?order_id=$RID&seconds=8" &
sleep 1
curl -X POST "http://localhost:8000/contend-lock?order_id=$RID"
```

The victim's trace gets a `Lock wait` span linking to the holder's trace. Tune the
poll cadence with `LOCK_POLL_INTERVAL` (default 0.5s); disable with `LOCK_POLLER=off`.

## Setup / usage

### 1. SigNoz via Foundry

```bash
curl -fsSL https://signoz.io/foundry.sh | bash
foundryctl cast -f deploy/casting.yaml
```

`deploy/casting.yaml` / `deploy/casting.yaml.lock` are the exact Foundry config this
was built and demoed against (self-hosted, compose flavor). The lock file's internal
metastore password is redacted — it's a container-only credential Foundry generates
per install, not something you need to reuse.

Complete the signup page at `http://<vps-ip>:8080/signup` before opening the SigNoz UI.

### 2. PlanSpan

```bash
cp .env.example .env         # edit PG_PASSWORD etc.
sudo PG_DB=shop PG_USER=planspan PG_PASSWORD=... ./deploy/postgres/setup.sh
cd deploy && docker compose up --build -d
```

`setup.sh` installs hypopg, drops the auto_explain config into
`/etc/postgresql/17/main/conf.d/`, restarts Postgres, and creates the app db/role.

Seed the shop (10M orders, server-side, idempotent):

```bash
docker compose exec demoapp python seed.py
# or a smaller set:
docker compose exec -e ORDERS=2000000 demoapp python seed.py
```

Endpoints:

- `GET /orders?email=user123@example.com` — indexed lookup; drop `ix_orders_email`
  live to watch it fall to a seq scan
- `GET /search` — unindexed aggregate over all orders
- `POST /checkout?user_id=1&product_id=1` — writes an order


## Demo path

1. Start SigNoz and PlanSpan using the setup steps above.
2. Seed the shop database.
3. Open `http://<vps-ip>:8080` and complete SigNoz signup.
4. Run:

   ```bash
   curl "http://localhost:8000/search"
   ```

5. In SigNoz Traces, open the `shop-api` `/search` request.
6. Expand the plan subtree to inspect individual Postgres plan-node spans.

For the full feature walkthrough, use the reproduction commands in the relevant
sections above.

### Verify the demo

A successful `/search` request should produce:

- A `shop-api` `/search` trace in SigNoz
- Plan-node child spans under that request
- Plan attributes such as node type, relation, rows, buffers, loops, and estimate-versus-actual skew
- Dashboard data after importing `deploy/signoz/dashboards/query-plans.json`

## Honest engineering

- **Overhead is real and governed, not hidden.** Measured on the VPS (25-run median,
  parallel aggregate over 10M rows): `auto_explain` with `log_analyze`+`log_buffers`+
  `log_timing` on adds **~35%** to that query's latency — the per-node instrumentation
  genuinely costs something. Governed two ways: `log_min_duration=200ms` means only
  slow queries pay it, and `auto_explain.sample_rate` can dial down further under load.
  The sidecar itself is cheap at idle (~0.3% CPU, ~31MB RSS).
- **The waterfall is a cost-map, not a timeline** — see `emitter/emit.py`. Postgres's
  iterator model interleaves node execution; EXPLAIN gives inclusive durations, not
  start offsets. Parent-start = child-start, duration = `actual_time × loops`. Widest
  bar = most expensive node still holds; it's a stated layout decision, not a bug.
- **PII:** filter clauses and captured lock-holder queries can carry real literal
  values (emails, ids). Set `SCRUB_LITERALS=true` to redact quoted literals before
  they reach span attributes.
- **What-if is EXPLAIN-only.** Nothing the sidecar suggests is ever executed against
  the database — hypopg indexes are in-memory and reset after each check.

## AI disclosure

I used Claude during development for testing support, bug fixing, and code quality improvements.

All project decisions, implementation, integration work, and final verification were done by us. We reviewed and validated the code and changes before including them in the project.


## Reference

### Span attributes

Spans use OTel's `db.*` conventions where they apply, plus a **PlanSpan-specific
custom namespace** `db.postgresql.plan.*` for the plan-node detail :

| attribute | meaning |
|---|---|
| `node_type` | Seq Scan, Index Scan, Hash Join, ... |
| `relation` / `index_name` | table / index touched |
| `total_ms` / `self_ms` | inclusive / exclusive time (× loops) |
| `rows_estimated` / `rows_actual` / `skew_ratio` | planner estimate vs reality |
| `buffers_hit` / `buffers_read` | 8KB pages from cache / disk |
| `loops` / `parallel_aware` | parallel execution detail |

### Repository layout

```
demoapp/          FastAPI shop (traffic source)
planspan/         the sidecar
  sidecar.py      entrypoint: wires tail loop + lock poller + what-if runner
  parser/         auto_explain JSON -> span-tree IR
  emitter/        IR -> OTel spans
  logreader.py    tail + multiline plan assembly
  traceparent.py  shared traceparent parse/extract helpers
  scrub.py        optional literal redaction (SCRUB_LITERALS)
  lockpoller/     lock forensics
  whatif/         hypopg what-if plans
  fingerprint/    plan-shape hashing
  billing/        IO + $ math, priced from pg_stat_statements call rate
deploy/
  casting.yaml[.lock]    Foundry install config for SigNoz
  docker-compose.yml     demoapp + sidecar (host network)
  postgres/              auto_explain config + setup.sh
  signoz/dashboards/     importable dashboard JSON
  signoz/alerts/         importable alert JSON
mcp/                     SigNoz MCP client, on-demand + alert-triggered diagnosis
tests/            parser, emitter, logreader, lockpoller, whatif, fingerprint,
                  scrub (+ real VPS plan fixture)
```

### Tests

Run the tests with `pytest` from the repo root (`pip install -r tests/requirements.txt`).