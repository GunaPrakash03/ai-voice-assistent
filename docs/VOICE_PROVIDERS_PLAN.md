# New voice providers — plan & progress

Add four new TTS voice providers to the dashboard's **Select Voice** picker so agents can be built on
voices beyond today's set: **Gemini TTS**, **Inworld**, **MiniMax**, and **Fish Audio**. ElevenLabs and
Cartesia already work and are untouched. We do **not** add Retell as a voice provider (Retell has no TTS
endpoint; in this codebase "Retell" is only a webhook payload format + legacy voice-id tolerance).

Shaped from the Retell "Custom Providers" picker the user is migrating off: match MiniMax / Cartesia /
Fish Audio / Inworld / ElevenLabs, minus Retell, plus Gemini voices.

## Decisions taken (change any time)

- **Scope of each provider:** two-phase. **Phase 1 = dashboard preview** (voices listed + sample-play in
  the agent builder) for all four. **Phase 2 = live phone calls** (streaming in real calls). Phase 1 ships
  and is useful on its own; Phase 2 follows.
- **Live calls are low-risk:** official LiveKit plugins exist for all four
  (`livekit-plugins-inworld`, `livekit-plugins-minimax-ai`, `livekit-plugins-fishaudio`, and
  Google/Gemini), so Phase 2 needs **no custom streaming adapter** — just new `elif` branches in
  `tts_manager.apply_voice()` and new deps in the worker image.
- **Keys:** Gemini reuses the existing `GEMINI_API_KEY`. The other three each need a new key the user must
  supply (`INWORLD_API_KEY`, `MINIMAX_API_KEY` + `MINIMAX_GROUP_ID`, `FISH_AUDIO_API_KEY`). Until a key is
  present, that provider's voices show in the grid but mark themselves "needs key" (same pattern ElevenLabs
  already uses when a voice is unavailable).
- **Voice lists:** start with a curated static set per provider in `VOICE_CATALOG` (fast, predictable), and
  — where the provider offers a ListVoices endpoint (Inworld) — add runtime account-sync later, mirroring
  the existing ElevenLabs `_elevenlabs_account_voices()` pattern. Not required for Phase 1.

## Architecture recap (why each task exists)

The voice list has one source of truth — `VOICE_CATALOG` in `agent/agent_builder.py` — consumed by two
**independent** synthesis paths that must each learn every new provider:

1. **Preview path** (dashboard sample) — `agent/voice_synthesizer.py`, served by `scripts/serve.py`
   `GET /api/agents/voice-audio`. Plain REST/urllib. → **Phase 1**
2. **Live-call path** (phone/WebRTC) — `agent/tts_manager.py` LiveKit plugins, driven by
   `agent/worker.py`. → **Phase 2**

Plus the dashboard UI (`web/agent-builder.html` tabs are **hardcoded**) and key management
(`agent/provider_manager.py` + `web/api-keys.html`).

## Progress

```
Phase 1  ████████████████████  11 / 12 tasks  (T12 commit pending)
Overall  ████████████░░░░░░░░  11 / 18 tasks  61%
```

Phase 1 built and verified 2026-10-07 — `scripts/verify_voice_providers.py` **15/15** (VERIFY_LIVE_LLM=1).
Live-synth confirmed: **Gemini, Inworld, MiniMax**. **Fish Audio** integration reaches the provider but the
account is billing-blocked (0 API credit — separate from playground credit; top up at fish.audio).
MiniMax note: sk-api keys bind the group to the token, so `MINIMAX_GROUP_ID` is left **empty** (the profile
"UID" is NOT the group id). Keys for all four are saved in `.env`. UNCOMMITTED.

| Phase | Tasks | Done |
|---|---|---|
| 1A. Catalog & shared plumbing | T1–T2 | 2 / 2 |
| 1B. Gemini TTS preview | T3 | 1 / 1 |
| 1C. Inworld preview | T4 | 1 / 1 |
| 1D. MiniMax preview | T5 | 1 / 1 |
| 1E. Fish Audio preview | T6 | 1 / 1 |
| 1F. Dashboard UI | T7–T8 | 2 / 2 |
| 1G. Keys & config | T9–T10 | 2 / 2 |
| 1H. Phase-1 tests & ship | T11–T12 | 1 / 2 (commit pending) |
| 2A. Live-call engines | T13–T16 | 0 / 4 |
| 2B. Phase-2 tests & ship | T17–T18 | 0 / 2 |

Status key: `[ ]` to do · `[~]` in progress · `[x]` done

---

## Phase 1 — Dashboard preview

### 1A. Catalog & shared plumbing

- [x] **T1 — Add the four providers to the voice catalog** (`agent/agent_builder.py`, `VOICE_CATALOG` ~`:60`)
  Add `VoiceOption` entries with new `provider` strings `gemini`, `inworld`, `minimax`, `fishaudio`.
  Curated starter sets: Gemini (~8 prebuilt: Zephyr, Puck, Charon, Kore, Fenrir, Leda, Orus, Aoede),
  Inworld (the screenshot set: Ashley, Brynne, Chloe, Cimo, Della, Derek, Saanvi…), MiniMax
  (English_* voices), Fish Audio (a few flagship `reference_id`s). Fill `model`, `gender`, `style`,
  `cost_per_1k_chars` per provider. Keep the existing `RETIRED_GEMINI` remap untouched.

- [x] **T2 — Readiness reporting for the new providers** (`agent/voice_synthesizer.py`, `voice_engine_readiness()` ~`:153`)
  Teach readiness which env key each new provider needs so `/api/agents/voices` annotates each voice with
  `api_ready` / `engine` / `api_note` (e.g. "needs INWORLD_API_KEY"). Add the id-prefix inference branch so
  a voice id still resolves to a provider if it's not found in the catalog (mirrors the `eleven-`/`aura-`
  logic ~`:672`).

### 1B. Gemini TTS preview

- [x] **T3 — Gemini sample synthesis** (`agent/voice_synthesizer.py`)
  New `_fetch_gemini_tts(text, voice_name, model)` → POST
  `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent`
  with `responseModalities:["AUDIO"]` and `speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName`.
  Response is **base64 PCM (24 kHz, 16-bit mono)** — wrap in a WAV header (reuse the existing `wave`-module
  helper; Playwright ffmpeg has no WAV demuxer per env notes) before returning. Add a `provider == "gemini"`
  branch to `_sync_generate_speech_audio_bytes` ~`:705` and to `stream_voice_audio` ~`:972`. Key:
  `GEMINI_API_KEY` (already in `.env`). Watch the free-tier rate limit (per memory, ~5 req/min).

### 1C. Inworld preview

- [x] **T4 — Inworld sample synthesis** (`agent/voice_synthesizer.py`)
  New `_fetch_inworld_tts(text, voice_id, model)` → POST SyncSynthesizeSpeech on `https://api.inworld.ai`,
  model `inworld-tts-1.5-mini` (cheap/fast) default, **HTTP Basic auth** with `INWORLD_API_KEY` sent
  verbatim. Returns audio bytes (request WAV/MP3). Add the `provider == "inworld"` branch in both the
  sync and streaming generators. (ListVoices → account-sync is a later enhancement, not Phase 1.)

### 1D. MiniMax preview

- [x] **T5 — MiniMax sample synthesis** (`agent/voice_synthesizer.py`)
  New `_fetch_minimax_tts(text, voice_id, model)` → POST `https://api.minimax.io/v1/t2a_v2`
  (US edge `api-uw.minimax.io` as fallback), body `{model, text, voice_setting:{voice_id}, audio_setting}`.
  Auth: `Authorization: Bearer MINIMAX_API_KEY` **plus `GroupId` query param = `MINIMAX_GROUP_ID`** (MiniMax
  requires both). Response is hex/base64 audio in JSON → decode to bytes. Add the `provider == "minimax"`
  branch in sync + streaming generators.

### 1E. Fish Audio preview

- [x] **T6 — Fish Audio sample synthesis** (`agent/voice_synthesizer.py`)
  New `_fetch_fishaudio_tts(text, reference_id, model)` → POST `https://api.fish.audio/v1/tts`, header
  `Authorization: Bearer FISH_AUDIO_API_KEY` and `model: s2.1-pro` (via header/body per their API), body
  `{text, reference_id, format:"mp3"}`. Returns audio bytes directly. Add the `provider == "fishaudio"`
  branch in sync + streaming generators.

### 1F. Dashboard UI

- [x] **T7 — Provider tabs + labels** (`web/agent-builder.html`)
  The Select Voice tabs are hardcoded ~`:599-605`. Add four tabs (Gemini / Inworld / MiniMax / Fish Audio),
  extend `PROVIDER_NAMES` ~`:842` and `PROVIDER_COLORS` ~`:1205`. The voice grid itself is already fetched
  from `/api/agents/voices`, so cards appear automatically once T1 lands; tab counts update via
  `updateProviderTabCounts` ~`:1058`. Verify the "needs key" badge path (`markVoiceUnavailable` ~`:891`,
  which reads the `X-Voice-Engine` / fallback headers) renders for keyless providers.

- [x] **T8 — Sample-play polish** (`web/agent-builder.html`, `playVoiceAudio()` ~`:954`)
  Confirm fallback messaging reads right for each provider (e.g. "add a MiniMax key to hear this voice")
  and the browser `speechSynthesis` fallback ~`:1018` still triggers when a provider key is absent.

### 1G. Keys & config

- [x] **T9 — Register key metadata** (`agent/provider_manager.py`)
  Add `inworld`, `minimax`, `fishaudio` to `KNOWN_PROVIDERS` ~`:29` (category "voice") and their env vars to
  `ALLOWED_CONFIG_KEYS` ~`:105`: `INWORLD_API_KEY`, `MINIMAX_API_KEY`, `MINIMAX_GROUP_ID`,
  `FISH_AUDIO_API_KEY`. Extend `test_provider_connection()` ~`:280` with a cheap liveness check per provider
  (Inworld ListVoices; MiniMax/Fish a tiny synth or voices call). Gemini already exists (flip its usage note
  to mention TTS, or leave as-is).

- [x] **T10 — Key cards + docs** (`web/api-keys.html` ~`:179-430`, `.env.example`)
  Add Super-Admin key cards for Inworld, MiniMax (two fields: key + group id), Fish Audio, each with a Test
  button. Document all new env vars in `.env.example` near the existing `CARTESIA_API_KEY` block.

### 1H. Phase-1 tests & ship

- [x] **T11 — Verification script** (`scripts/verify_voice_providers.py`, new)
  Mirror `verify_tts.py` / `verify_agent_builder.py`: assert each new provider appears in `list_voices()`,
  that `/api/agents/voices` annotates readiness correctly with and without keys, and — when a key is present
  (gated like `VERIFY_LIVE_LLM`) — that `_fetch_<provider>_tts` returns non-empty audio of the right type.
  Target a clean N/N. Add a Playwright check that each new tab renders and plays (or shows the keyless
  badge), extending `scripts/test_playwright_voices.py` (remember: it rewrites `web/audio/`, `git checkout`
  after).

- [ ] **T12 — Admin plan page + ship Phase 1** (`web/voice-providers-plan.html`, optional; commit)
  Optional admin-only progress page mirroring `web/knowledge-plan.html`. Commit Phase 1 (catalog +
  synthesizer + UI + keys + tests). Restart `scripts/serve.py` (port 8091) to pick up config; preview path
  needs no worker rebuild.

---

## Phase 2 — Live phone calls

### 2A. Live-call engines

- [ ] **T13 — Worker deps** (`requirements.txt`, worker image)
  Add `livekit-plugins-inworld`, `livekit-plugins-minimax-ai`, `livekit-plugins-fishaudio`, and the Google
  plugin for Gemini TTS (confirm exact distribution name/version against `livekit-agents~=1.0`). Rebuild:
  `docker compose build agent` (deps are NOT bind-mounted; code/config are).

- [ ] **T14 — apply_voice branches** (`agent/tts_manager.py`, `apply_voice()` ~`:339-383`)
  Add `elif provider == "inworld" / "minimax" / "fishaudio" / "gemini"` branches constructing the LiveKit
  plugin `TTS(...)` with the catalog voice id/model and the provider key. Keep the existing fallback chain
  (→ Cartesia → Deepgram → simulator) ~`:385-402`.
  **Pre-flight bug to fix first:** `tts_manager.py:288` annotates `_elevenlabs_cache: Dict[str, str]` but
  `Dict` is not imported (module imports only `Any, AsyncIterable, Callable, List, Optional` ~`:21`).
  Add `Dict` to the import (or the file may `NameError` on load once edited). Verify it currently loads.

- [ ] **T15 — Per-agent selection smoke test**
  Confirm `worker.get_voice()` → `apply_voice()` path (`worker.py:337-346`) and mid-call voice change
  (`worker.py:1524-1534`, `update_options(tts=…)`) work for a new-provider voice. Watch for the known
  "Cartesia: no audio frames were pushed" class of live-TTS failures (per env notes) on the new engines.

- [ ] **T16 — Optional account-sync** (`agent/agent_builder.py`)
  Where a provider lists voices (Inworld ListVoices), add a cached account-voice merge like
  `_elevenlabs_account_voices()` ~`:574` so the user's own/cloned voices appear. Nice-to-have.

### 2B. Phase-2 tests & ship

- [ ] **T17 — Live verification**
  Extend `verify_voice_providers.py` with live-call assertions (gated, like the existing live suites). Run
  the relevant `run_all_tests.py` voice suites **alone** (they flake if run concurrently per env notes).

- [ ] **T18 — Ship Phase 2**
  Commit; rebuild + restart the worker (`docker compose build agent && docker compose restart agent`;
  note: Docker worker restart has historically needed the user to run it). Deploy note for Railway: the
  worker image there also needs the new plugin deps (auto-deploys from `main`).

---

## Credentials the user must provide

| Provider | Env var(s) | Notes |
|---|---|---|
| Gemini TTS | `GEMINI_API_KEY` | Already set. Free tier ~5 req/min; fine for previews, watch limits. |
| Inworld | `INWORLD_API_KEY` | From Inworld portal; Basic-auth, sent verbatim. |
| MiniMax | `MINIMAX_API_KEY`, `MINIMAX_GROUP_ID` | Both required; group id is a separate field. |
| Fish Audio | `FISH_AUDIO_API_KEY` | From fish.audio developer console. |

## Risks & open questions

- **Rate / cost limits** differ per provider; the sample path should cache aggressively (an
  `_AUDIO_CACHE` already exists in `voice_synthesizer.py`) to avoid burning quota on repeated previews.
- **Audio formats vary** (Gemini returns raw PCM needing a WAV wrapper; others return MP3/WAV directly) —
  handled per-provider in T3–T6.
- **LiveKit plugin names/versions** must be pinned against `livekit-agents~=1.0`; verify at T13 before
  rebuild.
- **Account-sync (T16)** depends on each provider exposing a ListVoices endpoint — confirmed for Inworld;
  MiniMax/Fish use fixed voice ids / `reference_id`s, so their catalog stays curated.
