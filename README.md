# Voice Agent Service

The real-time media path for **AI Assistant Voice**. This is the one new
backend service in the plan — everything else (agent config, call records,
retrieval, RBAC) stays in the existing Drupal control plane.

See `../ai voice assistent/` for the estimation, stack and cost documents.

## Status

| Task | State |
|------|-------|
| 1.1 Media server setup | Local stack running, acceptance check passing. Cloud deploy pending. |
| 1.2 Streaming STT | Done — `verify_stt.py` 5/5, speech transcribed end to end. |
| 1.3 VAD & barge-in | Not started |
| 1.4 LLM dialogue manager | Not started |
| 1.5 Streaming TTS | Not started — needs `CARTESIA_API_KEY` |

## Run it

```bash
docker compose up -d
python3 scripts/verify.py     # no dependencies needed
```

The verify script proves the four things task 1.1 promises: the server
answers, tokens authenticate, forged tokens are rejected, and the room
management API works.

```bash
docker compose logs -f livekit   # watch signalling
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

**Two SDK defaults do not work on a self-hosted server.** The default turn
detector and the "adaptive" barge-in model are hosted services on
`agent-gateway.livekit.cloud`; with self-hosted keys both answer 401 and
retry in a loop. `agent/worker.py` pins `turn_detection="stt"` and turns
interruption off — task 1.3 replaces the first with local Silero VAD.

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
