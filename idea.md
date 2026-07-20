# PlanSpan
### Distributed tracing that doesn't stop at the database.


Postgres's query planner is a data source nothing can observe today — I bridged it into SigNoz" 

**Track 3 · Agents of SigNoz Hackathon · build week July 20–26, 2026 · solo-shipable**

---

## 0. Vision

Every APM on earth renders your database as a single opaque span: `SELECT … — 4.2s`. The trace ends exactly where the problem begins.

**PlanSpan continues the trace *inside* Postgres.** The query's execution plan becomes a subtree of real OpenTelemetry spans — every Seq Scan, Hash Join, and Sort with true timings, row estimates vs reality, and buffer I/O — parented under the exact HTTP request that ran it. Then it goes where no tool has gone:

- it traces the plan you **could have had** (what-if universes),
- it links you to the **other request that blocked you** (lock forensics),
- it prices every query in **dollars**,
- and it makes all of it legible to an **AI agent through SigNoz MCP**.

**One sentence for judges:** *"The trace doesn't just show you the plan — it shows the plan you could have had, and the request that blocked you. Built on nothing but contrib `auto_explain`, so it works on the RDS instance you're not allowed to touch."*

Positioning: **APM for the query planner.** Not a Postgres metrics dashboard (done a thousand times). Not a plan visualizer (pev2 exists). A missing layer of the distributed trace.

---

## 1. Why this wins — strategy before features

1. **The demo cannot flake.** Everything runs in one docker-compose: drop an index live → alert fires → red 4M-row Seq Scan → sibling what-if trace shows the 52× fix → AI explains it. No external APIs, no network dependency, no LLM in the data path.
2. **SigNoz is structurally irreplaceable.** The trace waterfall *is* the product surface. Port this to plain logs and the project ceases to exist.
3. **Two features exist in no tool at any price** — alternative plans as sibling traces, and lock-victim → culprit-trace navigation.
4. **Core is done by Day 3.** Everything after is layered wow with explicit kill-switches. Finished-and-polished beats ambitious-and-broken on every real judging panel. Deliberately deferred past the hackathon: forced-plan re-runs, always-on wait-event sampling, plan-contracts CI, autovacuum cause-correlation, ERROR-span surfaces.
5. **The submission is a blog** — and this idea produces its own pull quotes: *"1.9 GB read to return 12 rows," "blocked by trace #4832," "the trace your query could have had."*

---

## 2. The product — five acts

Each act answers the next question an engineer asks during a real incident. Each act is one screenshot in the blog.

### Act 1 — SEE · *"What did my query actually do?"*
`auto_explain` (contrib module: `log_format=json`, `log_analyze`, `log_buffers`, `log_min_duration=200ms`) logs every slow query's plan → sidecar tails the JSON log → **each plan node becomes an OTel span** with per-node timing, `est_rows` vs `actual_rows` (`skew_ratio` flags stale statistics), buffers hit/read, index name, filter clause.

**The stitch that changes everything:** a sqlcommenter-style `traceparent` comment injected into SQL (20-line SQLAlchemy event hook) survives into the auto_explain log → the plan subtree is parented **under the live application trace**. Click from a slow `/checkout` request straight down into the Seq Scan that caused it.

Trace Explorer becomes a **plan search engine**: *"all Seq Scans > 1s on table `orders`, last 24h"* — a query no Postgres tool can answer today.

### Act 2 — EXPLAIN · *"Why was it slow — and who did it?"*
- **Lock forensics — `db.blocked_by.trace_id`:** a lightweight poller watches `pg_stat_activity` for sessions stuck on locks (`wait_event_type = 'Lock'`) and calls `pg_blocking_pids()`. The blocking session's SQL carries *its own* traceparent comment → one regex → the victim's trace links to the **culprit's trace**, with a `Lock:relation orders (1.8s)` span event. Click from "checkout stuck 2s" to *the other user's request holding the lock*. Graceful fallback to `blocked_by_pid` + query text.
- **The IO story comes free:** buffers hit/read already sit on every plan node — *"read 1.9 GB from disk"* needs no extra machinery.

### Act 3 — DIAGNOSE CHANGE · *"What changed, and when?"*
- **Plan fingerprinting:** hash of plan structure per queryid, emitted as a metric. Planner flips index scan → seq scan: **alert fires before users tweet.**
- The regressed trace carries `plan.last_good_trace_id` (one click to the before/after) and a human diff in the alert body: *"Index Scan(orders_email_idx) → Seq Scan(orders); +4.1s."*

### Act 4 — FIX · *"What should I do — and what's it worth?"*
- **Parallel-universe plans:** on a slow query, PlanSpan asks `hypopg` *"what if the index existed?"*, runs `EXPLAIN`, and emits the hypothetical plan as a **sibling span subtree under the same request** — one waterfall, two universes side by side. Spans marked `simulated=true`, durations scaled by planner cost ratio; EXPLAIN-only, nothing is re-executed. (hypopg is on the RDS allowlist — the managed-Postgres story holds.)
  - Verdict on the span: `whatif.speedup=52x`, `whatif.ddl="CREATE INDEX CONCURRENTLY …"` — **the fix is copy-pasteable from the trace.**
  - Cheap to build: what-if output re-enters the *same* parser → emitter pipeline as the logs.
- **The Query Bill:** `io_amplification = buffers_read × 8KB ÷ rows_returned` (*"1.9 GB to return 12 rows"*) and $/month at observed call rate from pg_stat_statements (*"this missing index costs $214/month"* — labeled estimate, tunable to RDS pricing). Dashboard: **Top 10 Most Expensive Queries, in dollars.** Impact criterion, priced in the currency judges' managers understand.

### Act 5 — AUTOMATE · *"Don't wake me up."*
- **Ask your flamegraph (SigNoz MCP):** Claude connected to self-hosted SigNoz via MCP reads the plan spans — skew 1,000,000×, seq scan, 1.9 GB read — answers *"why is /checkout slow?"* with the diagnosis and the DDL, **citing the what-if sibling trace as its verification**. Diagnose → simulate → recommend, grounded entirely in SigNoz data. This is the hackathon's AI thesis made literal: *PlanSpan gives the database an inner monologue that agents can read.* Fallback: SigNoz Query API + Claude script — demo-day feature, zero risk to the pipeline.

Trigger it from the alert, as well as from a human typing a question. When the plan-fingerprint or IO-cost alert fires, the agent runs automatically and drops a written diagnosis before anyone asks — Q&A-on-demand is a demo gimmick; alert-triggered auto-diagnosis is an actual on-call assistant.
Make its output an artifact, not a paragraph. Instead of "here's what's wrong," have it emit the actual CREATE INDEX CONCURRENTLY ... as a ready-to-review migration file/PR diff, using the exact table/column it already detected in Act 4's what-if run. Verified fix, not narrated fix.

---

## 3. Architecture

```
 FastAPI demo shop ──(traceparent SQL comments)──▶  Postgres 16
   │  OTLP app traces                                 │ jsonlog: auto_explain plans,
   │                                                  │ deadlocks, autovacuum, DDL
   ▼                                                  ▼
 ┌─────────────────────────  SigNoz  ─────────────────────────────┐
 │  traces · metrics · logs · dashboards · alerts · MCP           │
 └───────▲──────────────────────▲─────────────────────▲───────────┘
         │ OTLP (plan spans,    │ OTLP (raw logs,     │ queries
         │ what-if universes)   │ pg_stat metrics)    │
 ┌───────┴────────────┐  ┌──────┴───────────┐  ┌──────┴──────────┐
 │ planspan sidecar   │  │ OTel Collector    │  │ Claude via MCP  │
 │ tail→parse→enrich  │  │ filelog receiver  │  │ "why is checkout│
 │ +lock poller       │  │ postgres receiver │  │  slow?"         │
 │ +what-if runner    │  └──────────────────┘   └─────────────────┘
 └────────────────────┘
```

**Components, sized for AI-agent codegen** (each is a small contract with tests an agent can crank):

| Component | Job | ~Size |
|---|---|---|
| `parser/` | pure function: auto_explain JSON → span-tree IR (per-loop math, top-10 node types) | 400 LOC + golden tests |
| `emitter/` | IR → OTel spans; backdated timestamps; synthetic layout | 150 LOC |
| `lockpoller/` | watches pg_stat_activity for blocked sessions → pg_blocking_pids → `blocked_by_trace_id` | 100 LOC |
| `whatif/` | hypopg EXPLAIN runner (no execution) → parser | 120 LOC |
| `fingerprint/` | plan-shape hash + human-readable diff | 120 LOC |
| `billing/` | IO amplification + $/month math | 80 LOC |
| `demoapp/` | FastAPI + SQLAlchemy shop, 10M-row seed, degradable scenarios | 300 LOC |
| `deploy/` | docker-compose (PG16 + SigNoz + Collector); **1 dashboard + 2 alerts as importable JSON** | config |

**Total: ~1,270 LOC** — a comfortable 7-day solo build with AI coding agents writing modules against golden tests.

**Stack:** Postgres 16 (`auto_explain`, `jsonlog`, `hypopg`, `pg_stat_statements` — all available on RDS), Python sidecar (opentelemetry-sdk, explicit timestamps), OTel Collector contrib, self-hosted SigNoz, one-command quickstart.

**Semantic conventions:** spans follow OTel `db.*` semconv and extend it with a proposed `db.postgresql.plan.*` namespace — the blog frames this as a de-facto semconv RFC. Standards-mindedness reads as expertise to observability judges, and costs nothing.

---

## 4. Honest engineering (in the blog, out loud — this preempts expert judges)

1. **The waterfall is a cost-map, not a timeline.** Postgres's iterator model interleaves node execution; EXPLAIN gives inclusive durations, not start offsets. Layout: parent-start = child-start, siblings by exclusive time, duration = `actual_time × loops`. "Widest bar = most expensive node" still holds — stated as a design decision, not buried.
2. **Backdated timestamps:** plan spans are stamped to actual execution time (log ts − duration) so the subtree aligns under its parent request.
3. **Day-1 load-bearing check:** traceparent comments must survive into auto_explain output (they do; `pg_stat_statements` strips comments — which is why queryid and traceparent are deliberately *separate* correlation channels).
4. **Overhead is governed and published:** `log_min_duration=200ms` + `auto_explain.sample_rate`; measured overhead number goes in the blog.
5. **Descoped and stated:** per-worker attribution for parallel query, exotic node types; what-ifs are EXPLAIN-only (nothing re-executed).
6. **`--scrub-literals` flag:** filter values normalized out of span attributes (PII safety in one line).

