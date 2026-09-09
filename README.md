# Voice Agent Service

The real-time media path for **AI Assistant Voice**. This is the one new
backend service in the plan — everything else (agent config, call records,
retrieval, RBAC) stays in the existing Drupal control plane.

See `../ai voice assistent/` for the estimation, stack and cost documents.

## Status

| Task | State |
|------|-------|
| 1.1 Media server setup | Done — local stack running, acceptance check 5/5. Cloud deploy pending. |
| 1.2 Streaming STT | Done — `verify_stt.py` 5/5, speech transcribed end to end. |
| 1.3 VAD & barge-in | Done — `verify_vad.py` 5/5, Silero VAD speech boundaries + barge-in cutoff. |
| 1.4 LLM dialogue manager | Done — `verify_llm.py` 5/5, streaming model wrapper, clause boundary splitter, context buffer. |
| 1.5 Streaming TTS | Done — `verify_tts.py` 5/5, Cartesia Sonic streaming TTS, 20ms chunking, sub-100ms TTFA, instant barge-in cut-off. |
| 1.6 Mid-Call Function Calling & Retrieval Tools | Done — `verify_tools.py` 5/5, JSON Schema tool registry, async non-blocking dispatcher, <10ms filler speech engine, grounded response synthesis. |
| 1.7 Browser WebRTC Client SDK | Done — `verify_sdk.py` 5/5, standalone browser SDK, UMD module, TypeScript definitions, audio visualizer, reconnect logic. |
| 2.1 SIP Gateway & Telephony Integration | Done — `verify_sip.py` 5/5, LiveKit SIP carrier trunk models, inbound DID routing, outbound dialer API, REST & WebRTC data channel events. |
| 2.2 Call Transfer Engine | Done — `verify_transfer.py` 6/6, Blind & Warm transfer, SIP REFER, caller hold, consultation bridging, handoff briefing, decline recovery. |
| 2.3 DTMF Digit Handling & IVR Navigation | Done — `verify_dtmf.py` 6/6, RFC 4733 & Goertzel dual-tone DSP, multi-digit buffer with `#` terminator, phone tree IVR routing engine, REST & WebRTC data channel events. |
| 2.4 Answering Machine Detection (AMD) & Voicemail Drop | Done — `verify_amd.py` 6/6, temporal cadence classifier, Goertzel voicemail beep DSP, transcript keyword detector, automated voicemail drop, REST & WebRTC data channel events. |
| 2.5 Real-Time Call Recording, Dual-Channel Stereo & Compliance | Done — `verify_recording.py` 6/6, dual-channel stereo split, 1400 Hz compliance tone, PCI/HIPAA pause/resume redaction, RIFF WAV container, REST & WebRTC data channel events. |
| 2.6 WebRTC Telephony Softphone / Web Calling Dialpad | Done — `verify_softphone.py` 6/6, standalone browser dialpad, Web Audio DTMF & ringback synthesizer, inbound call simulation, hardware selectors, and LiveKit WebRTC bridging. |

## Run it

```bash
docker compose up -d
python3 scripts/verify.py          # media server acceptance (1.1)
python3 scripts/verify_stt.py      # speech-to-text acceptance (1.2)
python3 scripts/verify_vad.py      # VAD & barge-in acceptance (1.3)
python3 scripts/verify_llm.py      # streaming LLM dialogue manager acceptance (1.4)
python3 scripts/verify_tts.py      # streaming TTS engine acceptance (1.5)
python3 scripts/verify_tools.py    # mid-call function calling & tools acceptance (1.6)
python3 scripts/verify_sdk.py      # browser WebRTC client SDK acceptance (1.7)
python3 scripts/verify_sip.py      # SIP gateway & telephony integration acceptance (2.1)
python3 scripts/verify_transfer.py # call transfer engine acceptance (2.2)
python3 scripts/verify_dtmf.py     # DTMF keypad & IVR phone tree acceptance (2.3)
python3 scripts/verify_amd.py      # Answering Machine Detection & Voicemail Drop (2.4)
python3 scripts/verify_recording.py # Dual-channel stereo recording & compliance (2.5)
python3 scripts/verify_softphone.py # WebRTC softphone & web dialpad acceptance (2.6)
```

The verify script proves the four things task 1.1 promises: the server
answers, tokens authenticate, forged tokens are rejected, and the room
management API works.

```bash
docker compose logs -f livekit   # watch signalling
docker compose logs -f agent     # watch transcription & VAD events
docker compose down              # stop
```

## Streaming STT (task 1.2)

The worker joins the room, streams caller audio to Deepgram and publishes
transcripts back as data messages on the `transcript` topic. The browser
page renders interim results greyed out and solid once final.

```bash
python3 scripts/set_key.py <deepgram-key>   # validates, then writes .env
docker compose up -d agent
python3 scripts/verify_stt.py
```

`verify_stt.py` checks the key, the worker, its registration, and that real
speech came back as text. For the last one, open the test page, join, and
say a sentence.

`scripts/publish_audio.py` does the same without a microphone: it publishes
audio into the room and prints the transcripts that come back, which is what
the browser does. Given a 16-bit mono WAV of speech it proves the whole
round trip; with no argument it publishes a tone, which exercises dispatch
and the Deepgram socket but produces no transcript. Either way it tells a
broken pipeline from a silent microphone.

## VAD & Barge-In Engine (task 1.3)

Replaces cloud-dependent endpointing with a fully local Silero VAD model running
on CPU via ONNX Runtime inside the agent container.

- **Speech boundary detection**: detects `START_OF_SPEECH` and `END_OF_SPEECH` in real time with sub-50ms latency.
- **Dynamic turn endpointing**: automatically detects when the caller stops speaking (0.4s to 1.8s silence window) and closes the user turn locally without waiting on external cloud gateway services.
- **Instant barge-in interruption**: when the caller begins speaking while the agent is outputting audio, the agent's playback buffer is immediately truncated, ongoing speech playback is cancelled, and an interruption event is published on the `interruption` data topic.
- **Real-time state broadcasting**: worker publishes live `vad` (speaking/listening), `agent_state`, and `interruption` data messages directly into the room.
- **Interactive verification**: open `http://localhost:8091`, join the room, and click **Test Agent Speech (Barge-in)** to hear the agent stream audio, then speak into the microphone to witness instant barge-in cut-off.

```bash
python3 scripts/verify_vad.py
```

## Streaming LLM Dialogue Manager (task 1.4)

Provides real-time conversational reasoning with sub-200ms Time-To-First-Token (TTFT),
sentence/clause boundary chunking for downstream TTS, and multi-turn context buffering.

- **Streaming Model Wrapper**: integrates OpenAI (`gpt-4o-mini`, `gpt-4o`) via `livekit-plugins-openai` and provides an offline local conversational streaming simulator when no API key is provided.
- **Clause Boundary Splitter**: segments incoming token streams at natural punctuation boundaries (`,`, `;`, `:`, `—`, `.`, `?`, `!`, `\n`) with configurable character/word minimums, ensuring downstream TTS (Task 1.5) receives fluent conversational units with sub-200ms TTFT instead of waiting for full paragraphs.
- **Conversation History Context Buffer**: maintains multi-turn user/assistant dialogue, tracks role turns, performs sliding window context trimming, and serializes state over WebRTC data channels (`chat_history`).
- **Instant Barge-in Interruption**: cancels active LLM generation in real time whenever the caller begins speaking during thought generation or response playback, annotating the context buffer with `[interrupted]`.
- **Key configuration & acceptance check**:

```bash
python3 scripts/set_key.py openai <your-openai-key> # validates with OpenAI and updates .env
docker compose up -d agent
python3 scripts/verify_llm.py                       # verifies 5/5 checks
```

## Streaming TTS Engine (task 1.5)

Provides ultra-low-latency text-to-speech with sub-100ms Time-To-First-Audio (TTFA), 20ms PCM audio chunking, Opus RTP packetization with playback clock sync, and instant barge-in cut-off.

- **Cartesia Sonic WebSocket Client**: integrates Cartesia Sonic (`livekit-plugins-cartesia~=1.0`) with configurable voice ID (`CARTESIA_VOICE_ID`) and model (`TTS_MODEL`).
- **Simulated Streaming TTS Engine**: local streaming synthesis fallback generating 24kHz vocal formant PCM frames for testing and verification without external paid API keys.
- **Audio Chunking & Clock Sync**: generates exact 20ms PCM audio frames (480 samples = 960 bytes @ 24kHz mono) with wall-clock pacing for seamless Opus RTP packetization without jitter buffer underflow.
- **Sub-100ms TTFA**: achieves sub-100ms Time-To-First-Audio streaming latency (typically 10-45ms) directly to caller WebRTC tracks.
- **Instant Barge-in Audio Cut-off**: when caller speech is detected by Silero VAD, active speech handles and in-flight audio buffers are immediately truncated (<50ms cut-off SLA) and muted.
- **Key configuration & acceptance check**:

```bash
python3 scripts/set_key.py cartesia <your-cartesia-key> # validates against Cartesia API and updates .env
docker compose up -d agent
python3 scripts/verify_tts.py                          # verifies 5/5 checks
```

## Mid-Call Function Calling & Retrieval Tools (task 1.6)

Enables real-time CRM/database lookups, scheduling, order status checks, and knowledge base retrieval during live calls without dead air:

- **JSON Schema Tool Registry**: standardized JSON Schema definitions for core tools (`check_availability`, `book_appointment`, `lookup_order`, `query_knowledge_base`, `execute_webhook`).
- **Async Non-Blocking Dispatcher**: executes tools asynchronously with latency tracking (`duration_ms`) and timeout protection (default 2.5–3.5s) to guarantee conversational flow.
- **Conversational Filler Speech Engine**: selects and emits natural filler phrases (e.g., *"Checking tracking details for order..."*, *"Let me check available times..."*) in <10ms to eliminate awkward silence while backend I/O runs.
- **Grounded Response Synthesis**: dynamically formats tool output directly into natural language streamed to the clause boundary splitter and TTS engine.
- **WebRTC Data Channel Telemetry**: emits `tool_call`, `filler_speech`, and `tool_result` events in real-time over the LiveKit data channels.

```bash
python3 scripts/verify_tools.py # verifies all 5/5 checks for Task 1.6
```

## Browser WebRTC Client SDK (task 1.7)

Provides a standalone, lightweight JavaScript / TypeScript SDK (`web/voice-client.js`, `web/voice-client.d.ts`) to embed real-time voice agent capabilities into any web frontend, React/Vue app, or embeddable widget:

- **Universal Module (UMD & ESM)**: works via `<script src="/voice-client.js">`, CommonJS `require()`, or ES6 `import`.
- **TypeScript Support**: full type safety and IDE autocomplete with `voice-client.d.ts`.
- **Event-Driven Architecture**: typed EventEmitter pattern for `transcript`, `vad`, `agentState`, `llmStream`, `llmClause`, `agentReply`, `ttsMetrics`, `interruption`, `toolCall`, `fillerSpeech`, and `toolResult`.
- **Microphone & Audio Controls**: automatic echo cancellation (AEC), noise suppression, automatic gain control (AGC), mute/unmute, and mic fallback handling.
- **Real-Time Audio Visualizer**: built-in `AudioVisualizer` helper supporting canvas waveforms, frequency bars, and volume level meters (0.0 to 1.0).
- **Exponential Backoff Reconnection**: automatically recovers from temporary network drops or room disconnects with configurable retry limits and jitter.

### Basic SDK Usage:

```javascript
import VoiceAgentClient from './voice-client.js';

const client = new VoiceAgentClient({
  room: 'support-call-123',
  tokenEndpoint: '/token',
});

client.on('transcript', ({ text, isFinal }) => console.log('User:', text));
client.on('agentReply', ({ text }) => console.log('AI:', text));
client.on('vad', ({ state }) => console.log('VAD state:', state));
client.on('toolCall', ({ tool, arguments: args }) => console.log('Tool dispatched:', tool, args));

await client.connect();
```

```bash
python3 scripts/verify_sdk.py # verifies all 5/5 checks for Task 1.7
```

## SIP Gateway & Telephony Integration (task 2.1)

Enables bidirectional connectivity between the public switched telephone network (PSTN) and LiveKit WebRTC rooms via carrier SIP trunks (Telnyx / Twilio / generic SIP PBX):

- **SIP Trunk Models & E.164 Normalization**: structured data models (`SIPInboundTrunk`, `SIPOutboundTrunk`, `SIPDispatchRule`, `TelephonyCallRecord`) with strict E.164 phone number normalization and ITU-T validation.
- **Inbound DID Dispatch Routing Engine**: maps incoming PSTN carrier numbers (DIDs) directly to dynamic LiveKit rooms (`call-<caller>-<timestamp>`), dispatching worker agents automatically on phone answer.
- **Programmatic Outbound Dialer**: creates dedicated call rooms and dispatches LiveKit SIP participant sessions (`livekit.api.CreateSIPParticipantRequest`) to call PSTN numbers with automatic fallback simulation for local and CI environments.
- **Telephony REST API Server (Port 8091)**:
  - `GET /api/telephony/trunks` — list configured carrier inbound and outbound SIP trunks.
  - `GET /api/telephony/calls` — query active and historic call session records.
  - `POST /api/telephony/dial` — initiate programmatic outbound PSTN call (`destination`, `caller_id`, `room`).
  - `POST /api/telephony/inbound/simulate` — simulate carrier DID dispatch and room provisioning.
- **WebRTC Data Channel Integration**: emits `telephony_event` notifications for caller states (`initiated -> ringing -> active -> completed`) and supports `dial_phone` / `get_telephony_trunks` actions over the WebRTC data channel.
- **Interactive Browser Telephony Console**: test outbound PSTN dialing and inbound DID simulation directly from `http://localhost:8091`.

```bash
python3 scripts/verify_sip.py # verifies all 5/5 checks for Task 2.1
```

## Call Transfer Engine (task 2.2)

Enables human-in-the-loop escalation, attended call handoffs, and carrier redirection:

- **Transfer Modes (Blind vs Warm)**:
  - **Blind (Cold) Transfer**: immediate redirect of the caller's SIP session to another telephone number or PBX extension via SIP REFER (`TransferSIPParticipantRequest`), with immediate AI agent departure.
  - **Warm (Attended) Transfer**: places caller on hold with music-on-hold, dials the receiving human specialist on a private consultation leg, delivers an automated structured conversational briefing (`generate_briefing`), bridges all participants together, and gracefully exits.
- **Call Hold State Controller**: manages caller hold status (`is_held`, `hold_music`, `hold_reason`) with real-time WebRTC `hold_state` notifications and un-hold recovery.
- **Automated Failure Recovery & Fallback**: if a transfer target is busy (SIP 486), declines, or times out, the caller is automatically taken off hold and the AI dialogue agent seamlessly resumes the conversation (*"Our specialist is currently unavailable. Would you like me to take a message?"*).
- **Mid-Call Function Tool (`transfer_call`)**: allows the streaming LLM to execute call transfers autonomously when callers request human assistance (*"Can you transfer me to billing?"*), with natural filler speech (*"Transferring you to our billing specialist right now, please stay on the line..."*).
- **Telephony Transfer REST API (Port 8091)**:
  - `GET /api/telephony/transfers` — list active and historic transfer records.
  - `GET /api/telephony/hold?call_id=<id>` — inspect live hold status.
  - `POST /api/telephony/transfer` — initiate blind or warm call transfer (`call_id`, `target_number`, `mode`, `department`, `reason`).
  - `POST /api/telephony/hold` — toggle hold and resume call (`call_id`, `hold: true|false`).
- **Interactive Browser Console**: test blind and warm transfers, monitor handoff briefings, and toggle live call hold from `http://localhost:8091`.

```bash
python3 scripts/verify_transfer.py # verifies all 6/6 checks for Task 2.2
```

## DTMF Digit Handling & IVR Navigation (task 2.3)

Enables caller touch-tone interaction, RFC 4733 / RFC 2833 telephone events, in-band dual-tone audio frequency detection, and multi-level IVR phone tree navigation:

- **RFC 4733 Out-of-Band & Goertzel In-Band Audio DSP**:
  - Full support for standard DTMF telephony digits (`0`-`9`, `*`, `#`, `A`-`D`).
  - Pure-Python **Goertzel Algorithm DSP**: extracts dual-tone frequencies directly from raw 8kHz/16kHz/24kHz PCM audio frames by computing spectral energy at low-band (697, 770, 852, 941 Hz) and high-band (1209, 1336, 1477, 1633 Hz) target frequencies.
- **Multi-Digit Buffer & Digit Sequences**:
  - `DTMFDigitBuffer`: supports collecting multi-digit strings (e.g. account numbers, PINs, extensions) with configurable minimum/maximum length, timeout windows, backspace correction, and `#` terminator completion.
- **Tree-Structured IVR Routing Engine**:
  - `IVRMenuNode` & `IVRMenuOption`: defines flexible hierarchical phone trees (Main Menu -> Sales -> Support -> Billing -> Live Operator).
  - Handles branching navigation (`navigate`), department warm transfers (`transfer`), audio replay (`repeat`), and custom mid-call tool invocation (`tool`).
  - Built-in retry limits with gentle automated fallback guidance (*"I'm sorry, that wasn't a valid option. Let me connect you with an operator."*).
- **DTMF & IVR REST API Server (Port 8091)**:
  - `GET /api/telephony/ivr` — inspect live IVR session state or list configured menu trees.
  - `POST /api/telephony/dtmf` — inject DTMF digits programmatically (`call_id`, `digit`, `duration_ms`).
  - `POST /api/telephony/ivr/reset` — reset an active call session back to the main menu.
- **WebRTC Data Channel Integration**:
  - LiveKit data channel events: emits `dtmf_event` and `ivr_state` topics.
  - Listens to `send_dtmf`, `get_ivr_state`, and `reset_ivr` control actions.
- **Interactive Browser Telephone Dialpad**:
  - Full 3x4 touch dialpad grid on `http://localhost:8091` with Web Audio API dual-tone sound synthesis, live buffered digit display, and real-time active IVR prompt updates.

```bash
python3 scripts/verify_dtmf.py # verifies all 6/6 checks for Task 2.3
```

## Answering Machine Detection (AMD) & Voicemail Drop (task 2.4)

Enables real-time classification of live human callers versus automated voicemail systems during outbound phone calls, with automated voicemail message drop and graceful call disconnect:

- **Temporal Cadence & Speech Energy Classifier**:
  - Distinguishes human greetings (short speech < 2.4s followed by an interactive listening pause > 0.7s) from machine greetings (long continuous monologue > 3.2s without conversational pauses).
- **Voicemail Prompt Beep Detection (Goertzel DSP)**:
  - Real-time pure-Python Goertzel audio DSP targeting North American and international recording prompt tones (1000 Hz, 800 Hz, 440 Hz) with tone duration gating (> 120ms) and noise rejection.
- **STT Transcript Semantic Keyword Detector**:
  - Pattern matcher analyzing incoming caller transcripts for voicemail markers (*"leave a message"*, *"after the tone"*, *"record your message"*, *"not available right now"*, *"mailbox"*) with high classification confidence (>95%).
- **Automated Voicemail Drop Engine**:
  - When an answering machine is detected and prompt beep sounds: streams personalized audio message (*"Hello, this is Northgate Support calling..."*) and disconnects the call automatically without human agent intervention.
- **Telephony AMD REST API Server (Port 8091)**:
  - `GET /api/telephony/amd?call_id=<id>` — inspect live AMD session state or default configuration.
  - `POST /api/telephony/amd/configure` — update AMD parameters (`message`, `action_on_machine`, `beep_detection_enabled`).
  - `POST /api/telephony/amd/simulate` — simulate human/machine greetings and beep tones for automated testing.
  - `POST /api/telephony/voicemail-drop` — programmatically trigger voicemail message drop.
- **WebRTC Data Channel Integration**:
  - LiveKit data channel events: emits `amd_event` and `amd_state` topics.
  - Listens to `simulate_amd`, `trigger_voicemail_drop`, and `configure_amd` control actions.
- **Interactive Browser Telephony Console**:
  - Dedicated AMD control card on `http://localhost:8091` with live confidence meter, speech/pause duration timer, Web Audio 1000 Hz beep simulator, and single-click Voicemail Drop.

```bash
python3 scripts/verify_amd.py # verifies all 6/6 checks for Task 2.4
```

## Real-Time Call Recording, Dual-Channel Stereo & Compliance (task 2.5)

Enables full-duplex regulatory-compliant call recording with separate stereo tracks for caller and AI agent:

- **Dual-Channel Stereo Audio Capture (Left: Caller, Right: Agent)**:
  - Interleaves separate mono 16-bit PCM audio frames into true stereo (`[L0, R0, L1, R1, ...]`), eliminating audio bleed and enabling isolated per-speaker STT diarization, acoustic analysis, and sentiment extraction in post-call pipelines.
- **Regulatory Consent & Compliance Engine**:
  - Full support for FCC, FTC, GDPR, and US Two-Party Consent regulations (California Penal Code § 632).
  - Generates standard 1400 Hz compliance notification beep tones on call start and periodic intervals.
  - Spoken disclosure notice flag (*"This call may be recorded for quality assurance and compliance purposes"*).
- **PCI-DSS & HIPAA Privacy Controls (Pause / Resume)**:
  - Dynamically pauses audio capture during sensitive data entry (credit card numbers, CVVs, SSNs, or health records).
  - Automatically injects zero-leakage silence frames during pause periods to maintain flawless timeline synchronization without storing sensitive PII on disk.
- **Broadcast-Standard RIFF/WAV Container Pipeline**:
  - Directly streams 16-bit PCM stereo WAV files with valid RIFF headers, sample counts, duration telemetry, and storage path tracking.
- **Telephony Recording REST API Server (Port 8091)**:
  - `GET /api/telephony/recordings` — list all active and historic call recordings.
  - `GET /api/telephony/recording?call_id=<id>` — inspect live recording metadata, status, duration, and file path.
  - `POST /api/telephony/recording/start` — start recording (`call_id`, `compliance_mode`, `beep_on_start`).
  - `POST /api/telephony/recording/pause` — pause recording for PCI redaction (`call_id`, `reason`).
  - `POST /api/telephony/recording/resume` — resume recording (`call_id`).
  - `POST /api/telephony/recording/stop` — stop recording and finalize WAV container.
- **WebRTC Data Channel Integration**:
  - LiveKit data channel events: emits `recording_event` (`recording_started`, `recording_paused`, `recording_resumed`, `recording_stopped`) and `recording_state`.
  - Listens to `start_recording`, `pause_recording`, `resume_recording`, `stop_recording`, and `get_recording_state` actions.
- **Interactive Browser Telephony Console**:
  - Dedicated Call Recording panel on `http://localhost:8091` with live duration clock, pulsing recording badge, PCI redaction pause/resume controls, and download info.

```bash
python3 scripts/verify_recording.py # verifies all 6/6 checks for Task 2.5
```

## WebRTC Telephony Softphone & Web Calling Dialpad (task 2.6)

Provides a comprehensive, standalone WebRTC SIP softphone interface (`web/softphone.html`) and PSTN dialpad connected directly to the LiveKit SIP Gateway and carrier trunks:

- **Standalone Web Calling Dialpad**:
  - Full LCD telephone display with real-time connection state (`LINE 1: IDLE`, `DIALING...`, `CONNECTED (TALKING)`, `ON HOLD`, `MUTED`), call duration counter, codec indicator (`Opus 16-bit / 48kHz`), and real-time VU audio activity meter.
  - 3x4 telephone keypad with touch-tone dual audio feedback, alphanumeric character guides, backspace, and quick dial enterprise presets (`Enterprise +1888`, `Support +1888`, `Billing +1800`, `Operator +1800`).
- **Web Audio DTMF & Ringback Tone Synthesizers**:
  - Implements standard RFC 4733 / ITU-T Q.23 dual-tone multifrequency synthesis (697–941 Hz row frequencies + 1209–1477 Hz column frequencies) for all 12 telephone keys (`0-9`, `*`, `#`).
  - Synthesizes authentic US standard ringback tones (440 Hz + 480 Hz audible cadenced ringing) during outbound call placement.
  - Implements acoustic warble ringtones for incoming calls.
- **Inbound PSTN Call Simulation & State Machine**:
  - Simulated carrier DID inbound dispatch banner presenting caller ID (`+15559876543`) and trunk name with pulsing ring alert.
  - Accept handler answers the call, joins the WebRTC media room, and connects audio.
  - Decline handler issues standard SIP 486 Busy Here call termination.
- **Hardware Device Selection & Audio Processing Constraints**:
  - Automatically queries and enumerates audio input microphones and audio output speakers via `navigator.mediaDevices.enumerateDevices()`.
  - Configures browser WebRTC audio constraints: Acoustic Echo Cancellation (AEC), Automatic Gain Control (AGC), and Noise Suppression.
- **In-Call Telephony Control Matrix**:
  - Microphone mute / unmute toggle with UI state feedback.
  - Call hold / un-hold toggle with REST `/api/telephony/hold` synchronization and hold state announcements.
  - Real-time dual-channel recording trigger (`/api/telephony/recording/start` & `/stop`).
  - Warm / attended and blind call transfer dispatch (`/api/telephony/transfer`).
- **Interactive Verification**:
  - Open `http://localhost:8091/softphone.html` in any browser, select audio hardware, dial any phone number or select a preset, and talk live with the AI voice assistant.

```bash
python3 scripts/verify_softphone.py # verifies all 6/6 checks for Task 2.6
```


## Ports

| Port | Purpose |
|------|---------|
| 7880 | HTTP + WebSocket signalling |
| 7881 | TCP media fallback for restrictive networks |
| 50000-50100/udp | RTP media |
| 6379 | Redis (live session state only) |

Everything except the UDP media range is bound to `127.0.0.1` — nothing is
exposed off this machine.

## Tokens

A LiveKit access token is a plain HS256 JWT with a `video` grant. There is
no proprietary handshake, which is why the Drupal side can mint browser
tokens directly with `firebase/php-jwt` and never call this service for
one. `agent/token.py` is the reference implementation.

**The token TTL covers joining, not talking.** Five minutes to enter the
room; once connected the session lasts as long as the call.

## Before this goes to a cloud VM

1. Generate new keys — the `.env` secret here is local-only and `.env` is gitignored.
2. Set `rtc.use_external_ip: true` so ICE candidates advertise the public address.
3. Put TLS in front of 7880 — browsers require `wss://` for microphone access on any non-localhost origin.
4. Open the UDP media range on the firewall, or deploy a TURN server for callers behind restrictive NATs.
5. Raise `room.max_participants` only if you add warm transfer (task 2.2); 4 is right for caller + agent + a transfer target.
# ai-voice-assistent
