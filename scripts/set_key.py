"""
Validate a Deepgram key against Deepgram, then write it to .env.

Checking the key BEFORE it goes in the config means a typo shows up here,
with a clear message, instead of surfacing later as a worker that starts
and silently transcribes nothing.

Usage:  python3 scripts/set_key.py <your-key>
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ENV = os.path.join(ROOT, ".env")

if len(sys.argv) != 2:
    raise SystemExit("usage: python3 scripts/set_key.py <deepgram-api-key>")

key = sys.argv[1].strip()

if len(key) < 20:
    raise SystemExit(f"That looks too short to be an API key ({len(key)} chars). "
                     "Copy the whole value from the Deepgram console.")

print("Checking the key with Deepgram...")
req = urllib.request.Request(
    "https://api.deepgram.com/v1/projects",
    headers={"Authorization": "Token " + key},
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        projects = json.loads(r.read()).get("projects", [])
except urllib.error.HTTPError as e:
    if e.code == 401:
        raise SystemExit(
            "Deepgram rejected this key (401).\n"
            "  - Check you copied the whole thing, with no trailing space\n"
            "  - A key is shown only once; if you lost it, delete it and make a new one")
    raise SystemExit(f"Deepgram returned HTTP {e.code}")
except Exception as e:
    raise SystemExit(f"Could not reach Deepgram: {e}")

print(f"  valid — {len(projects)} project(s): "
      + ", ".join(p.get("name", "?") for p in projects))

# Write it in, replacing any existing line.
lines = open(ENV).read().splitlines()
found = False
for i, l in enumerate(lines):
    if l.startswith("DEEPGRAM_API_KEY="):
        lines[i] = "DEEPGRAM_API_KEY=" + key
        found = True
if not found:
    lines.append("DEEPGRAM_API_KEY=" + key)
open(ENV, "w").write("\n".join(lines) + "\n")

print(f"  written to .env (gitignored — it will not be committed)")
print("\nNext:")
print("  docker compose up -d agent")
print("  python3 scripts/verify_stt.py")
