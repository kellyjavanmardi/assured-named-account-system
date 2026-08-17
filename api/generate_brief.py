"""
Vercel serverless function backing the dashboard's "Generate Brief" button.

This is the real trigger: the button calls this endpoint, which runs the
exact same run_account_brief() pipeline from agent_account_brief.py --
retrieve -> quality gate -> assemble prompt -> call Claude -> validate
grounding -> respond. The Anthropic API key lives only here, as a Vercel
environment variable -- it is never sent to the browser.

GET /api/generate_brief?account=<name>&constraint=<optional>

A plain WSGI app (not a framework) -- this is the callable Vercel's
Services entrypoint (vercel.json: services.api.entrypoint) loads.
"""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_account_brief import run_account_brief

VALID_CONSTRAINTS = {"champion_arming", "save_play", "expansion_push"}

REASON_PHRASES = {200: "OK", 400: "Bad Request", 404: "Not Found", 502: "Bad Gateway"}


def handler(environ, start_response):
    query = parse_qs(environ.get("QUERY_STRING", ""))
    account = (query.get("account", [""])[0] or "").strip()
    constraint = query.get("constraint", [None])[0]
    if constraint not in VALID_CONSTRAINTS:
        constraint = None

    if not account:
        return _respond(start_response, 400, {"error": "missing required query param: account"})

    try:
        result = run_account_brief(account, constraint=constraint)
    except ValueError as e:
        return _respond(start_response, 404, {"error": str(e)})
    except Exception as e:
        return _respond(start_response, 502, {"error": f"agent call failed: {e}"})

    return _respond(start_response, 200, result)


def _respond(start_response, status: int, body: dict):
    payload = json.dumps(body).encode("utf-8")
    status_line = f"{status} {REASON_PHRASES.get(status, 'Error')}"
    headers = [
        ("Content-Type", "application/json"),
        ("Cache-Control", "no-store"),
        ("Content-Length", str(len(payload))),
    ]
    start_response(status_line, headers)
    return [payload]
