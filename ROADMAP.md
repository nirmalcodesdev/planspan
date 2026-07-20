# PlanSpan — Build Roadmap

Solo build, July 20–26 2026. SigNoz hackathon Track 3.
Order is PR-by-PR. Each PR = one meaningful, self-contained chunk. Ship core (PR 1–5) before reach (PR 6–10).

**Commit discipline:** space commits out, group by logical unit, plain human messages, no attribution trailers, no AI-looking code. See memory `feedback_human_commits`.

**Deploy target:** dev on Windows, tested + demo-recorded on a **Linux VPS**. Dev loop = copy repo to VPS, run there (no local stack on Windows). Everything portable: no hardcoded `localhost`, env vars everywhere, `.env.example` committed / `.env` gitignored.

**VPS actuals (already provisioned):**
- Hostinger, Ubuntu 25.04, 4 vCPU, 15 GiB RAM, 200 GB disk (~155 GB free). Plenty — full 10M-row seed OK.
- Docker 29.2.1, Compose v5.0.2.
- **SigNoz** self-hosted via **Foundry** (replaced install.sh as of SigNoz v0.130.0). Runs as its own docker stack, own network.
- **PostgreSQL 17** via apt — **native systemd cluster, NOT docker**. Config at `/etc/postgresql/17/main/postgresql.conf`, log at `/var/log/postgresql/postgresql-17-main.log`, listens `:5432` on host.
- **hypopg NOT installed** — needs `sudo apt install postgresql-17-hypopg` before Act 4 (PR 7). Verify apt has it for 25.04; else build from source.

**Architecture consequence — 3 separate pieces, not one compose:**
1. SigNoz — Foundry-managed docker stack (don't touch, just target its OTLP `:4317`/`:4318`, UI `:8080`).
2. Postgres 17 — native systemd. Configured via manual `postgresql.conf` edits (documented in README + a setup script), not a container.
3. **Our compose** (`deploy/docker-compose.yml`) owns only: `demoapp` + `planspan` sidecar (+ optional extra collector).

**Cross-boundary wiring (RESOLVED from user's blog — verified working):**
- Sidecar/demoapp/collectors use `--network host` and reach SigNoz OTLP at `localhost:4317` (grpc, `insecure: true`). No host-gateway gymnastics.
- Native PG reachable at `localhost:5432` from host-network containers.
- Sidecar tails PG log by mounting `/var/log/postgresql:/var/log/postgresql:ro`.
- All as env vars (`PG_HOST`, `PG_LOG_PATH`, `OTLP_ENDPOINT`) — no hardcoding.

**⚠ SIGNOZ SILENT-FAILURE GOTCHA (cost user ~1hr):** ingester shows healthy in `docker ps` but SILENTLY DROPS ALL SPANS until first-run signup completed at `http://<host>:8080/signup` (OpAMP "cannot create agent without orgId" → zero receivers). If PlanSpan spans don't appear, check signup FIRST.

**Reuse existing VPS infra (already running, don't rebuild):**
- PG `monitoring` user (`pg_monitor` + grants). NOTE: pg_monitor does NOT grant table reads — needs `GRANT SELECT ON ALL TABLES IN SCHEMA public`. Same trap for demoapp.
- `otel-collector-postgres` container already tails logs + scrapes pg metrics → keep for metrics/raw-logs. PlanSpan sidecar owns its OWN tail loop (needs full JSON plan tree; filelog regex can't parse it). They coexist.
- ufw already opened 8080/4317/4318/8000.

**NET-NEW work:** auto_explain is NOT yet configured (blog used OTel zero-code instrumentation only). The auto_explain-JSON → plan-span pipeline is PlanSpan's actual contribution.

---

## Phase 0 — Foundation (Day 1)

### PR 1 — repo skeleton + PG17 setup + compose base
- `.gitignore` (python, env, docker volumes, logs)
- `README.md` stub (title, one-liner, "WIP") + **VPS setup section**
- `deploy/postgres/setup.sh` + `postgresql.conf` snippet — manual native-PG17 config (documented, run once on VPS):
  - `shared_preload_libraries = 'auto_explain,pg_stat_statements'`
  - auto_explain: `log_format=json`, `log_analyze=on`, `log_buffers=on`, `log_min_duration=200ms`, `log_timing=on`, `sample_rate` tunable
  - `sudo apt install postgresql-17-hypopg` + `CREATE EXTENSION hypopg; CREATE EXTENSION pg_stat_statements;`
  - `systemctl restart postgresql`
- `deploy/docker-compose.yml` — **our stack only**: `demoapp` + `planspan` sidecar (+ optional collector). NOT SigNoz (Foundry-managed), NOT Postgres (native).
  - `network_mode: host` (matches blog's verified wiring — reach SigNoz `localhost:4317`, PG `localhost:5432`)
  - mount `/var/log/postgresql:/var/log/postgresql:ro` into sidecar
  - env-driven: `PG_HOST`, `PG_LOG_PATH`, `OTLP_ENDPOINT`
- `.env.example` — all config keys, no secrets
- **Exit check (on VPS):** SigNoz UI (`:8080`) loads + **signup done**; native PG accepts connections; auto_explain writes JSON to `/var/log/postgresql/*.log`; a host-network container reaches both PG (`localhost:5432`) and SigNoz OTLP (`localhost:4317`).
- Commits: ~2–3 (skeleton, pg setup script, compose).

**Foundry note:** SigNoz already deployed on VPS via Foundry (v0.130.0+). Old `install.sh`/bundled compose deprecated. We do NOT manage SigNoz — just target it. Verify OTLP health: `curl -v http://localhost:4318/v1/traces` → `405 Method Not Allowed`.

**Self-hosted = no ingestion key.** Confirm sidecar/demoapp can reach SigNoz OTLP from a container (host-gateway or SigNoz network). Resolve this in PR 1 — it's the wiring risk.

**Prior art:** `elessar-ch/sql-tracing` (Gabriel Koch) already builds auto_explain-log → OTel-trace connector. PlanSpan differentiators (what-if siblings, lock forensics, $ billing, MCP auto-diagnosis) stay novel — acknowledge in blog, don't look scooped.

### PR 2 — demo app (demoapp/)
- FastAPI + SQLAlchemy shop (orders, users, products)
- Seed script: 10M rows (chunked, realistic distribution) — VPS has 155GB free, full seed fine. Idempotent, runs in container against native PG.
- Endpoints: `/checkout`, `/orders`, `/search` — at least one degradable (droppable index)
- SQLAlchemy event hook: inject `traceparent` SQL comment (sqlcommenter-style) — Day-1 load-bearing check
- OTLP app traces → SigNoz
- **Exit check (on VPS):** hit `/checkout`, see app trace in SigNoz; confirm traceparent comment lands verbatim in auto_explain JSON log. **This validates the core stitch — do it early.**
- Commits: ~3–4 (app scaffold, models, seed, trace hook).

---

## Phase 1 — Core pipeline / Act 1 SEE (Day 2–3)

### PR 3 — parser/ (pure function, golden tests)
- auto_explain JSON → span-tree IR
- per-loop math (`actual_time × loops`), `est_rows` vs `actual_rows`, `skew_ratio`, buffers hit/read, index name, filter clause
- golden test fixtures (real captured plans)
- **Exit check:** golden tests pass; IR shape stable.
- Commits: ~3 (IR types, parse logic, golden tests).

### PR 4 — emitter/ + sidecar tail loop
- IR → OTel spans; backdated timestamps (log ts − duration); synthetic parent/child layout
- sidecar tails native PG log via mount `/var/log/postgresql/postgresql-17-main.log` (ro) → feeds parser → emitter
- multi-line JSON log entry handling (auto_explain plan spans multiple log lines)
- `traceparent` from comment → parent plan subtree under live app trace (use ended-span-as-parent via context)
- emit → SigNoz OTLP (`OTLP_ENDPOINT` env)
- **Exit check (VPS):** slow query in demoapp → plan spans appear in SigNoz **nested under the HTTP request**. Money shot for Act 1.
- Commits: ~3–4 (emitter, tail loop, stitch, wiring).

### PR 5 — Trace Explorer polish + 1 dashboard
- span attributes follow `db.*` semconv + `db.postgresql.plan.*`
- verify "all Seq Scans > 1s on orders" type query works in Trace Explorer
- `deploy/` importable dashboard JSON (plan search starter)
- README: real quickstart + Act 1 screenshot
- **Exit check:** Act 1 fully demoable end-to-end. **Core done.**
- Commits: ~2–3.

---

## Phase 2 — Reach features (Day 4–5)

### PR 6 — Act 2 EXPLAIN · lock forensics (lockpoller/)
- poller watches `pg_stat_activity` `wait_event_type='Lock'` → `pg_blocking_pids()`
- blocking session's traceparent → `db.blocked_by.trace_id` on victim span + `Lock:relation` span event
- fallback: `blocked_by_pid` + query text
- **Kill-switch:** if flaky, ship IO story alone (buffers already on nodes).
- Commits: ~3.

### PR 7 — Act 4 FIX · what-if (whatif/) + billing/
- **Prereq:** `sudo apt install postgresql-17-hypopg` on VPS + `CREATE EXTENSION hypopg;` (not yet installed — do this before coding PR 7)
- `hypopg` EXPLAIN runner (no execution) → re-enters parser
- sibling span subtree, `simulated=true`, durations scaled by cost ratio
- `whatif.speedup`, `whatif.ddl="CREATE INDEX CONCURRENTLY …"`
- billing/: `io_amplification`, $/month from pg_stat_statements
- dashboard: Top 10 Most Expensive Queries ($)
- **Exit check:** one waterfall, two universes side by side.
- Commits: ~4.

### PR 8 — Act 3 DIAGNOSE · fingerprint (fingerprint/) + alerts
- plan-shape hash per queryid → metric
- `plan.last_good_trace_id`, human diff in alert body
- `deploy/` 2 importable alert JSONs (plan flip, IO cost)
- Commits: ~3.

---

## Phase 3 — AI + polish (Day 6)

### PR 9 — Act 5 AUTOMATE · SigNoz MCP + Claude
- Claude via SigNoz MCP reads plan spans → diagnosis + DDL, cites what-if sibling
- alert-triggered auto-diagnosis (not just Q&A)
- output = migration file / PR diff artifact, not paragraph
- **Fallback:** SigNoz Query API + Claude script (zero risk to pipeline)
- Commits: ~3.

### PR 10 — polish + blog + submission
- `--scrub-literals` flag (PII)
- overhead measurement number for blog
- full README quickstart, all screenshots
- blog draft with pull quotes
- **Exit check:** clean reproduce on VPS — PG17 setup script + `docker compose up` (our stack) against Foundry SigNoz → full 5-act demo runs.
- Commits: ~2–3.

---

## Ordering rule
Core = PR 1–5 (Act 1 fully working). Everything after is layered, each with kill-switch. Finished-and-polished beats ambitious-and-broken.

## Daily target (rough)
- Day 1 (Jul 20): PR 1–2
- Day 2–3 (Jul 21–22): PR 3–5 — **core done Day 3**
- Day 4–5 (Jul 23–24): PR 6–8
- Day 6 (Jul 25): PR 9–10
- Day 7 (Jul 26): buffer, blog finalize, submit
