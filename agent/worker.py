"""
Task 1.3 — Voice Activity Detection (VAD) & Barge-In Interruption Engine.

Replaces the cloud STT-based turn detection with local Silero VAD running via
ONNX Runtime. Provides:
1. Speech boundary detection (speech start & speech end / turn endpointing).
2. Local VAD turn detection without external cloud gateway dependencies.
3. Instant barge-in / interruption handling: when the user starts speaking while
   the agent is playing audio, the playback buffer is immediately truncated,
   agent speech is cancelled, and an interruption event is published.
4. Real-time VAD state broadcasting over WebRTC data messages (topics:
   `transcript`, `vad`, `interruption`, `agent_state`).
"""

import asyncio
import json
import logging
import math
import os
import time
import wave
from typing import AsyncIterable

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession
from livekit.plugins import deepgram, silero

load_dotenv()
log = logging.getLogger("vad-worker")

# nova-3 is the plugin default. Override with STT_MODEL if comparing models.
STT_MODEL = os.getenv("STT_MODEL", "nova-3")


class Transcriber(Agent):
    """Listens and transcribes, ready for dialogue manager in Task 1.4."""

    def __init__(self) -> None:
        super().__init__(instructions="You transcribe and monitor speech boundaries.")


async def tone_speech_frames(secs: float = 6.0, rate: int = 16000) -> AsyncIterable[rtc.AudioFrame]:
    """Generates a pleasant chime sequence for barge-in testing."""
    chunk_ms = 20
    chunk_samples = rate * chunk_ms // 1000
    total_frames = int(secs * 1000 // chunk_ms)
    phase = 0.0
    chimes = [440, 554, 659, 880]
    for frame_idx in range(total_frames):
        freq = chimes[(frame_idx // 25) % len(chimes)]
        frame = rtc.AudioFrame.create(rate, 1, chunk_samples)
        for i in range(chunk_samples):
            frame.data[i] = int(2500 * math.sin(phase))
            phase += 2 * math.pi * freq / rate
        yield frame
        await asyncio.sleep(chunk_ms / 1000.0)


async def sample_speech_frames(path: str, repeat: int = 3) -> AsyncIterable[rtc.AudioFrame]:
    """Streams audio frames from a WAV file for barge-in testing."""
    for _ in range(repeat):
        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            ch = w.getnchannels()
            chunk_samples = rate * 20 // 1000
            bytes_per_chunk = chunk_samples * ch * w.getsampwidth()
            while True:
                raw = w.readframes(chunk_samples)
                if len(raw) < bytes_per_chunk:
                    break
                yield rtc.AudioFrame(raw, rate, ch, chunk_samples)
                await asyncio.sleep(0.02)
        silence = b"\x00" * (rate * 20 // 1000 * ch * 2)
        for _ in range(10):
            yield rtc.AudioFrame(silence, rate, ch, rate * 20 // 1000)
            await asyncio.sleep(0.02)


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()
    log.info("joined room %s", ctx.room.name)

    # Local Silero VAD model running on CPU via ONNX Runtime.
    # Completely self-hosted; eliminates reliance on cloud turn-detector endpoints.
    vad = silero.VAD.load(
        min_speech_duration=0.05,      # 50ms speech triggers start-of-speech
        min_silence_duration=0.45,     # 450ms silence marks end-of-speech
        prefix_padding_duration=0.2,   # 200ms audio buffer before speech start
    )

    session = AgentSession(
        vad=vad,
        stt=deepgram.STT(
            model=STT_MODEL,
            language="en",
            interim_results=True,
            punctuate=True,
        ),
        turn_handling={
            # Local Silero VAD drives turn boundaries and speech endpointing
            "turn_detection": "vad",
            "endpointing": {
                "mode": "dynamic",
                "min_delay": 0.4,
                "max_delay": 1.8,
            },
            # Barge-in interruption enabled: instant cutoff upon detected speech
            "interruption": {
                "enabled": True,
                "mode": "vad",
                "min_duration": 0.3,
                "discard_audio_if_uninterruptible": True,
            },
        },
    )

    pending: set[asyncio.Task] = set()

    def publish(payload: dict, topic: str, reliable: bool = False):
        if not ctx.room or not ctx.room.local_participant:
            return
        task = asyncio.create_task(
            ctx.room.local_participant.publish_data(
                json.dumps(payload).encode(),
                topic=topic,
                reliable=reliable,
            )
        )
        pending.add(task)
        task.add_done_callback(pending.discard)

    @session.on("user_input_transcribed")
    def on_transcript(ev):
        publish({
            "type": "transcript",
            "text": ev.transcript,
            "is_final": ev.is_final,
            "speaker": ev.speaker_id or "caller",
            "timestamp": time.time(),
        }, topic="transcript", reliable=ev.is_final)
        log.info("%s %s", "FINAL " if ev.is_final else "interim", ev.transcript)

    @session.on("user_state_changed")
    def on_user_state(ev):
        log.info("VAD speech boundary: %s -> %s", ev.old_state, ev.new_state)
        publish({
            "type": "vad",
            "state": ev.new_state,
            "old_state": ev.old_state,
            "speaker": "caller",
            "timestamp": time.time(),
        }, topic="vad", reliable=True)

    @session.on("agent_state_changed")
    def on_agent_state(ev):
        log.info("Agent state changed: %s -> %s", ev.old_state, ev.new_state)
        publish({
            "type": "agent_state",
            "state": ev.new_state,
            "old_state": ev.old_state,
            "timestamp": time.time(),
        }, topic="agent_state", reliable=True)

    @session.on("overlapping_speech")
    def on_overlapping(ev):
        log.info("Overlapping speech detected: is_interruption=%s", ev.is_interruption)
        if ev.is_interruption:
            publish({
                "type": "interruption",
                "interrupted": True,
                "reason": "user_barge_in",
                "timestamp": time.time(),
            }, topic="interruption", reliable=True)

    @session.on("speech_created")
    def on_speech_created(ev):
        handle = ev.speech_handle
        def on_speech_done(_):
            if handle.interrupted:
                log.info("Playback buffer truncated: speech handle interrupted by caller")
                publish({
                    "type": "interruption",
                    "interrupted": True,
                    "reason": "barge_in_buffer_truncated",
                    "timestamp": time.time(),
                }, topic="interruption", reliable=True)
        handle.add_done_callback(on_speech_done)

    @ctx.room.on("data_received")
    def on_room_data(packet):
        try:
            data = json.loads(packet.data.decode())
        except Exception:
            return
        if data.get("action") == "test_speech":
            participant_id = packet.participant.identity if packet.participant else "unknown"
            log.info("Test speech requested by %s for barge-in verification", participant_id)
            async def run_say():
                sample_file = "/app/samples/speech.wav"
                if os.path.exists(sample_file):
                    frames_iter = sample_speech_frames(sample_file, repeat=4)
                else:
                    frames_iter = tone_speech_frames(secs=6.0)
                try:
                    session.say(
                        "Test announcement. Speak now to test barge-in interruption.",
                        audio=frames_iter,
                        allow_interruptions=True,
                    )
                except Exception as e:
                    log.error("Failed to start speech playback: %s", e)

            task = asyncio.create_task(run_say())
            pending.add(task)
            task.add_done_callback(pending.discard)

    await session.start(agent=Transcriber(), room=ctx.room)
    log.info("listening with Silero VAD — model=%s, vad=silero", STT_MODEL)


if __name__ == "__main__":
    if not os.getenv("DEEPGRAM_API_KEY"):
        raise SystemExit(
            "DEEPGRAM_API_KEY is not set.\n"
            "  Get a key at https://console.deepgram.com (free tier includes credit),\n"
            "  add it to .env as DEEPGRAM_API_KEY=..., then: docker compose up -d agent"
        )
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))

