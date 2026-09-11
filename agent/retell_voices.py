"""
Retell AI Platform Voice Library integration.

Retell AI is a voice-agent platform, not a stand-alone TTS vendor. Its public API
exposes the platform voice library (GET https://api.retellai.com/list-voices) but
no text-to-speech endpoint. Each Retell voice record names the underlying engine
(elevenlabs / openai / cartesia / minimax / fish_audio / inworld / platform) and
carries an official MP3 preview URL.

This module therefore does three things:
  1. Pulls the live Retell voice library with RETELL_API_KEY and caches it
     (in-memory + config/retell_voices_cache.json) so the catalogue survives
     restarts and offline periods.
  2. Serves the official Retell preview MP3 for a voice (cached under audio/retell/).
  3. Tells the synthesizer which underlying engine to route custom text through.
"""

import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

log = logging.getLogger("retell-voices")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FILE = os.path.join(ROOT_DIR, "config", "retell_voices_cache.json")
PREVIEW_DIR = os.path.join(ROOT_DIR, "audio", "retell")

RETELL_API_BASE = "https://api.retellai.com"
RETELL_LIST_VOICES_URL = f"{RETELL_API_BASE}/list-voices"
RETELL_DOCS_URL = "https://dashboard.retellai.com/apiKey"

# Retell voice ids are "<engine-prefix>-<Name>", e.g. 11labs-Cimo, openai-Alloy, cartesia-Katie.
RETELL_ID_PATTERN = re.compile(r"^(11labs|openai|deepgram|cartesia|minimax|fish_audio|inworld|play|platform)-", re.I)
# Bundled sample-backed voices use the "retell-<name>" id form.
RETELL_SAMPLE_PATTERN = re.compile(r"^retell-", re.I)

_CACHE_TTL_SECONDS = 10 * 60
_mem_cache: Dict[str, Any] = {"voices": None, "fetched_at": 0.0}


def get_retell_api_key() -> str:
    return (os.getenv("RETELL_API_KEY") or "").strip()


def is_retell_voice_id(voice_id: str) -> bool:
    return bool(voice_id) and bool(RETELL_ID_PATTERN.match(voice_id) or RETELL_SAMPLE_PATTERN.match(voice_id))


# ── Library fetch & cache ────────────────────────────────────────────────────
def _read_disk_cache() -> List[Dict[str, Any]]:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        voices = data.get("voices") if isinstance(data, dict) else data
        return voices if isinstance(voices, list) else []
    except Exception:
        return []


def _write_disk_cache(voices: List[Dict[str, Any]]) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": time.time(), "count": len(voices), "voices": voices}, fh, indent=2)
    except Exception as ex:
        log.warning("Could not persist Retell voice cache: %s", ex)


def fetch_retell_voices(api_key: Optional[str] = None, timeout: float = 8.0) -> List[Dict[str, Any]]:
    """Live call to Retell list-voices. Raises on HTTP/network failure."""
    key = (api_key or get_retell_api_key()).strip()
    if not key:
        raise RuntimeError("RETELL_API_KEY is not configured")
    req = urllib.request.Request(
        RETELL_LIST_VOICES_URL,
        headers={"Authorization": f"Bearer {key}", "User-Agent": "VoiceAgentService/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, dict):
        data = data.get("voices") or data.get("data") or []
    voices = [v for v in data if isinstance(v, dict) and v.get("voice_id")]
    _mem_cache["voices"] = voices
    _mem_cache["fetched_at"] = time.time()
    _write_disk_cache(voices)
    return voices


def list_retell_voices(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Returns the raw Retell voice records. Uses memory cache -> live API (if key) -> disk cache.
    Never raises; returns [] when nothing is available.
    """
    now = time.time()
    if not force_refresh and _mem_cache["voices"] is not None and (now - _mem_cache["fetched_at"]) < _CACHE_TTL_SECONDS:
        return _mem_cache["voices"]

    if get_retell_api_key():
        try:
            return fetch_retell_voices()
        except Exception as ex:
            log.warning("Retell list-voices failed, using cached copy: %s", ex)

    cached = _read_disk_cache()
    _mem_cache["voices"] = cached
    _mem_cache["fetched_at"] = now
    return cached


def get_retell_voice(voice_id: str) -> Optional[Dict[str, Any]]:
    if not voice_id:
        return None
    for v in list_retell_voices():
        if v.get("voice_id") == voice_id:
            return v
    low = voice_id.lower()
    for v in list_retell_voices():
        if str(v.get("voice_id", "")).lower() == low:
            return v
    return None


# ── Catalogue adapter ────────────────────────────────────────────────────────
_ENGINE_LABEL = {
    "elevenlabs": "ElevenLabs",
    "openai": "OpenAI",
    "cartesia": "Cartesia",
    "deepgram": "Deepgram",
    "minimax": "MiniMax",
    "fish_audio": "Fish Audio",
    "inworld": "Inworld",
    "platform": "Retell Platform",
}

_ENGINE_FIRST_AUDIO_MS = {
    "elevenlabs": 260,
    "openai": 420,
    "cartesia": 95,
    "deepgram": 180,
    "minimax": 350,
    "fish_audio": 400,
    "inworld": 380,
    "platform": 300,
}


def underlying_engine(voice: Dict[str, Any]) -> str:
    engine = str(voice.get("provider") or "").lower()
    if not engine:
        m = RETELL_ID_PATTERN.match(str(voice.get("voice_id", "")))
        engine = (m.group(1).lower() if m else "platform")
    if engine == "11labs":
        engine = "elevenlabs"
    if engine == "play":
        engine = "platform"
    return engine


def plain_voice_name(voice: Dict[str, Any]) -> str:
    """'Cimo' from voice_name, or from the id suffix (11labs-Cimo -> Cimo)."""
    name = str(voice.get("voice_name") or "").strip()
    if name:
        return name
    vid = str(voice.get("voice_id") or "")
    return RETELL_ID_PATTERN.sub("", vid) or vid


def to_voice_option_dict(voice: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a Retell record like agent_builder.VoiceOption.to_dict()."""
    engine = underlying_engine(voice)
    name = plain_voice_name(voice)
    style_bits = [b for b in (voice.get("accent"), voice.get("age")) if b]
    style = ", ".join(str(b) for b in style_bits) or "conversational"
    gender = str(voice.get("gender") or "female").lower()
    if gender not in ("male", "female"):
        gender = "unisex"
    return {
        "voice_id": voice["voice_id"],
        "name": f"{name} (Retell AI)",
        "provider": "retell",
        "model": f"retell/{engine}",
        "style": style,
        "gender": gender,
        "first_audio_ms": _ENGINE_FIRST_AUDIO_MS.get(engine, 300),
        "cost_per_1k_chars": 0.0,
        "engine": engine,
        "engine_label": _ENGINE_LABEL.get(engine, engine.title()),
        "preview_audio_url": voice.get("preview_audio_url") or "",
        "accent": voice.get("accent") or "",
        "age": voice.get("age") or "",
    }


def list_retell_voice_options(force_refresh: bool = False) -> List[Dict[str, Any]]:
    return [to_voice_option_dict(v) for v in list_retell_voices(force_refresh=force_refresh)]


def library_summary(voices: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    voices = list_retell_voices() if voices is None else voices
    by_engine: Dict[str, int] = {}
    for v in voices:
        eng = underlying_engine(v)
        by_engine[eng] = by_engine.get(eng, 0) + 1
    return {"total": len(voices), "by_engine": by_engine, "sample": [plain_voice_name(v) for v in voices[:8]]}


# ── Official preview audio ───────────────────────────────────────────────────
def _safe_filename(voice_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", voice_id)


def get_retell_preview_audio(voice_id: str, timeout: float = 8.0) -> Optional[bytes]:
    """
    Returns the official Retell preview MP3 for a voice. Cached on disk so the
    second play is instant and works offline.
    """
    voice = get_retell_voice(voice_id)
    url = (voice or {}).get("preview_audio_url") or ""
    path = os.path.join(PREVIEW_DIR, f"{_safe_filename(voice_id)}.mp3")
    # Local sample first: cached download, or the bundled retell-<name>.mp3/.wav recordings.
    base = os.path.join(PREVIEW_DIR, _safe_filename(voice_id))
    for candidate in (path, base + "_24k.wav", base + ".wav"):
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            try:
                with open(candidate, "rb") as fh:
                    return fh.read()
            except Exception:
                pass
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "VoiceAgentService/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            audio = resp.read()
        if audio:
            os.makedirs(PREVIEW_DIR, exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(audio)
        return audio or None
    except Exception as ex:
        log.warning("Retell preview download failed for %s: %s", voice_id, ex)
        return None
