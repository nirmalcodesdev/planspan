# PlanSpan + SigNoz MCP

Act 5: give the database an inner monologue an agent can read.

## Run the MCP server

```bash
docker run -d --name planspan-mcp --network host \
  -e TRANSPORT_MODE=http \
  -e MCP_SERVER_PORT=8009 \
  -e MCP_SERVER_HOST=127.0.0.1 \
  -e SIGNOZ_URL=http://localhost:8080 \
  -e SIGNOZ_API_KEY=<service-account-key> \
  signoz/signoz-mcp-server:latest
```

Get the API key from SigNoz UI: **Settings → Service Accounts** → create one, attach
the `signoz-admin` (or `signoz-viewer`) role, add a key. Admin-created keys only.

## Ask Claude directly

Point Claude Code or Claude Desktop at the running server:

```bash
claude mcp add --scope user --transport http signoz http://localhost:8009/mcp
```

Or in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "signoz": { "url": "http://localhost:8009/mcp" }
  }
}
```

Then ask: *"why is /orders slow?"* — Claude searches traces, reads the plan spans
(`db.postgresql.plan.*`), and can cite the what-if sibling (`whatif.ddl`,
`whatif.speedup`) as its verification.

## Automated diagnosis

`diagnose.py` is the zero-risk fallback from idea.md: no LLM required in the data
path. It queries the MCP server the same way an agent would, finds the biggest
recent what-if win, and writes a ready-to-review migration:

```bash
pip install -r requirements.txt   # only needed for --with Claude prose
python diagnose.py --minutes 60
```

Output: `migrations/add_index_<relation>.sql` — the exact `whatif.ddl` PlanSpan
already verified against the plan, not a paragraph. Set `ANTHROPIC_API_KEY` to
also get a short Claude-written diagnosis citing the trace; without it, a
template narrative is used instead.

Wire this to the plan-flip alert's webhook (`deploy/signoz/alerts/plan-flip.json`)
for on-call auto-diagnosis: the migration lands before anyone asks.
