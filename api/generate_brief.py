"""
Vercel serverless function backing the dashboard's "Generate Brief" button.

This is the real trigger: the button calls this endpoint, which runs the
exact same run_account_brief() pipeline from agent_account_brief.py —
retrieve -> quality gate -> assemble prompt -> call Claude -> validate
grounding -> respond. The Anthropic API key lives only here, as a Vercel
environment variable — it is never sent to the browser.

GET /api/generate_brief?account=<name>&constraint=<optional>
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_account_brief import run_account_brief

VALID_CONSTRAINTS = {"champion_arming", "save_play", "expansion_push"}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        account = (query.get("account", [""])[0] or "").strip()
        constraint = query.get("constraint", [None])[0]
        if constraint not in VALID_CONSTRAINTS:
            constraint = None

        if not account:
            self._send(400, {"error": "missing required query param: account"})
            return

        try:
            result = run_account_brief(account, constraint=constraint)
        except ValueError as e:
            self._send(404, {"error": str(e)})
            return
        except Exception as e:
            self._send(502, {"error": f"agent call failed: {e}"})
            return

        self._send(200, result)

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)
