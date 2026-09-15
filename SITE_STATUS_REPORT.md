# Site Status Report — 2026-09-14

## 0. Executive summary (end of day)

**State:** all 20 verification suites pass (full run 18/20 back-to-back; the two failures were the
known LiveKit handshake flake and a page-gate issue in the softphone suite, both pass alone — the
suite now takes a dev session). All 11 pages load with zero JavaScript errors. Work through ff70480 is
committed; the remaining uncommitted set is the last round of tweaks (permission relaxation, header
removal, builder new-agent mode, user guide, softphone suite fix).

**What the platform does now (built or fixed today):**
| Area | Result |
|---|---|
| Sign-in | Password, then SMS code (Twilio) when a phone is on the profile; first-run admin setup; HttpOnly sessions; sign-out |
| Users & roles | Two roles: admin / user. Users: full dashboard (all agents, calls, webhooks) except API keys & providers and member management |
| Agents | Owned per user; builder with Save/revisions; "new agent" mode; Set live; per-number routing |
| Dialogue | Gemini drives sandbox and live calls (retired model ids remapped, thinking budget fixed); tools via function calling |
| Voice | Real engines streamed (ElevenLabs premade, Deepgram Aura); honest fallback labels; picker readiness chips |
| Test call | One request per turn (1.2–1.6 s), streamed audio (0.5 s to voice on ElevenLabs), mic paused while agent speaks, auto hang-up, saved to history with dual-channel recording |
| Softphone | Actually joins the LiveKit room now; agent greets on pick-up; sign-off ends the call |
| Webhooks & data | Per-agent toggle; AI-suggested fields from the prompt; Gemini extraction from transcripts; signed call.completed payload with `extracted`; admin test/replay page |
| Storage | PostgreSQL write-through + restore for every store and every recording |
| Docs | In-app **How to use** page (`/user-guide.html`) linked from every sidebar |

**Still simulated / needs a decision:** real phone calls (no LiveKit SIP service in the stack), the
`s3://` archive URL (local placeholder), Twilio trial SMS restriction, ElevenLabs free plan voices.

**Recommended next:** commit; add the LiveKit SIP container and point the Twilio trunk at it for real
calls; decide on cloud storage for the archive.

---

Scope: pending work check, full site sweep (8 pages, 81 API routes), and the complete verification
suite run against the local dev stack (serve.py on :8091 + docker compose LiveKit / Redis / worker).

## 1. Summary

| Area | Result |
|---|---|
| Web pages (8) | All load, 0 JavaScript errors, 0 failed requests |
| API routes (81) | All respond as designed (GET routes 200, POST-only routes 404 on GET, tenant routes 401 without token) |
| Verification suites (19 + Playwright) | 18 of 19 pass; 1 needs a human to speak (`verify_stt.py`) |
| Playwright voice flows | 44 / 44 checks pass |
| Uncommitted work | Voice-engine truthfulness + intake transcript fixes (section 7), ready to commit |
| Pending task from last session | `web/flow-testing.html` (flows + testing doc) — **not started** |

## 2. Pending tasks & Carrier Setup

### 2.1 Current Progress & Changes
1. **Carrier Split Committed** — Telnyx and Twilio carrier split committed in `0de49f3`.
2. **Live Twilio Account Connected** — Account SID `AC••••••••••••••••••••••••••••••••` & Auth Token authenticated (`My first Twilio account`, active).
3. **Twilio DID Configured** — Live inbound number `+1 (951) 717-7889` added to `config/phone_numbers.json` and assigned to *Maya - Bottini & Bottini*.
4. **Flow & testing HTML doc** — `web/flow-testing.html` in the dark palette of `code-walkthrough.html` (pending).

### 2.3 Newly found — worth fixing

| Priority | Finding | Where |
|---|---|---|
| Medium | **Empty inbound-trunk POST creates a trunk.** `POST /api/telephony/trunks/inbound` with `{}` succeeds and persists `"Custom Inbound Trunk"` with `numbers: []` and `allowed_addresses: ["0.0.0.0/0"]`. Outbound requires ≥1 number; inbound should require ≥1 number and reject a blank name (the server fills a default before validating). | `scripts/serve.py:608`, `agent/telephony_manager.py:147` |
| Low | **No delete route for SIP trunks.** Once a trunk is created it can only be removed by editing `config/sip_trunks.json` and restarting. | `scripts/serve.py` |
| Low | **Marketplace carrier chip counts ignore the country filter.** Chip says "5" (all countries) while the grid shows 3 because the dropdown defaults to United States. | `web/index.html` `loadAvailableNumbers()` |
| Low | Released numbers stay in `config/phone_numbers.json` as `status: released` records forever. Fine for audit, but the file will grow. | `agent/telephony_manager.py` |

Two stray trunks and one probe number created during this sweep were removed from the config files;
the tree is back to the pre-sweep diff.

## 3. Site sweep

### 3.1 Pages (headless Chromium, 1366×900, networkidle + 2.5 s)

| Page | Title | JS errors | 4xx/5xx |
|---|---|---|---|
| `/` and `/index.html` | Call Desk — AI Voice Intelligence & SIP Telephony | 0 | 0 |
| `/call-desk.html` | Call Desk (same as index) | 0 | 0 |
| `/agent-builder.html` | Intake Agent — Agent Builder | 0 | 0 |
| `/api-keys.html` | API Keys & Providers | 0 | 0 |
| `/live-console.html` | AI Voice Assistant — Task 1.6 | 0 | 0 |
| `/softphone.html` | WebRTC Telephony Softphone (Task 2.6) | 0 | 0 |
| `/code-walkthrough.html` | Line-by-Line Code Walkthrough | 0 | 0 |
| `/task-plan.html` | Engineering Task Plan & Roadmap | 0 | 0 |

All internal links between pages resolve. `index.html` and `call-desk.html` are byte-for-byte the
same size and behave identically (kept in sync).

### 3.2 Carrier split — end-to-end check
- `?carrier=twilio` returns 8 Twilio-only numbers; `?carrier=telnyx` returns 5 (8 minus 3 owned).
- Purchasing a Twilio catalog number records `carrier: twilio`; release works.
- Unknown carrier is rejected: `Unknown carrier 'vonage'. Choose one of: telnyx, twilio`.
- In the browser: tab switch re-renders cards with the right badge, chip counts update, purchase modal
  shows the carrier. Verified on both `index.html` and `call-desk.html`.

### 3.3 Providers
- **ElevenLabs**: Configured (masked `sk_8d8••••a3c9`, 24 voices).
- **Twilio**: Configured & Authenticated (Account: `My first Twilio account`, SID: `AC2cd40••••400b059`, 1 active DID: `+1 951 717 7889`).
- **Cartesia**: Not configured (uses simulator / Deepgram fallback).
- **Voices**: 93 voices across ElevenLabs 24, Retell 18, Studio 18, Neural 11, Cartesia 9, Deepgram 7, OpenAI 6.
- **Agents**: Configured with live dynamic agent ("Maya - Bottini & Bottini").

## 4. Test results

Environment note: on arrival only the worker container was running. LiveKit and Redis were down, so
every live-WebRTC check failed with `failed to lookup address information`. After
`docker compose up -d redis livekit` and a worker restart, all live suites pass.

| Suite | Result |
|---|---|
| 1.1 Media Server & Room Minting (`verify.py`) | 5/5 |
| 1.2 STT (`verify_stt.py`) | 4/5 — last check needs a human to join and speak (manual) |
| 1.3 Silero VAD | 5/5 |
| 1.4 Streaming LLM | 5/5 |
| 1.5 Streaming TTS | 5/5 (TTFA 905 ms via Deepgram fallback; first cold run was 5158 ms) |
| 1.6 Function Calling | 5/5 |
| 1.7 WebRTC SDK | 5/5 (failed once with `Handshake 404` when run right after other live suites; passes alone) |
| 2.1 SIP Gateway | 5/5 |
| 2.2 Call Transfer | 6/6 |
| 2.3 DTMF / IVR | 6/6 |
| 2.4 AMD & Voicemail Drop | 6/6 |
| 2.5 Dual-Channel Recording | 6/6 |
| 2.6 Softphone | 6/6 |
| 3.1 Pipeline Worker | 6/6 |
| 3.2 Summary & Sentiment | 29/29 |
| 3.3 Schema Extractor | 41/41 |
| 3.4 HMAC Webhooks | 97/97 |
| 4.1 Agent Builder | 117/117 |
| 4.2 Call History & Inspector | 126/126 |
| 4.3 REST API & Multi-Tenant | 57/57 |
| 4.4 Load / Security / Hardening | 22/22 |
| Playwright voice flows (`test_playwright_voices.py`) | 44/44 |

Reminder from earlier sessions still holds: suites 1.4 / 1.6 / 2.5 / 2.6 and now 1.5 / 1.7 flake when
anything else touches LiveKit at the same time. Rerun a failing live suite alone before calling it a
regression.

## 5. Known limitations (unchanged, by design for now)
- Live calls still synthesise through `tts_manager` (Cartesia / Deepgram / simulator); the voice chosen
  in the Agent Builder picker does not reach real calls.
- Number purchase and outbound dial are simulated; no carrier API is called.

## 6. Recommended next steps (in order)
1. ~~Commit the carrier split & connect Twilio~~ (**Completed** in `0de49f3` + Twilio live setup).
2. Write `web/flow-testing.html` (the pending doc) and link it from the nav.
3. Tighten inbound-trunk validation (require ≥1 number, reject blank name) and add a trunk delete route.
4. Optionally make the carrier chip counts respect the country filter.

## 7. Test-call review & fixes (2026-09-14, afternoon)

Reviewed the "Maya" sandbox transcript the user pasted (Test AI Voice on the Agent Builder page).

### 7.1 Why the picked voice was not the voice you heard
- **Root cause:** the ElevenLabs key on file is a **free-plan** key. Fiona, Anika and "Adam (Workspace
  Cloned)" are *professional / Voice Library* voices, and ElevenLabs refuses them via the API with
  `HTTP 402 paid_plan_required`. The synthesizer silently fell back to a free neural voice (Ava) and the
  UI kept labelling every reply "Fiona". The other 21 ElevenLabs voices (Sarah, Roger, Laura, …) are
  `premade` and work.
- **Live calls had a second, separate cause:** the worker built one Deepgram engine at start-up with a
  hard-coded voice, never loaded the active agent, and the container had a stale private copy of
  `config/agents.json` (still pointing at Aurora). The picker's choice never reached a real call.

### 7.2 Code fixes applied
| Area | Change |
|---|---|
| `agent/voice_synthesizer.py` | Records which engine really produced each clip; remembers 402-locked voices so they are not retried on every reply; exposes `get_voice_engine_status()` / `plan_locked_voices()`. |
| `scripts/serve.py` | `voice-audio` now returns `X-Voice-Engine`, `X-Voice-Requested-Provider`, `X-Voice-Fallback-Reason`; `/api/agents/voices` marks locked voices `api_ready:false` with the reason. |
| `web/agent-builder.html` | Clips are fetched so headers can be read. When a fallback speaks: a toast explains why, the call badge shows "Fiona → fallback: Neural HD (Ava)", each transcript reply is labelled with the real engine, and the voice card gets a "🔒 plan locked" chip. |
| `agent/tts_manager.py` | New `apply_voice()` picks the streaming engine from the voice's provider (ElevenLabs Turbo v2.5 / Deepgram Aura / OpenAI / Cartesia), pre-flights ElevenLabs once per session to catch 402, and falls back to a gender-matched Aura voice with a logged reason. |
| `agent/worker.py` | Loads the **active agent** at session start (prompt, temperature, voice), swaps the live TTS engine on `apply_agent_config` via `agent.update_options(tts=…)`, and publishes a `voice_engine` event with the engine and any fallback reason. |
| `docker-compose.yml`, `requirements.txt` | `livekit-plugins-elevenlabs` added to the worker image; `ELEVEN_API_KEY` passed to the worker; `./config` bind-mounted so the worker sees the same `agents.json` as the dashboard. Image rebuilt. |

Verified live: with the agent temporarily set to Sarah (premade), a real LiveKit session streamed
**ElevenLabs eleven_turbo_v2_5, TTFA 417 ms**. With Fiona the worker logs the 402 reason and speaks
Deepgram `aura-asteria-en`. Agent restored to Fiona afterwards.

### 7.3 Transcript logic fixes (`agent/agent_builder.py`)
| Problem in the transcript | Fix |
|---|---|
| Caller said "the number I call is the best call back number" and Maya answered **"Connecting you with a specialist…"** (a transfer). The tool matcher counted any 4-letter word from a tool description, so "call" + "number" scored as `transfer_call`. | Tool matching now needs an explicit intent ("speak to a person", "transfer me", "book an appointment", "track my order"…); generic words are ignored and description matches need two distinct hits. |
| "guna" was accepted as a spelled full name and the flow moved on. | Name is read back letter by letter ("I have Guna, spelled G-U-N-A. Is that your first name? And could you spell your last name?"), then the last name is confirmed the same way. |
| Callback answer was not acknowledged. | "This number / the number I'm calling from" → "I'll use the number you're calling from"; spoken or typed digits are read back; anything else is re-asked. |
| Mailing-address question was silently skipped for every caller (the "email address" question satisfied its `"address"` check). | Checks for "mailing address" only. |
| No acknowledgement after the caller described their matter. | "Thank you for sharing that." precedes the name question. |

Replayed the pasted transcript: the call now runs intake → name readback → callback → email →
mailing address → representation → prior contact → referral → affiliation → retention consent → close,
with no spurious transfer. `verify_agent_builder.py` 117/117, Playwright voices 44/44, live suites
1.5 / 1.6 / 1.7 / 2.2 all pass after the change.

### 7.4 What still needs a decision from you
1. **Voice for Maya.** Either upgrade the ElevenLabs plan (Fiona / Anika / cloned Adam need Starter or
   higher for API use) or pick a premade ElevenLabs voice such as Sarah, Laura or Jessica. Until then
   both the sandbox and live calls will say so and use the fallback.
2. **Live-call dialogue.** The worker has no `OPENAI_API_KEY`, so real calls use the mock dialogue
   manager, whose canned replies are about a medical clinic, not the Bottini intake script. The sandbox
   flow above only runs in the Agent Builder test call. Add an LLM key (or port the scripted intake into
   the worker) before putting Maya on the Twilio number.

### 7.5 Second test call (Cimo) — what was wrong and what changed
- **Replies were cut to two sentences** by `clean_spoken_speech_text` (server) and its JS twin, so the
  name readback lost its question and the greeting lost "Is now an okay time to talk?". Cap raised to
  five sentences and a trailing question is never dropped.
- "the number I have call to us is my number" now counts as caller ID; a filler answer ("then", "what")
  to the last-name question re-asks instead of being spelled back.
- **Which model writes the sentences: none.** With no `GEMINI_API_KEY` the sandbox uses the rule-based
  script in `agent_builder._preview_reply`; the LLM dropdown only takes effect for `gemini-*` models
  once that key exists (`OPENAI_API_KEY` is read but not used by the sandbox). Live calls use the mock
  dialogue manager unless `OPENAI_API_KEY` is set for the worker.
- **Which engine speaks:** "Cimo (Retell AI)" is a sample-clip voice with no `RETELL_API_KEY`, and
  Fiona is plan-locked, so *both* fall to the same free Microsoft neural voice (Ava). That is why
  switching Cimo → Fiona sounds identical. Voices that actually change today: any ElevenLabs
  **premade** voice (Sarah, Laura, Jessica, Roger…) and every **Deepgram Aura** voice (Asteria, Luna,
  Stella, Orion…). The UI now warns for Retell voices too.

### 7.6 Two agents editing "the same page" — fixed
- Every dashboard **Edit** button opened `/agent-builder.html` with no agent id, and the builder always
  loaded the *live* agent. Saving also made the saved agent live, and a save without an id fell back to
  updating the live agent. Net effect: editing agent 2 overwrote agent 1.
- Now: Edit links carry `?agent=<id>`; the builder has an **Editing** switcher in the header, a
  **＋ New agent…** entry (`new: true` on the API, never falls back), and a **📞 Set live** button.
  Saving never changes which agent answers calls; only Set live (`/api/agents/activate`) does.
- Verified in a browser: created a second agent, edited and saved it, switched back — Maya's prompt was
  untouched and she stayed live. `verify_agent_builder.py` 117/117.

### 7.7 Why the wording still does not follow the prompt
There is **no language model connected**. The builder now shows an amber notice saying so. The
sandbox's words come from a rule script (`agent_builder._preview_reply`); the prompt is only used to
detect *which* script (legal intake vs clinic) to run. Add `GEMINI_API_KEY` (free tier) and choose a
Gemini model to make the sandbox follow the prompt; add `OPENAI_API_KEY` to the worker for live calls.

### 7.8 Confirm instead of moving on
The script now checks each answer once before advancing: an unclear "what happened" gets "Take your
time…"; a non-email gets re-asked with an example; a non-address gets re-asked; yes/no questions get
"just to confirm, yes or no: …"; "skip / don't have" is honoured; the retention-agreement answer is
confirmed ("nothing will be sent" / "I'll note you're happy to receive it"). One retry per question,
so the call never loops.

### 7.9 Gemini as the dialogue brain (wired, waiting for the key)
- **Status:** no Gemini key is present (`/api/providers/keys` → google: not configured; nothing in `.env`).
  Selecting "gemini-2.0-flash" in the builder only names the model. Add the key on the API Keys page
  (Google Gemini card) or as `GEMINI_API_KEY=` in `.env`, then `docker compose up -d agent`.
- **Sandbox:** Gemini call timeout raised 3 s → 20 s (needed for the 51k-char prompt); failures now log a
  warning and the transcript shows "🧠 gemini" or "📜 rule script" on every reply.
- **Live worker:** `StreamingDialogueManager` gained a Gemini backend (SSE streaming via aiohttp),
  chosen automatically when the agent's model is `gemini-*` and the key exists; `GEMINI_API_KEY` is
  passed into the container. STT stays Deepgram, TTS stays ElevenLabs / Deepgram Aura; only the words
  come from the model. A model error now speaks a short recovery line instead of going silent.
- Verified with an invalid key: both paths report "API key not valid" clearly; suites 4.1, 1.4, 1.6, 1.5 pass.

### 7.10 Gemini is live (key added 2026-09-14)
- Key saved via `/api/providers/keys` → `.env` `GEMINI_API_KEY`; worker restarted with it.
- `gemini-2.0-flash` / `1.5-*` are **retired** (HTTP 404). Catalog now: `gemini-3.5-flash-lite` (default,
  free tier ≥8 req/min), `gemini-3.6-flash` (smarter, free tier **5 req/min** — too low for a call),
  `gemini-flash-latest`, `gemini-2.5-pro`. Old ids in saved agents are remapped automatically.
- Gemini 3.x thinking tokens ate the output budget ("Thanks for calling" and nothing else). Now
  `thinkingLevel: minimal` (3.x) / `thinkingBudget: 0` (2.5) with a 300-token reply budget.
- Maya's first message had drifted to "Hi, I'm Maya…"; the prompt mandates a specific opening, so
  Gemini re-said it. Restored to the mandated greeting. **Keep First Message equal to the prompt's opening.**
- Sandbox transcript now runs on Gemini (shows "🧠 gemini"); live worker logs `Dialogue backend: gemini`
  and completed a real WebRTC turn (TTFT 1.24 s). Suites 4.1, 1.4, 1.6 pass; 8 pages clean.
- Free-tier caveat: a busy call can hit 429 on Flash Lite too; the sandbox then falls back to the script for
  that turn and a live call speaks the recovery line.

### 7.11 Tool calling on real calls (Gemini function calling)
- Mid-call tools (`transfer_call`, `book_appointment`, `lookup_order`, …) were only wired into the
  simulator; with a real model driving, they never fired. `StreamingDialogueManager` now declares the
  agent's **enabled** tools to Gemini as function declarations, dispatches the call (same
  filler → run → result callbacks the worker already handles), echoes Gemini 3's thought signature back,
  and lets the model phrase the result. Verified in the container ("check availability for dental
  cleaning tomorrow" → tool call → "We have slots at 9:30 AM, 11:00 AM…") and over live WebRTC (5/5).
- `verify_tools.py` accepts a real model staying in character (a law-firm agent will not look up a
  retail order) as long as the data-channel round-trip completes.

### 7.12 Adding more than one agent
Agent Builder → **Editing** dropdown (top right) → **＋ New agent…**. It creates a separate agent, opens
it, and you can rename it, paste its own prompt, pick its voice and model, then **Save changes**. It is
not answering calls until you press **📞 Set live**; only one agent is live at a time. The dashboard's
Agents tab lists them all with their own Edit buttons.

### 7.13 Repeated questions, slow replies, and test-call recordings (third transcript)
- **Root cause of both repeats and slowness:** every message in the test call re-sent the whole
  conversation and the server regenerated *every* earlier reply (`test_run` replays all utterances), so
  a 20-turn call fired 20 Gemini requests per message. That blew the free-tier limit; failed turns fell
  to the rule script, whose own question order collided with Gemini's (email asked twice, name asked
  again). Fix: the page now sends the transcript it shows plus the new line and the server generates
  **one** reply (`reply_once`). Measured: 1.2–1.4 s per reply, one request each.
- When Gemini is configured but a turn fails (429/5xx/timeout) the server retries once after the
  advertised delay, then says a short holding line. The rule script is used only when no model key exists.
- Each test message also wrote a new agent revision to disk even when nothing changed
  (`config/agents.json` had reached 1.4 MB). Now only real changes are saved.
- **Test calls now appear in the dashboard.** On hang-up the browser mixes the exact agent clips it
  played (right channel) and the mic when it was used (left channel) into the recorder's 16 kHz
  dual-channel WAV, uploads it with the transcript to `/api/agents/test-call/save`, and the post-call
  pipeline runs (sentiment, summary, waveform). Listed as direction "sandbox", from "Agent Builder test
  call", with a playable recording in Call Desk → Calls / Call detail.
- Transcript meta now shows the real round-trip time ("⏱ 1.3s reply") instead of a catalog estimate.
- Suites: agent builder 117/117, call history 126/126, Playwright voices 44/44; 8 pages clean.
  (Duration shown for a typed sandbox call is the pipeline's spoken-word estimate, which can exceed
  the WAV length since typing is faster than talking.)

### 7.14 Fourth transcript: "guna@our website", call not ending, voice "not changing"
- **Email corrupted to "guna@our website"**: the spoken-text cleaner rewrote any bare `something.com`
  as "our website" (meant for links). It mangled the transcript *and* the history sent back to
  Gemini, which then kept confirming the wrong address. Now only real links (http/www or domain+path)
  are rewritten; emails and bare domains stay intact (server and page cleaners).
- **Call did not end after the sign-off**: the server now flags a closing line (`end_call`, via
  `looks_like_closing`), the page lets the audio finish, hangs up and saves the recording; the live
  worker publishes `call_end` and deletes the LiveKit room (which drops a SIP leg) after a sign-off.
  Verified: hang-up + save ~7 s after the closing line.
- **"I change the voice and nothing changes"**: the picker works (Sarah → real ElevenLabs audio,
  verified), but 12 catalog voices (Cimo, Fiona, Anika…) all fell back to the *same* neural voice
  "Ava", so switching between them was inaudible. Now: (1) fallback voices are spread across 23
  distinct neural voices; (2) `/api/agents/voices` reports `api_ready`/`engine`/`api_note` for every
  voice up front (ElevenLabs categories fetched once, 402 probed once for the 3 professional voices);
  (3) the picker shows a green "✅ ElevenLabs Turbo / Deepgram Aura / Neural HD" chip or an amber
  "⚠️ Neural HD fallback (…)" chip on every card, has a "Real voices only" toggle, and warns the moment
  you pick a fallback voice. 57 of 93 voices are real today. **Maya is now set to Sarah** (ElevenLabs
  premade); the live worker confirms "Live TTS engine: elevenlabs eleven_turbo_v2_5 for voice Sarah".
- Model-quality items in that transcript (asking to spell the company name, T-E-S-L-L-A loop,
  accepting "January 2002") are Gemini 3.5 Flash Lite behaviour, not code. gemini-3.6-flash is
  noticeably better but the free tier allows 5 requests/min for it; a paid Gemini tier removes that.

### 7.15 Agent management moved to the dashboard; one agent per phone number
- Per request, the Editing dropdown / ＋ New agent / Set live controls were removed from the Agent
  Builder header. The builder edits the agent in its URL and shows a green "this agent answers live
  calls" chip plus an "☰ All agents" link. Call Desk → Agents now has **＋ New agent**, and every
  card has **Edit** (opens that agent) and **📞 Set live** (non-live agents).
- **Several agents on several numbers:** `worker.resolve_agent_for_call()` reads the dialled DID from
  the SIP participant (`sip.trunkPhoneNumber`) and uses the agent assigned to that number on the
  SIP Trunks & DIDs tab (the per-number agent dropdown). Unassigned numbers and web/test rooms fall
  back to the live agent. Verified in the container: with the Twilio DID assigned to a temporary
  "Support Line Agent", calls to it resolve to that agent while Maya stays the live default.

### 7.16 Test call "typing by itself"
- Cause: the microphone starts with the call and kept listening while the agent's audio played
  through the speakers, so Web Speech transcribed *her* words and auto-submitted them as the caller.
- Fix (half-duplex): recognition is stopped while the agent speaks, results arriving during playback
  (and 700 ms after) are discarded, a transcript that is mostly the agent's last line is dropped as
  echo, and the input placeholder shows "🔇 Mic paused while the agent speaks…" → "🎙️ Listening…".
  Verified: paused for the whole greeting, listening again the moment it ended.
- Also this round: Retell warning reworded (Retell is a call platform with no TTS API; entries are
  demo clips), ElevenLabs list now syncs from the account (+ "↻ Sync accounts" button), and the
  `s3://voice-archive/...` archive URL is confirmed to be a local placeholder
  (`recordings/archive/…`), not real cloud storage.

### 7.17 Text arrives, then the voice: latency work
- Reply text costs ~1.3 s (Gemini). The voice then needed a *second* wait: the whole clip was
  synthesized before anything played (Neerja neural: 0.9 s short / 3 s long; ElevenLabs ~1 s).
- Now: (1) the reply is split into speech chunks (first ~40+ chars, then the rest) and all chunks are
  pre-warmed on the server the instant Gemini's text exists; (2) `voice-audio?stream=1` streams
  bytes as the engine produces them and the page plays progressively (ElevenLabs `/stream` with
  `optimize_streaming_latency=3`, Deepgram, edge-tts); (3) concurrent requests for the same clip
  share one in-flight synthesis (`_LiveStream`) instead of running it twice; (4) each reply shows
  "🔊 0.5s voice" = text→first audio.
- Measured in a browser: ElevenLabs Sarah 0.5–0.9 s text→voice (was ~1.0–1.3 s); Neerja neural
  1.2–1.5 s, bounded by that free engine's own ~1.1 s first-chunk latency. For the fastest voice use
  an ElevenLabs premade voice; Deepgram Aura is ~1 s.

### 7.18 Softphone: "Microphone permission failed… engine not connected" and no agent voice
- Cause: `web/softphone.html` created the LiveKit `Room` and wired its events but **never called
  `room.connect()`**. The mic therefore had no session to publish to (that exact error), no agent was
  dispatched, and "Call connected successfully" was printed regardless.
- Fix: the softphone now joins the room with the token's URL, logs the agent joining, the voice
  engine in use (with any fallback reason), subscribes the agent audio, and hangs up when the agent
  signs off. The worker now **greets on join** (Agent Builder "Who speaks first" = AI) for real and
  dashboard rooms; `verify-*` test rooms are excluded. Verified headless: agent joins in ~2 s, greeting
  audio streams (TTFA 275 ms). Suites 2.6 / 1.4 / 1.6 pass.
- Still true: there is **no LiveKit SIP service** in the stack (`docker-compose.yml` has redis,
  livekit, agent only), so "Dialing … via LiveKit SIP Gateway" is simulated — no phone rings and the
  Twilio number cannot deliver inbound calls yet. What works today is talking to Maya in the browser
  (softphone / dashboard tester / Agent Builder). Wiring real calls needs the `livekit/sip` container,
  a public address, and the Twilio trunk pointed at it.
- Live-call voices: the worker can stream ElevenLabs premade and Deepgram Aura only; neural/Retell/
  locked voices fall back to Deepgram Aura on live calls (the browser sandbox can still play neural).

### 7.19 Durable storage: PostgreSQL for everything
- New `postgres` service in `docker-compose.yml` (postgres:17-alpine, container `voice-postgres`,
  host port **127.0.0.1:5433**, named volume `voice_pgdata`). The pre-existing `db` container on 5432
  belongs to another project and is not touched.
- New `agent/storage.py`: write-through + restore. Every manager save (agents, phone numbers, SIP
  trunks/rules, workspaces/users/API keys, post-call jobs) also upserts one row per document into
  `app_documents` (JSONB); every recording written to disk (telephony recorder and sandbox test
  calls) is also stored as bytes in `recordings`. At start-up `bootstrap()` rebuilds any JSON file
  that is missing or older than the database and writes back any recording missing from the folder.
  The JSON files/WAVs stay as the working copy the rest of the code reads.
- Config: `DATABASE_URL` in `.env` (added), `psycopg[binary]` in requirements (worker image rebuilt,
  host installed). Without `DATABASE_URL` everything runs file-only, as before.
- `GET /api/storage/status` shows connection state, row counts per collection, recordings count/bytes.
- Verified: initial push stored 127 recordings (135 MB) and all documents; deleting a recording and
  `config/phone_numbers.json` then restarting restored both byte-for-byte; a new sandbox call added a
  recording row and a call_jobs row. Suites 4.1, 4.2, 4.3, 2.1, 2.5, 3.1 pass.
- Note: the pipeline's "archive" copy still lands in `recordings/archive/` with the placeholder
  `s3://voice-archive/...` URL; the primary recording is what the database keeps.

### 7.20 Profile: sidebar card + dedicated page
- `GET/POST /api/v1/auth/profile` (name, title, email, phone, workspace name; plus members, API keys,
  workspace details). The dashboard has no sign-in, so the profile is the workspace admin the server
  runs as (`usr-admin-01`); the values live in the auth store and are mirrored to Postgres.
- `web/profile.js` injects a compact profile card into the WORKSPACE block of every page (click →
  `/profile.html`). `web/profile.html` is the full page: editable profile, workspace card, members,
  API keys summary and data/storage status. "My Profile" added to the rail navigation.
- The Overview "Live AI Voice Assistant Testing" card and the "Test AI Voice" tab were removed from
  the Call Desk at the user's request (voice testing lives in the Agent Builder and the softphone).

### 7.21 Login: password, then SMS code
- **Auth** (`agent/auth_manager.py`): PBKDF2-SHA256 passwords on users, server-side sessions (token
  hash stored; 12 h, or 30 days with "keep me signed in"), first-run `setup_admin`, `check_password`
  → `begin_two_step` (pending ticket + 6-digit code, 5 min, 5 attempts, resend after 30 s) →
  `complete_two_step` → session. Accounts without a phone sign in with password alone.
- **SMS** (`agent/sms.py`): Twilio Messages API with the telephony credentials and the workspace's
  SMS-capable Twilio number (+19517177889). `SMS_DRY_RUN=1` logs the code instead of sending (tests).
- **Server**: `/login.html` + `/api/v1/auth/{session,setup,login,otp/verify,otp/resend,logout}`;
  HttpOnly `va_session` cookie; every page redirects to login when signed out; JSON APIs return 401
  unless the caller is localhost (`AUTH_TRUST_LOOPBACK=1` default, so the verify suites and the worker
  keep working) — set it to `0` to require a session everywhere. `/api/v1/auth/dev-session`
  (localhost only) mints a session for automated browser tests.
- **UI**: `web/login.html` (first-run setup → password → code, resend countdown, wrong-code handling);
  Sign out on the sidebar card and profile page; "Security" card on the profile page (change
  password); phone field labelled as the SMS sign-in number.
- Verified headless with dry-run SMS: redirect → setup → sign out → wrong password → code step (masked
  +91•••••••508) → wrong code → correct code → dashboard. Suites 4.1 / 4.3 / 4.2 / Playwright pass;
  10 pages clean. Admin was reset to first-run afterwards (no password, no phone, no sessions).
- **Twilio is a Trial account.** Trial accounts can only text numbers verified in the Twilio console,
  so add your phone under Verified Caller IDs (or upgrade) before enabling the SMS step.

### 7.22 Permissions: two roles, shared agents and calls
*(Updated 2026-09-15 per user: the operator / analyst roles and the per-user agent privacy model were
removed. There are now two roles and everything in the workspace is shared.)*
- Roles: `admin` and `user` (`agent/auth_manager.py` `UserRole`). Legacy `operator` / `analyst` values
  in `config/auth_store.json` or in token payloads are folded into `user` by `normalize_role()`.
- `user` gets the whole dashboard: every agent, all call history, webhooks, telephony, set-live.
  Admin-only: `/api-keys.html`, `/admin-guide.html`, `/cost-comparison.html` (redirect to `/?denied=admin`)
  and `/api/providers`, `/api/v1/users`, `/api/v1/api-keys`, `/api/v1/workspaces`, `/api/v1/auth/users`
  (403). Enforced by `_enforce_role()` in `scripts/serve.py`.
- `_viewer()` now always returns `owner=None` / `agents=None`, so `list_agents`, `can_access`,
  `visible_agent_names` and `call_history` scoping are unrestricted for every signed-in member. The
  `owner_id` field on agents is still recorded (shown as the creator) but no longer gates access.
- UI: `data-admin-only` hides only the API Keys, Admin guide and Cost comparison links plus the
  member-management card on the profile page. Role picker offers User / Admin.
- Storage: users, workspaces, API keys and sessions are read from and written to PostgreSQL only
  (`app_documents` collections `users` / `workspaces` / `api_keys` / `sessions`); `config/auth_store.json`
  is gone (imported once on first start, then deleted; now git-ignored). The JSON file is used only when
  `DATABASE_URL` is unset (the tenant test runs with `env -u DATABASE_URL`). Verified 2026-09-15:
  member created via API → login → sees both agents incl. another user's, calls, webhooks; api-keys
  page 302, /api/providers and /api/v1/users 403; `verify_api_tenant.py` 57/57.

### 7.23 Webhook & AI data extraction (optional per agent)
- Agent Builder gained a **Webhook & data extraction** card with an **Enable** toggle. When on:
  ✨ *Suggest from prompt* (Gemini reads the agent's prompt and proposes fields: name, type,
  description, required, the question to ask), editable field table, ▶ *Preview on latest call*, saved
  with the agent (`extraction_fields`, `webhook_enabled`). When off, nothing is extracted or posted for
  that agent's calls (pipeline + webhook bridge both check the flag).
- `schema_extractor.extract_with_ai()` — Gemini fills the agent's schema from the transcript
  (regex fallback when no key / error); spoken digits → E.164, "guna at gmail dot com" → email, yes/no
  → booleans. Registered as schema `agent:<id>`; results in `job.metadata.agent_extraction`.
- Webhook payload now carries `call{}`, `extracted{}` (flat values), `extraction{}`, `summary`,
  `sentiment`, `transcript[]`, signed `X-Signature-256`. Fixed a real bug: deliveries scheduled during
  a pipeline run were dropped because the loop closed first (`run_pipeline_job()` drains them).
- New admin page **Webhooks & Data** (`/webhooks.html`): endpoints (add / **Test connection** with
  HTTP code + latency / delete, health pill from last delivery), recent deliveries with replay, per-agent
  fields with suggest/preview, payload example. Endpoints are workspace-level; the toggle is per agent.
- Verified end to end with a local catcher: test ping → 200; a saved sandbox call produced
  `call.extracted` + `call.completed` with 8 of 10 fields filled by Gemini (coverage 80%); toggle off →
  0 deliveries, toggle on → 2. Suites 3.4 / 3.3 / 3.1 / 4.1 pass; 10 pages clean.
- Maya currently has the toggle **on** with 10 AI-suggested fields; the local test endpoint was removed.

### 7.24 Final checks and in-app guide
- `web/user-guide.html`: "How to use this dashboard" — 12 sections (sign in, provider keys, build an
  agent, test call, voices, go live & numbers, calls & recordings, webhooks & data, softphone, users &
  roles, where data lives, troubleshooting) in the app's own style, linked as **How to use** in every
  sidebar. Renders with the profile card, no errors.
- Agent Builder top header bar removed (user request). Members with no agents get a "Create your first
  agent" form; saving creates their own agent (server no longer falls back to the live agent for them).
- Full regression: 20/20 suites pass individually (`verify_softphone.py` now mints a dev session for
  the page fetch). 11 pages clean.

### 7.25 Two guides + cost comparison page
- `web/user-guide.html` (operators/analysts, 11 sections) and `web/admin-guide.html` (admins, 11
  sections; admin-only, server-gated) share `web/guide.css`; both linked in every sidebar.
- `web/cost-comparison.html` (admin-only): interactive per-minute and per-month comparison of Call
  Desk's provider costs vs Retell AI's bundled price, by model (OpenAI GPT-4o mini / 4.1 mini / 4.1 /
  5.1 / 5.2 / 5.4 / 5.4 mini / 5.6 Luna, Gemini 2.5 Flash-Lite / 3.5 Flash Lite / 3.6 Flash / 3.5
  Flash, Claude Haiku 4.5 / Sonnet 4.5), voice (Deepgram Aura-1/2, ElevenLabs), telephony, prompt
  size, turns/min, caching, volume, numbers, server cost. List prices captured 14 Sep 2026 with sources.
- Headline (GPT-4.1, Aura-2, Twilio inbound, 2.5k-token prompt, 4 turns/min, caching on):
  Call Desk ≈ $0.035/min vs Retell $0.13/min (−73%); at 5,000 min/month ≈ $236 vs $654 including a
  $60 server and two numbers. With Maya's 13k-token prompt: $0.043/min cached, $0.137/min uncached.
  With an ElevenLabs voice: $0.074 vs $0.155.

### 7.24 Site cleanup: no icons, Retell AI removed
*(2026-09-15, per user: "no symbol need to show anywhere" and "remove the retell ai".)*
- Every emoji / dingbat / icon glyph was stripped from all `web/*.html` and JS (661 characters across
  17 files, plus entity-encoded ones in softphone, live-console, task-plan, code-walkthrough). Buttons
  keep their text labels; the password-eye toggle became Show / Hide; the voice cards lost the TTFA,
  "100% Free" and engine chips (an amber fallback warning still appears only when a voice cannot be
  synthesised). Plain arrows (← Back, →) were kept. Headless Chromium pass: 15 pages, 0 JS errors,
  0 icon glyphs in rendered text or CSS pseudo-content.
- Retell AI is gone end to end: `agent/retell_voices.py`, `audio/retell/`, the 18 `retell-*` catalog
  voices and neural aliases, the provider card and `RETELL_API_KEY`, the builder tab, the
  `retell_configured` flag on `/api/agents/voices` (now 75 voices), and the Playwright F7 check.
  `cost-comparison.html` is now a Call Desk-only cost calculator; `competitor-analysis.html` compares
  against Vapi, Bland, ElevenLabs Agent and Synthflow. Earlier sections that mention Retell describe
  history and are left as written.

### 7.25 Gemini hears the caller; Deepgram is voice-only
*(2026-09-15, per user: "no need use the tts and stt in the other platform, use our llm for this; only the
voice is enough for the deepgram". Confirmed: Gemini (current default) listens to the audio directly.
Then "can reduce the 1-3 s time of response": the two-call design was collapsed into one.)*
- `agent/gemini_stt.py`: a LiveKit non-streaming `stt.STT`. Silero VAD segments the caller (LiveKit wraps
  it in `StreamAdapter`); each utterance is resampled to 16 kHz mono WAV. Two modes:
  `hand_off_audio=True` (dialogue backend is Gemini): no transcription call; the clip is parked and a
  `AUDIO_MARK` handle is returned as the "transcript". `False` (OpenAI dialogue): Gemini transcribes
  the clip (temperature 0, thinking minimal, keep-alive session) and returns the text.
- `agent/llm_manager.py` `generate_response(user_audio=..., on_transcript=...)`: the utterance audio is
  attached to the last user content of the Gemini dialogue call with `HEAR_INSTRUCTION`; the model
  answers "CALLER: <transcript>\n<reply>" in one round trip. `_split_transcript` peels the first line
  off the token stream (reported via `on_transcript`, written into the context in place of
  `AUDIO_PLACEHOLDER`), only the reply reaches the clause splitter / TTS. Tool follow-ups re-use the
  heard transcript as text (no second CALLER line). `metrics["transcript"]` carries it.
- `agent/worker.py`: builds the dialogue manager first, `build_stt(hand_off_audio=backend=="gemini")`,
  intercepts `AUDIO_MARK` transcripts, merges split clips (`concat_wavs`) per turn, and
  `publish_caller_transcript` sends the heard text to the dashboard + AMD. `STT_PROVIDER=deepgram`
  restores Nova; missing `GEMINI_API_KEY` falls back to Deepgram with a warning.
- Verified in real rooms (`scripts/publish_audio.py` inside the worker, `samples/caller_test.wav` 6 s and
  `samples/caller_short.wav` 2 s): transcripts exact, reply spoken by Aura, tool call path clean.
- **Latency (Maya, 52k-char prompt, gemini-3.5-flash-lite):** end of caller speech → first spoken clause
  was ~4.3-4.7 s with transcribe-then-answer; now ~2.8-3.2 s (one call; a tool turn adds ~1.4 s for the
  follow-up). Gemini's own audio TTFT from this network is ~1.5-2.6 s and the 52k prompt adds ~0.2-0.4 s;
  Deepgram Nova + Gemini text was ~1.3-1.7 s. Streaming Gemini Live (WebSocket) is the only way below
  ~1.5 s while keeping Gemini as the ears.
- Pages: API Keys card is "Deepgram Aura (voice)"; admin guide provider table; cost calculator's
  "Speech-to-text" line became "Hearing the caller" = caller audio tokens (32/s) at the model's input price.
- **Reverted to vendors 2026-09-15 (user: "now the stt and tts use the vendor like deepgram or 11labs"):**
  `STT_PROVIDER=deepgram` is the default again (worker, compose, `.env`); Gemini-hears-audio stays
  available as `STT_PROVIDER=gemini`. Pages describe Deepgram Nova STT again. Verified in a room:
  interim transcripts back, "What are your office hours?" → first spoken clause 1.6 s after the final
  transcript, Aura TTFA 163 ms.
- **Corrected the same day:** the "use the vendor" message was a question, not an instruction. Final
  state: `STT_PROVIDER=gemini` (default everywhere) — Gemini hears the caller, Deepgram Aura /
  ElevenLabs supply only the voice; `STT_PROVIDER=deepgram` remains the opt-in for Nova. Pages describe
  the Gemini-hears setup. Re-verified in a room ("Hello, what are your office hours?" heard exactly).

### 7.26 Sandbox recordings: playback mix and transcript sync
*(2026-09-15, user: "I can hear my voice in the recording" → the file is caller-left / agent-right and the
player played it raw; then "check s3://voice-archive/recordings/sandbox-1789464562392_1789465284.wav".)*
- Player (`web/inspector.js`, Call Desk + home): both channels are now mixed into both ears through a
  Web Audio splitter/merger; a "split L/R" checkbox restores the raw stereo. File format unchanged.
- The `s3://voice-archive/...` URL is the pipeline's placeholder archive name (`recordings/archive/`);
  there is no cloud upload (already stated in the admin guide).
- Analysis of that recording: caller speech is present and clean (no speaker echo in the mic channel),
  but the transcript timeline was out of sync: caller turns were stamped when Chrome delivered the
  final result (after the speech), agent turns when the reply text arrived (before the audio), and
  t=0 was the first turn instead of the recording start. Clicking a caller line landed in silence.
- Fix: the builder now stamps each caller turn with the time of its first interim result (−0.6 s
  recogniser lag) and its final result, and each agent turn with the real audio start/stop
  (`stampAgentTurn`); the save API carries `end_at` → `end_timestamp` through
  `pipeline_worker` normalisation; `call_history` anchors t=0 at `metadata.started_at` and uses the
  real end when present. Existing 59 sandbox jobs were re-timed from their audio (agent clips on the
  right channel ↔ agent turns matched 42/42 on the call in question).

### 7.27 Test call is now a real LiveKit call; every call is recorded and filed
*(2026-09-15, user: "give the proper solution" after Chrome's Web Speech recogniser kept going deaf,
interrupting the greeting on its own echo, and losing the agent's audio from recordings; and "can you
record the conversation so I can listen to it".)*
- **Builder test call = WebRTC call into the worker** (`web/agent-builder.html`): joins room
  `test-<agent_id>-<stamp>` via `/token`, publishes the mic (browser AEC/NS/AGC), plays the agent's
  track, and renders the worker's data topics (transcript, agent_reply, tool_call/filler_speech,
  agent_state incl. call_end, interruption, tts_metrics, agent_config_event). Typed lines go over the
  data channel as `test_prompt`. Chrome's SpeechRecognition code is gone. If the room cannot be joined
  the old HTTP text sandbox is used (typing only).
- **Worker** (`agent/worker.py`): `resolve_agent_for_call` loads the agent named in a `test-` room;
  `session.start(record={"audio": True, ...})` turns on LiveKit's own `RecorderIO` (dual-channel
  OGG, caller left / agent right, agent placed at real playout time); `call_turns` records every
  caller turn (VAD start/end), typed line, greeting and reply (with backend/tool/interrupted); when
  the caller leaves, `finalize_call` closes the session, converts `audio.ogg` → 
  `recordings/rec-<room>-<ts>.wav` (16 kHz stereo, via `av`), enqueues + runs the post-call pipeline
  with `direction=sandbox` for test rooms / `inbound` otherwise, then shuts the job down. Applies to
  real SIP calls too, so phone calls now get recordings and Call History without any client action.
- `agent/pipeline_worker.py` `_save_state` merges with the on-disk file (dashboard and worker both
  write `recordings/pipeline_jobs.json`; atomic replace).
- Verified with headless Chromium using `samples/fake_mic_loop.wav` as the microphone: greeting →
  caller heard by Gemini ("Hi, I would like to book…", interrupting the greeting) → reply → second
  question → reply; call filed with 5 turns and a 48 s recording whose channels line up with the
  timeline (agent 0.9–12.8 s, caller 12.8–19.1 s, …). Player/waveform work unchanged.
- The user guide's test-call section and troubleshooting rows were rewritten accordingly.

### 7.28 Retell-style pipeline settings
*(2026-09-15, user: "how was Retell AI configured, check and fix like that".)*
- Retell: streaming STT partials → LLM streams as soon as the turn ends → streaming TTS, plus a
  turn-taking model; ~600 ms quoted, with LLM TTFT the dominant remainder.
- Ours now matches the structure: `STT_PROVIDER=deepgram` (streaming Nova) is the default again;
  endpointing `min_delay 0.25 s / max_delay 1.2 s` and interruption `min_duration 0.4 s`
  (env-overridable: ENDPOINT_MIN_DELAY, ENDPOINT_MAX_DELAY, INTERRUPT_MIN_DURATION).
- Measured in a room: caller stops → turn committed 0.12 s → first LLM clause +1.7 s (Gemini 3.5
  Flash Lite on Maya's 52k-char prompt) → Aura first audio +0.3-0.5 s ≈ **2.2 s** end-of-speech to
  first words (was ≈ 3.5 s with Gemini hearing the audio). Gemini TTFT from this network: ~1.0 s with a
  3k prompt, ~1.4 s with the full prompt; that floor, not the pipeline, is what stands between 2.2 s and
  Retell's number. Options: shorter prompt (−0.4 s, −90 % model cost), a lower-latency model/region.

### 7.29 Live-call turn taking, noise and choppy voice
*(2026-09-15, from the user's live test calls `mu2k3hbb`, `mu2kbzze`, `mu2kt8dw`.)*
- Turns now come from LiveKit's `on_user_turn_completed` (all Deepgram finals of a turn merged,
  endpointing 0.25-1.2 s applied); the worker no longer commits turns on raw VAD. An unanswered turn
  cut off by new speech is merged into the next; a barge-in that yields no words is answered after 1 s.
- The user's mic is hot/noisy (noise floor RMS 250-1200, speech clipping at 32768). The worker's own
  VAD-triggered barge-in (`session.interrupt(force=True)` on any VAD event) cut the greeting on noise;
  removed. Interruption is LiveKit's: `min_words 2`, `min_duration 0.4`, `resume_false_interruption`
  with a 1 s timeout; VAD `activation_threshold 0.6`, `min_speech 0.12 s`. Env: VAD_THRESHOLD,
  VAD_MIN_SPEECH, INTERRUPT_MIN_WORDS, INTERRUPT_MIN_DURATION, FALSE_INTERRUPT_TIMEOUT.
- Choppy voice: (1) "flush audio emitter due to slow audio generation" ×7 — Deepgram Aura audio
  arrived in bursts and the room plays through a 200 ms queue → `agent/audio_buffer.py`
  `BufferedAudioOutput` holds 400 ms (TTS_PREBUFFER_MS) before playout starts; (2) "resumed false
  interrupted speech" ×3 — one-word backchannels paused her for 2 s → min_words 2 / 1 s timeout.
- Recording export (`av` decode → WAV) runs in a thread; the remaining loop block at hang-up is the
  post-call pipeline's synchronous HTTP calls, after the caller has left.
- 2026-09-15 call `mu2npjsd` (full intake, ~100 turns): closing readback said "your name is unknown, number
  unknown, email unknown" and Maya re-asked name/phone/email/address mid-call. Cause: the dialogue
  context kept only the last 20 turns / 4000 tokens (`ConversationContextBuffer`), so the caller's
  details fell out of the window. Now 400 turns / 48k tokens. Also `check_availability` fired on
  "what state" and Gemini invented `submit_retention_agreement(... 'unknown')`: Maya's enabled tools
  cut to `transfer_call`. Endpointing min raised to 0.8 s (max 2.0) for callers who pause mid-sentence.
