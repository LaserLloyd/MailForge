"""Container HEALTHCHECK for MailForge.

GET /healthz returns 200 only when every mailbox listener is healthy and 503
("degraded") otherwise - including on a fresh install with no mailbox yet. A
container is "up" as long as the server answers and the database does, so this
accepts both 200 and 503 and fails on anything else (unreachable, non-JSON, or
db != ok). Per-mailbox state is for monitoring, not for restarting the container.
"""
import json
import os
import sys
import urllib.error
import urllib.request

port = os.environ.get("MAILFORGE_UI_PORT", "8090")
url = f"http://127.0.0.1:{port}/healthz"
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        resp = opener.open(url, timeout=4)
        code, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read()
    body = json.loads(raw)
except Exception:
    sys.exit(1)
sys.exit(0 if code in (200, 503) and body.get("db") == "ok" else 1)
