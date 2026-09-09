"""
Task 1.2 acceptance check.

Verifies everything that can be checked without a human talking:
credentials, worker registration, and the transcript path being wired.
The last step — that spoken audio actually comes back as text — needs a
person at a microphone, and this script says so rather than pretending.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def env(name):
    for line in open(os.path.join(ROOT, ".env")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return ""


def check(label, fn):
    try:
        detail = fn()
        print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))
        return True
    except Exception as e:
        print(f"  FAIL  {label} — {e}")
        return False


def key_present():
    if not env("DEEPGRAM_API_KEY"):
        raise AssertionError("DEEPGRAM_API_KEY is empty in .env")
    return "set"


def key_valid():
    """Ask Deepgram who we are. Catches a typo'd or revoked key up front."""
    req = urllib.request.Request(
        "https://api.deepgram.com/v1/projects",
        headers={"Authorization": "Token " + env("DEEPGRAM_API_KEY")},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            n = len(json.loads(r.read()).get("projects", []))
            return f"{n} project(s) visible"
    except urllib.error.HTTPError as e:
        raise AssertionError(f"Deepgram rejected the key (HTTP {e.code})")


def worker_running():
    """
    "Running" is not enough. A container that crashes and is restarted by
    the restart policy is "running" every time you look at it, so check the
    restart counter too — otherwise this check passes on a crash loop.
    """
    out = subprocess.run(
        ["docker", "compose", "ps", "--format", "{{.Name}} {{.State}}"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    if not any("agent" in l and "running" in l for l in out.splitlines()):
        raise AssertionError("agent container is not running — docker compose up -d agent")

    restarts = subprocess.run(
        ["docker", "inspect", "voice-agent-worker",
         "--format", "{{.RestartCount}}"],
        capture_output=True, text=True,
    ).stdout.strip()
    if restarts.isdigit() and int(restarts) > 2:
        raise AssertionError(
            f"crash-looping ({restarts} restarts) — it starts, fails, and is "
            "restarted. See: docker compose logs --tail 5 agent")
    return "running, not looping"


def worker_registered():
    """The worker tells LiveKit it can take jobs. No registration, no calls."""
    out = subprocess.run(
        ["docker", "compose", "logs", "--tail", "500", "agent"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.lower()
    if "registered worker" in out:
        return "registered with LiveKit"
    if "error" in out or "traceback" in out:
        raise AssertionError("worker logged an error — docker compose logs agent")
    raise AssertionError("no registration in logs yet; give it a few seconds")


def transcript_seen():
    """Only true once somebody has actually spoken into the browser."""
    out = subprocess.run(
        ["docker", "compose", "logs", "--tail", "500", "agent"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    finals = [l for l in out.splitlines() if "FINAL" in l]
    if not finals:
        raise AssertionError(
            "no transcript yet — open http://localhost:8474, join, and speak")
    return f"{len(finals)} final transcript(s), last: {finals[-1].split('FINAL')[-1].strip()[:48]}"


print("Task 1.2 — streaming STT acceptance\n")
results = [
    check("Deepgram key present", key_present),
    check("Deepgram key accepted", key_valid),
    check("agent worker running", worker_running),
    check("worker registered for jobs", worker_registered),
    check("real speech transcribed", transcript_seen),
]

passed = sum(results)
print(f"\n{passed}/{len(results)} checks passed")
if not results[-1] and passed == len(results) - 1:
    print("\nEverything automated passes. The last check needs a human:")
    print("  open http://localhost:8474 — join the room — say a sentence — rerun this.")
sys.exit(0 if all(results) else 1)
