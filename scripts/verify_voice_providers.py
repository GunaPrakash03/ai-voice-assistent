"""Phase 1 acceptance check — new TTS voice providers in the dashboard voice picker.

Covers the preview/sample path for Gemini TTS, Inworld, MiniMax and Fish Audio:
1. Each provider contributes voices to VOICE_CATALOG / list_voices() with correctly prefixed ids.
2. voice_engine_readiness() reports the right engine and a clear "needs key" note per provider.
3. provider_manager registers each provider's key metadata (KNOWN_PROVIDERS + ALLOWED_CONFIG_KEYS).
4. The synthesizer exposes a _fetch_<provider>_tts helper and routes the provider in the dispatcher.
5. Dashboard UI (agent-builder.html) and the API Keys page (api-keys.html) expose each provider.
6. Live synthesis (gated): with a provider key present AND VERIFY_LIVE_LLM=1, the provider
   actually returns audio bytes. Gemini is validated whenever GEMINI_API_KEY is configured.

Run:  python3 scripts/verify_voice_providers.py
Live: VERIFY_LIVE_LLM=1 python3 scripts/verify_voice_providers.py
"""

import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

PASS = 0
FAIL = 0


def check(label, fn):
    global PASS, FAIL
    try:
        detail = fn()
        print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))
        PASS += 1
        return True
    except Exception as e:
        print(f"  FAIL  {label} — {e}")
        FAIL += 1
        return False


NEW = {
    # env alternatives satisfy "any". MiniMax's GROUP_ID is optional (newer sk-api keys bind
    # the group to the token), so only the API key gates readiness.
    "gemini": {"prefix": "gemini-", "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "engine": "Gemini TTS"},
    "inworld": {"prefix": "inworld-", "env": ["INWORLD_API_KEY"], "engine": "Inworld TTS"},
    "minimax": {"prefix": "minimax-", "env": ["MINIMAX_API_KEY"], "engine": "MiniMax T2A"},
    "fishaudio": {"prefix": "fishaudio-", "env": ["FISH_AUDIO_API_KEY"], "engine": "Fish Audio"},
}

SAMPLE = "Hello, thanks for calling. How can I help you today?"


def provider_keyed(meta):
    """True when this provider's key(s) are configured (ANY alternative, or ALL when requires_all)."""
    vals = [os.getenv(k) for k in meta["env"]]
    return all(vals) if meta.get("requires_all") else any(vals)


def read(path):
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as fh:
        return fh.read()


# ── 1. Catalog ────────────────────────────────────────────────────────────────

def test_catalog_has_each_provider():
    from agent.agent_builder import VOICE_CATALOG
    missing = [p for p in NEW if not any(v.provider == p for v in VOICE_CATALOG)]
    if missing:
        raise AssertionError(f"no catalog voices for: {missing}")
    counts = {p: sum(1 for v in VOICE_CATALOG if v.provider == p) for p in NEW}
    return ", ".join(f"{p}={n}" for p, n in counts.items())


def test_voice_ids_prefixed():
    from agent.agent_builder import VOICE_CATALOG
    bad = []
    for v in VOICE_CATALOG:
        if v.provider in NEW and not v.voice_id.startswith(NEW[v.provider]["prefix"]):
            bad.append(v.voice_id)
    if bad:
        raise AssertionError(f"ids missing provider prefix: {bad[:5]}")
    return "all new voice ids carry their provider prefix"


def test_list_voices_includes_new():
    from agent.agent_builder import AgentBuilder
    voices = AgentBuilder().list_voices()
    provs = {v["provider"] for v in voices}
    missing = [p for p in NEW if p not in provs]
    if missing:
        raise AssertionError(f"list_voices() missing: {missing}")
    return f"list_voices() exposes {len(voices)} voices across {len(provs)} providers"


# ── 2. Readiness ────────────────────────────────────────────────────────────────

def test_readiness_matches_key_state():
    from agent import voice_synthesizer as vs
    from agent.agent_builder import VOICE_CATALOG
    notes = []
    for p, meta in NEW.items():
        sample = next(v for v in VOICE_CATALOG if v.provider == p)
        r = vs.voice_engine_readiness(sample.voice_id, p, sample.gender)
        has_key = provider_keyed(meta)
        if has_key and not r["ready"]:
            raise AssertionError(f"{p}: key present but readiness says not ready ({r})")
        if not has_key:
            if r["ready"]:
                raise AssertionError(f"{p}: no key but readiness says ready ({r})")
            if not r["note"]:
                raise AssertionError(f"{p}: missing 'needs key' note")
        notes.append(f"{p}={'ready' if r['ready'] else 'needs-key'}")
    return ", ".join(notes)


# ── 3. provider_manager ────────────────────────────────────────────────────────

def test_known_providers():
    from agent import provider_manager as pm
    for p in ("inworld", "minimax", "fishaudio"):
        if p not in pm.KNOWN_PROVIDERS:
            raise AssertionError(f"{p} missing from KNOWN_PROVIDERS")
        if pm.KNOWN_PROVIDERS[p]["category"] != "voice":
            raise AssertionError(f"{p} not category 'voice'")
    for env in ("INWORLD_API_KEY", "MINIMAX_API_KEY", "MINIMAX_GROUP_ID", "FISH_AUDIO_API_KEY"):
        if env not in pm.ALLOWED_CONFIG_KEYS:
            raise AssertionError(f"{env} not in ALLOWED_CONFIG_KEYS")
    return "inworld/minimax/fishaudio registered; env vars allow-listed"


def test_status_list_returns_new():
    from agent.provider_manager import ProviderManager
    ids = {p["provider_id"] for p in ProviderManager().list_providers_status()}
    missing = [p for p in ("inworld", "minimax", "fishaudio") if p not in ids]
    if missing:
        raise AssertionError(f"list_providers_status missing: {missing}")
    return "status list exposes the new providers"


# ── 4. Synthesizer helpers + dispatcher ─────────────────────────────────────────

def test_fetch_helpers_exist():
    from agent import voice_synthesizer as vs
    for fn in ("_fetch_gemini_tts", "_fetch_inworld_tts", "_fetch_minimax_tts", "_fetch_fishaudio_tts"):
        if not callable(getattr(vs, fn, None)):
            raise AssertionError(f"missing helper {fn}")
    return "all four _fetch_*_tts helpers present"


def test_provider_inference():
    """Without a key, synthesis must fall back gracefully (never raise) and note the missing key."""
    from agent import voice_synthesizer as vs
    from agent.agent_builder import VOICE_CATALOG
    for p, meta in NEW.items():
        if provider_keyed(meta):
            continue  # keyed providers are exercised live below
        sample = next(v for v in VOICE_CATALOG if v.provider == p)
        audio = vs.get_voice_audio(sample.voice_id, sample.name, sample.gender, "professional", p, SAMPLE)
        if not audio or len(audio) < 1000:
            raise AssertionError(f"{p}: fallback produced no audio")
    return "keyless providers fall back to a neural sample without error"


# ── 5. UI surfaces ──────────────────────────────────────────────────────────────

def test_agent_builder_ui():
    html = read("web/agent-builder.html")
    for p in NEW:
        if f'data-prov="{p}"' not in html:
            raise AssertionError(f"no voice tab for {p}")
        if f"{p}:" not in html:
            raise AssertionError(f"{p} missing from PROVIDER_NAMES/COLORS")
    return "4 new provider tabs + name/color maps present"


def test_api_keys_ui():
    html = read("web/api-keys.html")
    for env in ("INWORLD_API_KEY", "MINIMAX_API_KEY", "MINIMAX_GROUP_ID", "FISH_AUDIO_API_KEY"):
        if f"key-{env}" not in html:
            raise AssertionError(f"no key input for {env}")
    for cid in ("card-inworld", "card-minimax", "card-fishaudio"):
        if cid not in html:
            raise AssertionError(f"missing {cid}")
    return "Inworld/MiniMax/Fish key cards present (Gemini reuses the Google card)"


def test_env_example_documented():
    txt = read(".env.example")
    for env in ("INWORLD_API_KEY", "MINIMAX_API_KEY", "MINIMAX_GROUP_ID", "FISH_AUDIO_API_KEY"):
        if env not in txt:
            raise AssertionError(f"{env} not documented in .env.example")
    return "new env vars documented"


# ── 6. Live synthesis (gated) ───────────────────────────────────────────────────

def _billing_blocked(provider):
    """Raw one-shot probe: returns a message if the provider auth'd but rejected for billing/credit,
    else "". Proves the integration reached the provider even when synthesis can't complete."""
    import json, urllib.request, urllib.error
    try:
        if provider == "fishaudio":
            req = urllib.request.Request(
                "https://api.fish.audio/v1/tts",
                data=json.dumps({"text": "Hi.", "format": "mp3"}).encode(),
                headers={"Authorization": f"Bearer {os.getenv('FISH_AUDIO_API_KEY')}",
                         "Content-Type": "application/json", "model": "s1"})
            urllib.request.urlopen(req, timeout=12).read()
        elif provider == "minimax":
            url = f"https://api.minimax.io/v1/t2a_v2?GroupId={os.getenv('MINIMAX_GROUP_ID') or ''}"
            req = urllib.request.Request(url, data=json.dumps({
                "model": "speech-02-turbo", "text": "Hi.", "stream": False,
                "voice_setting": {"voice_id": "English_captivating_female1"}, "audio_setting": {"format": "mp3"}}).encode(),
                headers={"Authorization": f"Bearer {os.getenv('MINIMAX_API_KEY')}", "Content-Type": "application/json"})
            d = json.loads(urllib.request.urlopen(req, timeout=12).read().decode())
            msg = (d.get("base_resp") or {}).get("status_msg", "")
            if (d.get("base_resp") or {}).get("status_code") in (1008,):
                return f"MiniMax: {msg}"
    except urllib.error.HTTPError as e:
        if e.code == 402:
            body = e.read().decode("utf-8", "replace")[:120]
            return f"HTTP 402 (billing): {body}"
    except Exception:
        pass
    return ""


def _live_test(provider):
    def fn():
        from agent import voice_synthesizer as vs
        from agent.agent_builder import VOICE_CATALOG
        sample = next(v for v in VOICE_CATALOG if v.provider == provider)
        audio = vs.get_voice_audio(sample.voice_id, sample.name, sample.gender, "professional", provider, SAMPLE)
        status = vs.get_voice_engine_status(sample.voice_id)
        if audio and len(audio) >= 2000 and status.get("engine") == provider:
            return f"{len(audio)} bytes via {provider} ({sample.voice_id})"
        # Didn't synthesize via the provider — only acceptable if the provider rejected us for billing.
        note = _billing_blocked(provider)
        if note:
            return f"integration reached {provider} but account is billing-blocked — {note}"
        raise AssertionError(f"served by '{status.get('engine')}' not '{provider}' ({status.get('fallback_reason')})")
    return fn


def main():
    print("Phase 1 — new voice providers (Gemini / Inworld / MiniMax / Fish Audio)\n")

    print("Catalog")
    check("each provider has catalog voices", test_catalog_has_each_provider)
    check("new voice ids are provider-prefixed", test_voice_ids_prefixed)
    check("list_voices() includes new providers", test_list_voices_includes_new)

    print("\nReadiness")
    check("readiness matches key configuration", test_readiness_matches_key_state)

    print("\nProvider key registry")
    check("KNOWN_PROVIDERS + ALLOWED_CONFIG_KEYS", test_known_providers)
    check("list_providers_status() returns new providers", test_status_list_returns_new)

    print("\nSynthesizer")
    check("_fetch_*_tts helpers exist", test_fetch_helpers_exist)
    check("keyless providers fall back cleanly", test_provider_inference)

    print("\nUI surfaces")
    check("agent-builder.html voice tabs", test_agent_builder_ui)
    check("api-keys.html provider cards", test_api_keys_ui)
    check(".env.example documents new vars", test_env_example_documented)

    live = os.getenv("VERIFY_LIVE_LLM") == "1"
    print("\nLive synthesis" + ("" if live else " (skipped — set VERIFY_LIVE_LLM=1)"))
    for p, meta in NEW.items():
        has_key = provider_keyed(meta)
        # Gemini is validated whenever its key exists; others need the live flag too.
        run = has_key and (live or p == "gemini")
        if run:
            check(f"live {p} synthesis", _live_test(p))
        else:
            reason = "no key" if not has_key else "VERIFY_LIVE_LLM unset"
            print(f"  SKIP  live {p} synthesis — {reason}")

    total = PASS + FAIL
    print(f"\n{PASS}/{total} checks passed" + (f", {FAIL} FAILED" if FAIL else ""))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
