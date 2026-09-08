"""
Task 1.1 acceptance check.

Proves the three things the task actually promises:
  1. the media server is up and answering
  2. token authentication works (and rejects a bad secret)
  3. the room management API creates and lists rooms
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agent.token import admin_token, join_token  # noqa: E402

HTTP = "http://127.0.0.1:7880"
ROOM = "verify-room"


def env(name):
    for line in open(os.path.join(os.path.dirname(__file__), "..", ".env")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} missing from .env")


def rpc(method, token, body):
    req = urllib.request.Request(
        f"{HTTP}/twirp/livekit.RoomService/{method}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def check(label, fn):
    try:
        detail = fn()
        print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))
        return True
    except Exception as e:
        print(f"  FAIL  {label} — {e}")
        return False


key, secret = env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET")
print("Task 1.1 — media server acceptance\n")
results = []

results.append(check("server responds", lambda: (
    urllib.request.urlopen(HTTP, timeout=5).read().decode().strip() or "OK")))

results.append(check("room created", lambda: rpc(
    "CreateRoom", admin_token(key, secret),
    {"name": ROOM, "empty_timeout": 60, "max_participants": 4}).get("sid")))

results.append(check("room listed", lambda: ", ".join(
    r["name"] for r in rpc("ListRooms", admin_token(key, secret), {})
    .get("rooms", []))))


def reject_bad_secret():
    try:
        rpc("ListRooms", admin_token(key, "wrong-secret"), {})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return f"HTTP {e.code} as expected"
        raise
    raise AssertionError("a forged token was ACCEPTED")


results.append(check("forged token rejected", reject_bad_secret))

results.append(check("browser join token mints", lambda: (
    join_token(key, secret, ROOM, "caller-1")[:24] + "...")))

rpc("DeleteRoom", admin_token(key, secret), {"room": ROOM})
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
