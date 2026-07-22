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

## Alert-triggered auto-diagnosis

`diagnose.py` also runs itself when an alert fires — the on-call-assistant half of
Act 5. `webhook.py` is a tiny stdlib listener that SigNoz's alertmanager POSTs to;
on a firing alert it runs the same diagnosis in the background, so the migration
lands before anyone asks.

```bash
python webhook.py          # listens on 0.0.0.0:8010/alert
```

Wire it to SigNoz once:

1. **Notification channel** → point a webhook channel at the listener. SigNoz runs
   in its own bridge network, so use the bridge gateway, not `localhost`:
   `http://<bridge-gateway>:8010/alert` (find it with
   `docker network inspect signoz-network -f '{{(index .IPAM.Config 0).Gateway}}'`).
2. **Firewall** → allow the port from that subnet only:
   `sudo ufw allow from <subnet> to any port 8010 proto tcp`.
3. **Attach** the channel to the plan-flip alert (its `channels` list).

Now drop an index, wait for the flip alert to fire, and the migration appears with
no human in the loop. Env: `WEBHOOK_HOST`/`WEBHOOK_PORT`/`WEBHOOK_PATH`.
