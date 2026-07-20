# PlanSpan

Distributed tracing that doesn't stop at the database.

Postgres renders as a single opaque span in every APM: `SELECT ... — 4.2s`. PlanSpan
continues the trace *inside* Postgres — each plan node (Seq Scan, Hash Join, Sort)
becomes a real OpenTelemetry span, parented under the HTTP request that ran it, and
rendered in SigNoz.

Built for the Agents of SigNoz hackathon (Track 3). **WIP.**

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

## Layout

```
demoapp/          FastAPI shop (traffic source)
planspan/         the sidecar
  parser/         auto_explain JSON -> span-tree IR
  emitter/        IR -> OTel spans
  lockpoller/     lock forensics
  whatif/         hypopg what-if plans
  fingerprint/    plan-shape hashing
  billing/        IO + $ math
deploy/           docker-compose + postgres setup
```
