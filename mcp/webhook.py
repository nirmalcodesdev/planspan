"""Alert-triggered auto-diagnosis: the other half of Act 5.

SigNoz's alertmanager POSTs here when the plan-flip (or IO-cost) alert fires.
On a firing alert, runs the same diagnosis diagnose.py does on demand, so the
migration is sitting in a PR before anyone asks — the on-call-assistant story
from idea.md, not just Q&A-on-demand.

Stdlib only, no framework: this is a tiny, occasionally-invoked listener, not a
service worth a dependency.

  WEBHOOK_HOST   default 0.0.0.0
  WEBHOOK_PORT   default 8010
  WEBHOOK_PATH   default /alert
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from diagnose import run_diagnosis

PATH = os.environ.get("WEBHOOK_PATH", "/alert")


def _is_firing(payload: dict) -> bool:
    """Alertmanager sends {"status": "firing"|"resolved", "alerts": [...]}."""
    if payload.get("status") == "firing":
        return True
    return any(a.get("status") == "firing" for a in payload.get("alerts", []))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        # respond immediately — alertmanager expects a fast ack, diagnosis runs after
        self.send_response(200)
        self.end_headers()

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}

        if self.path.split("?")[0] != PATH:
            return
        if not _is_firing(payload):
            print(f"webhook: non-firing payload ({payload.get('status', 'unknown')}), skipping", flush=True)
            return

        threading.Thread(target=self._diagnose, daemon=True).start()

    def _diagnose(self):
        print("webhook: alert firing, running diagnosis", flush=True)
        try:
            result = run_diagnosis()
        except Exception as e:  # noqa: BLE001 - keep listener alive
            print(f"webhook: diagnosis failed: {e}", flush=True)
            return
        if result:
            print(f"webhook: wrote {result['file']}", flush=True)
        else:
            print("webhook: nothing to diagnose", flush=True)

    def log_message(self, fmt, *args):
        print(f"webhook: {fmt % args}", flush=True)


def serve():
    host = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBHOOK_PORT", "8010"))
    server = HTTPServer((host, port), Handler)
    print(f"webhook listener up on {host}:{port}{PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()
