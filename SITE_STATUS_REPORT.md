# Site Status Report — 2026-09-14

Scope: pending work check, full site sweep (8 pages, 81 API routes), and the complete verification
suite run against the local dev stack (serve.py on :8091 + docker compose LiveKit / Redis / worker).

## 1. Summary

| Area | Result |
|---|---|
| Web pages (8) | All load, 0 JavaScript errors, 0 failed requests |
| API routes (81) | All respond as designed (GET routes 200, POST-only routes 404 on GET, tenant routes 401 without token) |
| Verification suites (19 + Playwright) | 18 of 19 pass; 1 needs a human to speak (`verify_stt.py`) |
| Playwright voice flows | 44 / 44 checks pass |
| Uncommitted work | None — Carrier split committed in `0de49f3`, live Twilio credentials & DID configured |
| Pending task from last session | `web/flow-testing.html` (flows + testing doc) — **not started** |

## 2. Pending tasks & Carrier Setup

### 2.1 Current Progress & Changes
1. **Carrier Split Committed** — Telnyx and Twilio carrier split committed in `0de49f3`.
2. **Live Twilio Account Connected** — Account SID `AC87faa60d...` & Auth Token authenticated (`retell`, active).
3. **Twilio DID Configured** — Live inbound number `+1 (515) 585-9366` added to `config/phone_numbers.json` and assigned to *Maya - Bottini & Bottini*.
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
- **Twilio**: Configured & Authenticated (Account: `retell`, SID: `AC87faa••••01651a`, 1 active DID: `+1 515 585 9366`).
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
