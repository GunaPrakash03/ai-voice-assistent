"""
Twilio Bi-Directional Media Streams Handler (Direct Retell AI / Vapi style).

Connects Twilio phone calls directly to the AI Voice Agent pipeline over WebSockets:
Twilio PSTN <--> Twilio Media Stream WebSocket <--> STT (Deepgram) <--> LLM (Gemini/OpenAI) <--> TTS (Cartesia/ElevenLabs)

No LiveKit Cloud, no SIP trunks, no dispatch rules, no middleman.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import hashlib
import json
import logging
import os
import re
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

import urllib.request

log = logging.getLogger("twilio-stream")

# Twilio sends 8000Hz, 1 channel, mu-law audio in 20ms chunks (160 bytes per chunk).
TWILIO_SAMPLE_RATE = 8000
TWILIO_CHUNK_SIZE = 160  # 20ms @ 8kHz mu-law


class TwilioMediaStreamSession:
    """Manages an active bi-directional telephone call with Twilio."""

    def __init__(self, send_ws_json_cb) -> None:
        self.send_ws_json = send_ws_json_cb  # Callable[[dict], None]
        self.call_sid: str = ""
        self.stream_sid: str = ""
        self.called_number: str = ""
        self.caller_number: str = ""
        self.agent_cfg: Any = None
        self.history: List[Dict[str, str]] = []
        self.started_at: float = time.time()
        self.is_active: bool = True
        self.is_agent_speaking: bool = False
        self._current_speech_task: Optional[asyncio.Task] = None
        self._deepgram_ws = None
        self._deepgram_task: Optional[asyncio.Task] = None
        self._speech_accumulator: List[str] = []
        self._silence_counter: int = 0

    async def on_start(self, start_data: dict) -> None:
        """Called when Twilio emits the 'start' event with call metadata."""
        self.stream_sid = start_data.get("streamSid", "")
        self.call_sid = start_data.get("callSid", "")
        custom = start_data.get("customParameters", {})
        self.called_number = custom.get("called") or custom.get("To") or start_data.get("called", "")
        self.caller_number = custom.get("caller") or custom.get("From") or start_data.get("caller", "")

        log.info(
            "Twilio Stream started: call_sid=%s stream_sid=%s called=%s caller=%s",
            self.call_sid, self.stream_sid, self.called_number, self.caller_number,
        )

        # Resolve the agent assigned to this phone number
        self.agent_cfg = self._resolve_agent(self.called_number)
        agent_name = getattr(self.agent_cfg, "name", "AI Agent")
        log.info("Bound phone call %s to voice agent: %s", self.call_sid, agent_name)

        # Dispatch Retell AI compatible call_started webhook
        try:
            from agent.webhook_dispatcher import webhook_dispatcher
            webhook_dispatcher.dispatch_soon("call_started", {
                "call": {
                    "call_id": self.call_sid,
                    "agent_id": getattr(self.agent_cfg, "agent_id", "agent_default"),
                    "direction": "inbound",
                    "from_number": self.caller_number,
                    "to_number": self.called_number,
                    "started_at": self.started_at,
                }
            }, call_id=self.call_sid)
        except Exception as ex:
            log.warning("Could not dispatch call_started webhook: %s", ex)

        # Connect to Deepgram streaming STT over WebSocket
        await self._start_deepgram_stt()

        # Send initial greeting
        greeting = getattr(self.agent_cfg, "first_message", None) or f"Hello, thank you for calling. How can I help you today?"
        self.history.append({"speaker": "agent", "text": greeting})
        await self._speak_agent_text(greeting)

    def _resolve_agent(self, called_number: str) -> Any:
        """Looks up the assigned agent in the database/config."""
        try:
            from agent.telephony_manager import telephony_manager, normalize_phone_number
            from agent.agent_builder import agent_builder

            norm = normalize_phone_number(called_number)
            assigned_name = ""
            for num_record in telephony_manager.list_owned_numbers():
                if normalize_phone_number(num_record.get("phone_number", "")) == norm:
                    assigned_name = num_record.get("assigned_agent", "")
                    break

            if assigned_name:
                for agent in agent_builder._agents.values():
                    if agent.name.lower() == assigned_name.lower() or agent.agent_id == assigned_name:
                        return agent

            # Fallback to active agent or default
            return agent_builder.get_active_agent() or list(agent_builder._agents.values())[0]
        except Exception as ex:
            log.warning("Could not resolve specific agent for %s: %s", called_number, ex)
            from agent.agent_builder import agent_builder
            return list(agent_builder._agents.values())[0]

    async def _start_deepgram_stt(self) -> None:
        """Connects to Deepgram streaming STT WebSocket using native 8kHz mu-law."""
        dg_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
        if not dg_key:
            log.warning("DEEPGRAM_API_KEY not set; voice recognition will be offline")
            return

        try:
            import websockets
            url = (
                "wss://api.deepgram.com/v1/listen?"
                "encoding=mulaw&sample_rate=8000&channels=1&model=nova-3"
                "&endpointing=350&interim_results=true&smart_format=true"
            )
            headers = {"Authorization": f"Token {dg_key}"}
            self._deepgram_ws = await websockets.connect(url, additional_headers=headers)
            self._deepgram_task = asyncio.create_task(self._listen_deepgram_events())
            log.info("Deepgram Nova-3 STT connected for call %s", self.call_sid)
        except Exception as ex:
            log.error("Failed to connect to Deepgram STT: %s", ex)

    async def _listen_deepgram_events(self) -> None:
        """Receives transcripts from Deepgram and handles turn completion."""
        try:
            while self.is_active and self._deepgram_ws:
                msg = await self._deepgram_ws.recv()
                data = json.loads(msg)
                msg_type = data.get("type")

                if msg_type == "Results":
                    channel = data.get("channel", {})
                    alternatives = channel.get("alternatives", [{}])
                    transcript = alternatives[0].get("transcript", "").strip() if alternatives else ""

                    if not transcript:
                        continue

                    # If caller starts speaking while agent is speaking, trigger barge-in!
                    if self.is_agent_speaking and len(transcript) > 2:
                        log.info("Caller barge-in detected during playback! Interrupting agent.")
                        await self.interrupt_agent()

                    is_final = data.get("is_final", False)
                    speech_final = data.get("speech_final", False)

                    if is_final:
                        self._speech_accumulator.append(transcript)

                    if speech_final or data.get("from_finalize", False):
                        full_turn = " ".join(self._speech_accumulator).strip()
                        self._speech_accumulator.clear()
                        if full_turn:
                            log.info("Caller said: '%s'", full_turn)
                            await self.on_caller_turn(full_turn)

        except asyncio.CancelledError:
            pass
        except Exception as ex:
            if self.is_active:
                log.warning("Deepgram listener closed: %s", ex)

    async def on_media(self, payload_b64: str) -> None:
        """Incoming audio packet from Twilio (8kHz mu-law)."""
        if not self.is_active or not self._deepgram_ws:
            return

        try:
            raw_mulaw = base64.b64decode(payload_b64)
            # Pass directly to Deepgram (Deepgram accepts raw mu-law 8kHz bytes)
            await self._deepgram_ws.send(raw_mulaw)
        except Exception as ex:
            log.debug("Error forwarding media to STT: %s", ex)

    async def interrupt_agent(self) -> None:
        """Immediately halts any speech generation and clears Twilio's playback queue."""
        self.is_agent_speaking = False
        if self._current_speech_task and not self._current_speech_task.done():
            self._current_speech_task.cancel()
            self._current_speech_task = None

        # Send Twilio clear command to instantly purge audio buffer
        self.send_ws_json({
            "event": "clear",
            "streamSid": self.stream_sid,
        })
        log.info("Sent Twilio 'clear' event to drop buffered speech")

    async def on_caller_turn(self, user_text: str) -> None:
        """Called when the caller finishes speaking a sentence/turn."""
        self.history.append({"speaker": "caller", "text": user_text})

        # Cancel any previous speaking task
        if self._current_speech_task and not self._current_speech_task.done():
            self._current_speech_task.cancel()

        # Start generating reply
        self._current_speech_task = asyncio.create_task(self._process_and_reply(user_text))

    async def _process_and_reply(self, user_text: str) -> None:
        """Queries the LLM and streams TTS audio back to Twilio."""
        try:
            from agent.agent_builder import agent_builder

            loop = asyncio.get_running_loop()
            reply_text = await loop.run_in_executor(
                None,
                lambda: agent_builder._preview_reply(
                    self.agent_cfg,
                    user_text,
                    tool_name=None,
                    history=self.history,
                    turn_idx=len(self.history),
                ),
            )
            reply_text = (reply_text or "").strip()
            if not reply_text:
                return

            log.info("Agent replying: '%s'", reply_text)
            self.history.append({"speaker": "agent", "text": reply_text})
            await self._speak_agent_text(reply_text)

        except asyncio.CancelledError:
            log.info("Agent reply generation cancelled (interrupted).")
        except Exception as ex:
            log.error("Failed to generate agent reply: %s", ex)

    async def _speak_agent_text(self, text: str) -> None:
        """Synthesizes text into 8kHz mu-law audio and streams to Twilio."""
        self.is_agent_speaking = True
        try:
            # 1. Synthesize audio
            mulaw_bytes = await self._synthesize_to_mulaw(text)
            if not mulaw_bytes:
                log.warning("No audio synthesized for text: '%s'", text)
                self.is_agent_speaking = False
                return

            # 2. Stream audio to Twilio in 20ms chunks (160 bytes of 8kHz mu-law)
            chunk_size = TWILIO_CHUNK_SIZE
            total_chunks = (len(mulaw_bytes) + chunk_size - 1) // chunk_size

            for i in range(total_chunks):
                if not self.is_active or not self.is_agent_speaking:
                    log.info("Speech playback stopped mid-stream.")
                    break

                chunk = mulaw_bytes[i * chunk_size : (i + 1) * chunk_size]
                if len(chunk) < chunk_size:
                    # Pad with silence (0xFF in mu-law is quiet)
                    chunk += b"\xff" * (chunk_size - len(chunk))

                b64_payload = base64.b64encode(chunk).decode("utf-8")
                self.send_ws_json({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": b64_payload},
                })
                # 20ms pacing per chunk
                await asyncio.sleep(0.020)

        finally:
            self.is_agent_speaking = False

    async def _synthesize_to_mulaw(self, text: str) -> bytes:
        """Generates 8000Hz mu-law audio bytes directly using Deepgram Aura, Cartesia, or fallback."""
        voice_id = getattr(self.agent_cfg, "voice_id", "aura-luna-en") or "aura-luna-en"
        raw_voice_id = voice_id.replace("cartesia-", "").replace("eleven-", "").replace("deepgram-", "")

        # Option A: Deepgram Aura native 8kHz mu-law (super fast & natural)
        dg_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
        if dg_key and (raw_voice_id.startswith("aura-") or "aura" in raw_voice_id):
            try:
                url = f"https://api.deepgram.com/v1/speak?model={raw_voice_id}&encoding=mulaw&sample_rate=8000&container=none"
                payload = json.dumps({"text": text}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "Authorization": f"Token {dg_key}",
                        "Content-Type": "application/json",
                    },
                )
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5.0).read())
                if data:
                    return data
            except Exception as ex:
                log.warning("Deepgram Aura mu-law synthesis failed for %s: %s", raw_voice_id, ex)

        # Option B: Cartesia Sonic native 8kHz mu-law (sub-100ms)
        cartesia_key = os.getenv("CARTESIA_API_KEY", "").strip()
        if cartesia_key and not raw_voice_id.startswith("aura-"):
            try:
                url = "https://api.cartesia.ai/tts/bytes"
                payload = json.dumps({
                    "model_id": "sonic-3",
                    "transcript": text,
                    "voice": {"mode": "id", "id": raw_voice_id},
                    "output_format": {"container": "raw", "sample_rate": 8000, "encoding": "pcm_mulaw"},
                }).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "X-API-Key": cartesia_key,
                        "Cartesia-Version": "2024-06-10",
                        "Content-Type": "application/json",
                    },
                )
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5.0).read())
                if data:
                    return data
            except Exception as ex:
                log.warning("Cartesia native mu-law synthesis failed: %s", ex)

        # Option C: Fallback synthesis via voice_synthesizer
        try:
            from agent.voice_synthesizer import generate_speech_audio_bytes
            pcm_bytes = await generate_speech_audio_bytes(
                voice_id=voice_id,
                name=getattr(self.agent_cfg, "name", "Assistant"),
                text=text,
            )
            if pcm_bytes:
                return self._convert_pcm_to_mulaw(pcm_bytes)
        except Exception as ex:
            log.error("Fallback speech synthesis failed: %s", ex)

        return b""

    def _convert_pcm_to_mulaw(self, pcm_bytes: bytes) -> bytes:
        """Converts arbitrary audio bytes to 8000Hz 1-channel mu-law."""
        try:
            # Strip standard 44-byte WAV header if present
            if pcm_bytes.startswith(b"RIFF") and len(pcm_bytes) > 44:
                pcm_bytes = pcm_bytes[44:]
            mulaw = audioop.lin2ulaw(pcm_bytes, 2)
            return mulaw
        except Exception:
            return b""

    async def on_stop(self) -> None:
        """Called when Twilio emits the 'stop' event (call ended)."""
        self.is_active = False
        duration = time.time() - self.started_at
        log.info("Twilio Stream call completed: %s (duration: %.1fs)", self.call_sid, duration)

        # Dispatch Retell AI compatible call_ended webhook
        try:
            from agent.webhook_dispatcher import webhook_dispatcher
            webhook_dispatcher.dispatch_soon("call_ended", {
                "call": {
                    "call_id": self.call_sid,
                    "agent_id": getattr(self.agent_cfg, "agent_id", "agent_default"),
                    "direction": "inbound",
                    "from_number": self.caller_number,
                    "to_number": self.called_number,
                    "started_at": self.started_at,
                    "duration_seconds": round(duration, 2),
                },
                "transcript": self.history,
                "disconnection_reason": "user_hangup",
            }, call_id=self.call_sid)
        except Exception as ex:
            log.warning("Could not dispatch call_ended webhook: %s", ex)

        # Close Deepgram
        if self._deepgram_ws:
            try:
                await self._deepgram_ws.send(json.dumps({"type": "CloseStream"}))
                await self._deepgram_ws.close()
            except Exception:
                pass
            self._deepgram_ws = None

        if self._deepgram_task:
            self._deepgram_task.cancel()

        # Log call history record in telephony manager
        try:
            from agent.telephony_manager import telephony_manager, TelephonyCallRecord, CallDirection, CallStatus
            rec = TelephonyCallRecord(
                call_id=self.call_sid or f"tw-{int(time.time())}",
                direction=CallDirection.INBOUND,
                from_number=self.caller_number,
                to_number=self.called_number,
                room_name=f"tw-stream-{self.stream_sid[:8]}",
                participant_identity=f"caller-{self.caller_number}",
                status=CallStatus.COMPLETED,
                created_at=self.started_at,
                answered_at=self.started_at,
                ended_at=time.time(),
                duration_seconds=round(duration, 2),
                metadata={
                    "agent_name": getattr(self.agent_cfg, "name", "AI Agent"),
                    "stream_sid": self.stream_sid,
                    "turns": len(self.history),
                    "connection_type": "twilio_media_stream",
                },
            )
            telephony_manager._calls[rec.call_id] = rec
            log.info("Recorded telephony call history for %s", rec.call_id)
        except Exception as ex:
            log.warning("Could not persist call record: %s", ex)

        # File into pipeline worker for post-call intelligence, summaries, CRM extractions & Call History
        try:
            from agent.pipeline_worker import pipeline_worker
            turns = []
            for i, h in enumerate(self.history, start=1):
                role = "user" if h.get("speaker") == "caller" else "assistant"
                turns.append({
                    "turn_index": i,
                    "role": role,
                    "speaker": "Customer" if role == "user" else getattr(self.agent_cfg, "name", "AI Agent"),
                    "text": h.get("text", ""),
                    "timestamp": self.started_at,
                    "word_count": len(h.get("text", "").split()),
                })
            if turns:
                call_id = self.call_sid or f"tw-{int(time.time())}"
                job = pipeline_worker.enqueue_call(
                    call_id=call_id,
                    room_name=f"tw-stream-{self.stream_sid[:8] if self.stream_sid else 'call'}",
                    transcript_turns=turns,
                    metadata={
                        "source": "twilio_media_stream",
                        "agent_name": getattr(self.agent_cfg, "name", "AI Agent"),
                        "agent_id": getattr(self.agent_cfg, "agent_id", ""),
                        "direction": "inbound",
                        "from_number": self.caller_number,
                        "to_number": self.called_number,
                        "voice_id": getattr(self.agent_cfg, "voice_id", ""),
                        "started_at": self.started_at,
                        "ended_at": time.time(),
                        "duration_seconds": max(0.0, duration),
                        "stream_sid": self.stream_sid,
                    },
                    priority=2,
                )
                await pipeline_worker.execute_job(job.job_id)
                log.info("Twilio call %s processed by post-call pipeline (job %s)", call_id, job.job_id)
        except Exception as ex:
            log.warning("Could not execute post-call pipeline for Twilio call: %s", ex)


# ── RFC 6455 WebSocket Framing & Server Handler ──────────────────────────────

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def compute_ws_accept(sec_key: str) -> str:
    """Computes the Sec-WebSocket-Accept header value for the handshake."""
    combined = (sec_key.strip() + WS_GUID).encode("utf-8")
    return base64.b64encode(hashlib.sha1(combined).digest()).decode("utf-8")


def read_ws_frame(stream) -> Tuple[int, bytes]:
    """Reads one RFC 6455 WebSocket frame from a socket rfile."""
    b1_b2 = stream.read(2)
    if not b1_b2 or len(b1_b2) < 2:
        return 0x8, b""  # Close opcode
    b1, b2 = b1_b2[0], b1_b2[1]
    opcode = b1 & 0x0F
    is_masked = bool(b2 & 0x80)
    length = b2 & 0x7F

    if length == 126:
        length_bytes = stream.read(2)
        if len(length_bytes) < 2:
            return 0x8, b""
        length = struct.unpack("!H", length_bytes)[0]
    elif length == 127:
        length_bytes = stream.read(8)
        if len(length_bytes) < 8:
            return 0x8, b""
        length = struct.unpack("!Q", length_bytes)[0]

    mask = stream.read(4) if is_masked else None
    if is_masked and (not mask or len(mask) < 4):
        return 0x8, b""

    payload = stream.read(length) if length > 0 else b""
    if mask and payload:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    return opcode, payload


def build_ws_frame(text: str, opcode: int = 1) -> bytes:
    """Encodes a text message into an unmasked RFC 6455 frame."""
    data = text.encode("utf-8")
    length = len(data)
    header = bytearray([0x80 | (opcode & 0x0F)])

    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))

    return bytes(header) + data


def handle_twilio_media_stream(handler, request_path: str) -> None:
    """
    Upgrades an incoming HTTP connection to an RFC 6455 WebSocket and manages
    the Twilio Media Stream protocol directly.
    """
    sec_key = handler.headers.get("Sec-WebSocket-Key", "")
    if not sec_key:
        handler.send_error(400, "Missing Sec-WebSocket-Key")
        return

    # 1. Complete RFC 6455 Handshake
    accept_val = compute_ws_accept(sec_key)
    response_lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept_val}",
        "\r\n",
    ]
    handler.wfile.write("\r\n".join(response_lines).encode("latin1"))
    handler.wfile.flush()

    log.info("Twilio Media Stream WebSocket upgraded successfully.")

    # 2. Run async event loop for the session on this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def send_ws_json(obj: dict) -> None:
        try:
            frame = build_ws_frame(json.dumps(obj))
            handler.wfile.write(frame)
            handler.wfile.flush()
        except Exception as ex:
            log.debug("Error sending WS frame to Twilio: %s", ex)

    session = TwilioMediaStreamSession(send_ws_json)

    async def stream_loop():
        rfile = handler.rfile
        while session.is_active:
            try:
                # Read next frame in executor so as not to block asyncio
                opcode, payload = await loop.run_in_executor(None, read_ws_frame, rfile)
                if opcode == 0x8:  # Close frame
                    log.info("Twilio WebSocket sent close frame.")
                    break
                elif opcode == 0x9:  # Ping
                    handler.wfile.write(build_ws_frame("", opcode=0xA))
                    handler.wfile.flush()
                    continue
                elif opcode == 0x1:  # Text frame (JSON event)
                    msg_text = payload.decode("utf-8", "replace")
                    try:
                        event = json.loads(msg_text)
                    except Exception:
                        continue

                    evt_type = event.get("event")
                    if evt_type == "start":
                        await session.on_start(event.get("start", {}))
                    elif evt_type == "media":
                        media_data = event.get("media", {})
                        await session.on_media(media_data.get("payload", ""))
                    elif evt_type == "stop":
                        await session.on_stop()
                        break
            except Exception as ex:
                log.warning("Twilio stream loop error: %s", ex)
                break

        await session.on_stop()

    try:
        loop.run_until_complete(stream_loop())
    finally:
        loop.close()
