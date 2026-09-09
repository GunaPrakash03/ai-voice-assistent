"""
Local test harness for task 1.1.

Serves the browser test page and mints join tokens for it. Stdlib only —
no pip install. This is a development harness, not the production token
path: in production Drupal mints the token (see BACKEND-FRONTEND-STACK).
"""

import json
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from agent.token import join_token  # noqa: E402

# Default port is 8091; override with:
#   python3 scripts/serve.py <port>
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
WEB = os.path.join(ROOT, "web")


def env(name):
    for line in open(os.path.join(ROOT, ".env")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} missing from .env")


KEY, SECRET = env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET")
WS_URL = env("LIVEKIT_URL")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/token":
            return super().do_GET()

        q = parse_qs(parsed.query)
        room = (q.get("room") or ["test-room"])[0]
        identity = (q.get("identity") or q.get("user") or ["caller"])[0]

        # Development harness only. A real endpoint authenticates the
        # visitor and rate-limits before minting anything.
        body = json.dumps({
            "url": WS_URL,
            "room": room,
            "identity": identity,
            "token": join_token(KEY, SECRET, room, identity),
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        first_arg = str(args[0]) if args else ""
        if "/token" in first_arg:
            sys.stderr.write("  token issued\n")


print(f"Test page:  http://localhost:{PORT}")
print(f"Signalling: {WS_URL}")
print("Ctrl+C to stop\n")
try:
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
except OSError as e:
    raise SystemExit(f"Port {PORT} is in use ({e}). Pass another: "
                     f"python3 scripts/serve.py 9090")
except KeyboardInterrupt:
    print("\nstopped")
