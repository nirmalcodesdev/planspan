# PlanSpan

Distributed tracing that doesn't stop at the database.

Postgres renders as a single opaque span in every APM: `SELECT ... — 4.2s`. PlanSpan
continues the trace *inside* Postgres — each plan node (Seq Scan, Hash Join, Sort)
becomes a real OpenTelemetry span, parented under the HTTP request that ran it, and
rendered in SigNoz.

Built for the Agents of SigNoz hackathon (Track 3).

**Five acts:** [See](#see-the-plans) the plan as a trace · [Explain](#lock-forensics)
who blocked you · [Diagnose](#plan-fingerprinting) what changed · [Fix](#what-if-plans)
it with a verified migration · [Automate](#ai-diagnosis-signoz-mcp) with an agent that
reads the trace.

## How it fits together

Three moving parts, deliberately separate:

- **SigNoz** — self-hosted via Foundry, runs as its own docker stack. We just target its
  OTLP endpoint (`localhost:4317`).
- **Postgres 17** — native systemd cluster (not a container). `auto_explain` logs slow
  query plans as JSON.
- **PlanSpan** (this repo) — a sidecar that tails the auto_explain log, turns each plan
  node into an OTel span, and stitches the subtree under the live app trace via a
  `traceparent` SQL comment. Plus a FastAPI demo shop to generate the traffic.

## Setup (VPS)

Assumes SigNoz is already up (Foundry) and you've completed the signup page at
`http://<vps-ip>:8080/signup` — until you do, SigNoz silently drops spans.

```bash
cp .env.example .env         # edit PG_PASSWORD etc.
sudo PG_DB=shop PG_USER=planspan PG_PASSWORD=... ./deploy/postgres/setup.sh
cd deploy && docker compose up --build -d
```

`setup.sh` installs hypopg, drops the auto_explain config into
`/etc/postgresql/17/main/conf.d/`, restarts Postgres, and creates the app db/role.

Seed the shop (10M orders, server-side, idempotent):

```bash
docker compose exec demoapp python seed.py       # or ORDERS=2000000 for a smaller set
```

Endpoints:

- `GET /orders?email=user123@example.com` — indexed lookup; drop `ix_orders_email`
  live to watch it fall to a seq scan
- `GET /search` — unindexed aggregate over all orders
- `POST /checkout?user_id=1&product_id=1` — writes an order

## See the plans

Hit a slow endpoint, then open SigNoz Traces (`http://<vps-ip>:8080`):

```bash
curl "http://localhost:8000/search"
```

Find the `shop-api` `/search` request and expand the waterfall. Under the HTTP span
you'll see the plan subtree — `Aggregate` → `Gather Merge` → `Sort` → `Seq Scan orders`
— each a real span carrying rows, buffers, loops, and estimate-vs-actual skew.

The sidecar logs one line per captured plan:

```
emitted 5 spans  dur=882.0ms  tp=00-3272814e9e37...-d7b88a3d78f2...-01
```

`tp=00-...` is the trace it stitched under; `no-parent` means the query had no
traceparent (e.g. a background query, not an app request).

### Trace Explorer as a plan search engine

Because every plan node is a span with `db.postgresql.plan.*` attributes, Trace
Explorer answers questions no Postgres tool can — e.g. filter
`db.postgresql.plan.node_type = Seq Scan` and `db.postgresql.plan.relation = orders`
to list every sequential scan on `orders` in the window, slowest first.

### Dashboard

Import `deploy/signoz/dashboards/query-plans.json` (Dashboards → Import JSON):
slowest plan nodes, seq scans by relation, node time by type, row skew, buffers read.

## What-if plans

When a slow query does a Seq Scan with a filter, the sidecar asks `hypopg` what the
plan *would* be if the matching index existed — `EXPLAIN` only, nothing is executed.
If the planner would use it, PlanSpan emits the hypothetical plan as a **sibling span
subtree under the same request**, marked `db.postgresql.plan.simulated=true`, with:

- `whatif.speedup` — planner cost ratio (baseline / hypothetical)
- `whatif.ddl` — `CREATE INDEX CONCURRENTLY ...`, copy-pasteable from the trace

So one waterfall shows two universes: the plan you have and the plan you could have.
Reproduce by dropping the index and hitting the endpoint:

```bash
psql "$CONN" -c "DROP INDEX ix_orders_email"
curl "http://localhost:8000/orders?email=user4242@example.com"
```

The `/orders` trace gets a `[what-if] Index Scan orders` sibling with the speedup and
DDL. Disable the runner with `WHATIF=off`.

## Query billing

`planspan/billing` turns plan facts into cost: `io_amplification` (bytes read per row
returned — the "1.9 GB to return 12 rows" story) and `dollars_per_month` (CPU time ×
observed call rate × a tunable vCPU price). Estimates, labeled as such.

## Plan fingerprinting

Every plan is hashed by its shape (node types + relations/indexes, ignoring timings).
When the same query's fingerprint changes — an Index Scan falling back to a Seq Scan —
the sidecar emits a `Plan flip` span with a human diff
(`Index Scan[ix_orders_email] -> Seq Scan[orders]`) and `last_good_trace_id` for the
before. Import the alerts in `deploy/signoz/alerts/` (plan flip, high-IO seq scan) to
get paged before users notice. The flip demo needs `auto_explain.log_min_duration = 0`
so the fast (indexed) baseline plan is also logged.

## AI diagnosis (SigNoz MCP)

Two ways in, same data, same fix — see `mcp/README.md` for setup:

- **A human types a question.** Claude connects to SigNoz via the official MCP
  server and reads the plan spans directly. Ask "why is /orders slow?" and it cites
  the what-if sibling as verification, distinguishing a real missing index from a
  planner just picking the wrong plan.
- **An alert fires.** `mcp/webhook.py` listens for SigNoz's alertmanager POST and
  runs the same diagnosis automatically — no LLM in the data path required.
  `mcp/diagnose.py` pulls the biggest recent what-if win through MCP and writes a
  ready-to-review `CREATE INDEX CONCURRENTLY` migration file: a verified fix, not
  a narrated one, sitting there before anyone asks.

## Lock forensics

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

## Span attributes

Spans follow the OTel `db.*` semconv and extend it with a proposed
`db.postgresql.plan.*` namespace:

| attribute | meaning |
|---|---|
| `node_type` | Seq Scan, Index Scan, Hash Join, ... |
| `relation` / `index_name` | table / index touched |
| `total_ms` / `self_ms` | inclusive / exclusive time (× loops) |
| `rows_estimated` / `rows_actual` / `skew_ratio` | planner estimate vs reality |
| `buffers_hit` / `buffers_read` | 8KB pages from cache / disk |
| `loops` / `parallel_aware` | parallel execution detail |

## Layout

```
demoapp/          FastAPI shop (traffic source)
planspan/         the sidecar
  parser/         auto_explain JSON -> span-tree IR
  emitter/        IR -> OTel spans
  logreader.py    tail + multiline plan assembly
  lockpoller/     lock forensics
  whatif/         hypopg what-if plans
  fingerprint/    plan-shape hashing
  billing/        IO + $ math
deploy/
  docker-compose.yml     demoapp + sidecar (host network)
  postgres/              auto_explain config + setup.sh
  signoz/dashboards/     importable dashboard JSON
  signoz/alerts/         importable alert JSON
mcp/                     SigNoz MCP client + auto-diagnosis script
tests/            parser / emitter / logreader (+ real VPS plan fixture)
```

Run the tests with `pytest` from the repo root (`pip install -r tests/requirements.txt`).
