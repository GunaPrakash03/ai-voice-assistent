"""
Validate an API key against provider APIs, then write it to .env.

Supports:
- Deepgram: python3 scripts/set_key.py deepgram <key> (or python3 scripts/set_key.py <key>)
- OpenAI:   python3 scripts/set_key.py openai <key>   (or python3 scripts/set_key.py sk-...)
- Cartesia: python3 scripts/set_key.py cartesia <key>
"""

import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ENV = os.path.join(ROOT, ".env")

if len(sys.argv) < 2 or len(sys.argv) > 3:
    raise SystemExit(
        "Usage:\n"
        "  python3 scripts/set_key.py deepgram <key>\n"
        "  python3 scripts/set_key.py openai <key>\n"
        "  python3 scripts/set_key.py cartesia <key>\n"
        "  python3 scripts/set_key.py <key> (auto-detects service)"
    )

if len(sys.argv) == 2:
    arg = sys.argv[1].strip()
    if arg.startswith("sk-"):
        provider = "openai"
        key = arg
    elif "cartesia" in arg.lower():
        provider = "cartesia"
        key = arg
    else:
        provider = "deepgram"
        key = arg
else:
    provider = sys.argv[1].strip().lower()
    key = sys.argv[2].strip()

if len(key) < 16:
    raise SystemExit(f"That looks too short to be an API key ({len(key)} chars).")

if provider == "deepgram":
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
            raise SystemExit("Deepgram rejected this key (HTTP 401 Unauthorized).")
        raise SystemExit(f"Deepgram returned HTTP {e.code}")
    except Exception as e:
        raise SystemExit(f"Could not reach Deepgram: {e}")

    env_var = "DEEPGRAM_API_KEY"
    print(f"  valid — {len(projects)} project(s): " + ", ".join(p.get("name", "?") for p in projects))

elif provider == "openai":
    print("Checking the key with OpenAI...")
    req = urllib.request.Request(
        "https://api.openai.com/v1/models",
        headers={"Authorization": "Bearer " + key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            models_data = json.loads(r.read()).get("data", [])
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SystemExit("OpenAI rejected this key (HTTP 401 Unauthorized). Check your API key.")
        raise SystemExit(f"OpenAI returned HTTP {e.code}")
    except Exception as e:
        raise SystemExit(f"Could not reach OpenAI: {e}")

    env_var = "OPENAI_API_KEY"
    print(f"  valid — OpenAI key accepted ({len(models_data)} models accessible)")

elif provider == "cartesia":
    print("Checking the key with Cartesia Sonic...")
    req = urllib.request.Request(
        "https://api.cartesia.ai/voices",
        headers={
            "X-API-Key": key,
            "Cartesia-Version": "2025-04-16",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            voices_data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SystemExit("Cartesia rejected this key (HTTP 401 Unauthorized). Check your Cartesia console.")
        raise SystemExit(f"Cartesia returned HTTP {e.code}")
    except Exception as e:
        raise SystemExit(f"Could not reach Cartesia: {e}")

    env_var = "CARTESIA_API_KEY"
    print(f"  valid — Cartesia key accepted ({len(voices_data)} voices available)")

else:
    raise SystemExit(f"Unknown provider: {provider}. Supported: deepgram, openai, cartesia")

# Write to .env
lines = open(ENV).read().splitlines() if os.path.exists(ENV) else []
found = False
for i, l in enumerate(lines):
    if l.startswith(f"{env_var}="):
        lines[i] = f"{env_var}=" + key
        found = True
if not found:
    lines.append(f"{env_var}=" + key)

open(ENV, "w").write("\n".join(lines) + "\n")
print(f"  written {env_var} to .env (gitignored)")
print("\nNext:")
print("  docker compose up -d agent")
if provider == "deepgram":
    print("  python3 scripts/verify_stt.py")
elif provider == "openai":
    print("  python3 scripts/verify_llm.py")
else:
    print("  python3 scripts/verify_tts.py")
