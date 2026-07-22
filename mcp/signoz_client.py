"""Talk to SigNoz through the MCP server (the same one Claude uses).

The diagnosis script drives the official SigNoz MCP server over HTTP JSON-RPC,
calling signoz_execute_builder_query with a Query Builder v5 payload. This is the
on-thesis path: PlanSpan's automation reads the plan spans the exact way an AI
agent would through MCP.

Config:
  MCP_URL   default http://localhost:8009/mcp
"""
import json
import os
import time
import urllib.request


class MCPError(RuntimeError):
    pass


class MCP:
    def __init__(self, url=None):
        self.url = url or os.environ.get("MCP_URL", "http://localhost:8009/mcp")
        self._id = 0

    def call(self, name, arguments):
        self._id += 1
        req = {
            "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        r = urllib.request.Request(
            self.url,
            data=json.dumps(req).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
        )
        raw = urllib.request.urlopen(r, timeout=45).read().decode()
        raw = raw.split("data: ")[-1] if "data:" in raw else raw
        resp = json.loads(raw)
        if "error" in resp:
            raise MCPError(resp["error"].get("message", "mcp error"))
        content = resp["result"]["content"][0]["text"]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            raise MCPError(content)
        if isinstance(parsed, dict) and parsed.get("status") == "error":
            raise MCPError(json.dumps(parsed))
        return parsed

    def raw_traces(self, filter_expr: str, select_fields: list, minutes: int = 60, limit: int = 20):
        """requestType 'raw': individual spans matching filter_expr, with the
        given custom attributes projected via selectFields."""
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - minutes * 60 * 1000
        query = {
            "schemaVersion": "v1",
            "start": start_ms, "end": now_ms,
            "requestType": "raw",
            "compositeQuery": {
                "queries": [{
                    "type": "builder_query",
                    "spec": {
                        "name": "A", "signal": "traces", "disabled": False,
                        "limit": limit, "offset": 0,
                        "order": [{"key": {"name": "timestamp"}, "direction": "desc"}],
                        "having": {"expression": ""},
                        "filter": {"expression": filter_expr},
                        "selectFields": select_fields,
                    },
                }],
            },
            "formatOptions": {"formatTableResultForUI": False, "fillGaps": False},
            "variables": {},
        }
        return self.call("signoz_execute_builder_query", {"query": query})


WHATIF_FIELDS = [
    {"name": "whatif.ddl", "fieldDataType": "string", "signal": "traces", "fieldContext": "tag"},
    {"name": "whatif.speedup", "fieldDataType": "number", "signal": "traces", "fieldContext": "tag"},
    {"name": "db.postgresql.plan.relation", "fieldDataType": "string", "signal": "traces", "fieldContext": "tag"},
    {"name": "trace_id", "fieldDataType": "string", "signal": "traces", "fieldContext": "span"},
    {"name": "name", "fieldDataType": "string", "signal": "traces", "fieldContext": "span"},
]
