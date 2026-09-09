"""
Task 1.6 — Mid-Call Function Calling & Retrieval Tools.

Integrates:
1. Speech-to-Text (STT) via Deepgram Nova-3.
2. Local Silero VAD for speech boundary detection and dynamic turn endpointing.
3. Streaming LLM Dialogue Manager with clause boundary splitter (sub-200ms TTFT)
   and multi-turn conversation history buffer.
4. Streaming TTS Engine (Cartesia Sonic, audio chunking, Opus RTP packetizer, clock sync).
5. Mid-call function calling & retrieval tools (JSON Schema parser, async dispatcher,
   filler speech engine to eliminate dead air, error fallback recovery).
6. Instant barge-in interruption: when the caller speaks while the agent is generating
   or speaking, generation is immediately aborted, playback buffers truncated,
   and interruption events broadcast.
7. Real-time WebRTC data broadcasting over topics:
   `transcript`, `vad`, `interruption`, `agent_state`, `llm_stream`, `llm_clause`,
   `agent_reply`, `chat_history`, `tts_metrics`, `tool_call`, `filler_speech`,
   `tool_result`, `tools_list`.
"""

import asyncio
import json
import logging
import math
import os
import time
import wave
from typing import AsyncIterable, Optional

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession
from livekit.plugins import deepgram, silero

from agent.llm_manager import (
    ClauseBoundarySplitter,
    ConversationContextBuffer,
    StreamingDialogueManager,
)
from agent.tts_manager import StreamingTTSManager
from agent.telephony_manager import telephony_manager, asdict
from agent.transfer_manager import transfer_manager
from agent.dtmf_manager import dtmf_manager
from agent.amd_manager import AMDManager, AMDState, AMDAction, VoicemailDropConfig
from agent.recording_manager import recording_manager, RecordingConfig, ComplianceMode, RecordingStatus
from agent.pipeline_worker import pipeline_worker

load_dotenv()
log = logging.getLogger("dialogue-worker")
amd_manager = AMDManager()

# Model configuration
STT_MODEL = os.getenv("STT_MODEL", "nova-3")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
TTS_MODEL = os.getenv("TTS_MODEL", "sonic-3")
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "f786b574-daa5-4673-aa0c-cbe3e8534c02")
TTS_SAMPLE_RATE = int(os.getenv("TTS_SAMPLE_RATE", "24000"))


class VoiceAssistantAgent(Agent):
    """Voice assistant agent with dialogue management capabilities."""

    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a friendly, helpful, and concise AI voice assistant. "
                "Speak in conversational English without bullet points or emojis."
            )
        )


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
    vad = silero.VAD.load(
        min_speech_duration=0.05,      # 50ms speech triggers start-of-speech
        min_silence_duration=0.45,     # 450ms silence marks end-of-speech
        prefix_padding_duration=0.2,   # 200ms audio buffer before speech start
    )

    tts_manager = StreamingTTSManager(
        model=TTS_MODEL,
        voice=CARTESIA_VOICE_ID,
        sample_rate=TTS_SAMPLE_RATE,
    )

    session = AgentSession(
        vad=vad,
        stt=deepgram.STT(
            model=STT_MODEL,
            language="en",
            interim_results=True,
            punctuate=True,
        ),
        tts=tts_manager.tts,
        turn_handling={
            "turn_detection": "vad",
            "endpointing": {
                "mode": "dynamic",
                "min_delay": 0.4,
                "max_delay": 1.8,
            },
            "interruption": {
                "enabled": True,
                "mode": "vad",
                "min_duration": 0.3,
                "discard_audio_if_uninterruptible": True,
            },
        },
    )

    llm_manager = StreamingDialogueManager(model=LLM_MODEL)
    current_turn_texts: list[str] = []
    user_is_speaking = False
    turn_process_task: Optional[asyncio.Task] = None
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

    @tts_manager.tts.on("metrics_collected")
    def on_tts_metrics(m):
        ttfa_ms = round(m.ttfb * 1000.0, 2) if getattr(m, "ttfb", None) and m.ttfb > 0 else None
        dur_ms = round(m.duration * 1000.0, 2) if getattr(m, "duration", None) else None
        audio_dur_ms = round(m.audio_duration * 1000.0, 2) if getattr(m, "audio_duration", None) else None
        log.info("TTS metrics: TTFA=%sms, dur=%sms, audio_dur=%sms", ttfa_ms, dur_ms, audio_dur_ms)
        publish({
            "type": "tts_metrics",
            "ttfa_ms": ttfa_ms,
            "duration_ms": dur_ms,
            "audio_duration_ms": audio_dur_ms,
            "characters": getattr(m, "characters_count", 0),
            "provider": getattr(tts_manager, "provider", "cartesia"),
            "model": getattr(tts_manager.tts, "model", getattr(tts_manager, "model", TTS_MODEL)),
            "interrupted": getattr(m, "cancelled", False),
            "timestamp": time.time(),
        }, topic="tts_metrics", reliable=True)

    async def execute_llm_turn(user_input: str):
        if not user_input.strip():
            return
        log.info("Starting streaming LLM turn for input: '%s'", user_input)
        publish({
            "type": "agent_state",
            "state": "thinking",
            "old_state": "listening",
            "timestamp": time.time(),
        }, topic="agent_state", reliable=True)

        async def on_token_cb(token: str):
            publish({
                "type": "llm_stream",
                "token": token,
                "timestamp": time.time(),
            }, topic="llm_stream", reliable=False)

        async def on_clause_cb(clause: str, is_final: bool, index: int):
            log.info("LLM Clause [%d%s]: %s", index, " (final)" if is_final else "", clause)
            publish({
                "type": "llm_clause",
                "clause": clause,
                "is_final": is_final,
                "index": index,
                "timestamp": time.time(),
            }, topic="llm_clause", reliable=True)

        async def on_tool_call_cb(tool_name: str, tool_args: dict):
            log.info("Mid-call tool invoked: %s(%s)", tool_name, tool_args)
            publish({
                "type": "tool_call",
                "tool": tool_name,
                "arguments": tool_args,
                "timestamp": time.time(),
            }, topic="tool_call", reliable=True)

        async def on_filler_cb(filler_phrase: str):
            log.info("Conversational filler speech: '%s'", filler_phrase)
            publish({
                "type": "filler_speech",
                "phrase": filler_phrase,
                "timestamp": time.time(),
            }, topic="filler_speech", reliable=True)

        async def on_tool_result_cb(tool_name: str, tool_res: dict):
            log.info("Tool '%s' result: status=%s (dur=%.2fms)", tool_name, tool_res.get("status"), tool_res.get("duration_ms", 0.0))
            publish({
                "type": "tool_result",
                "tool": tool_name,
                "status": tool_res.get("status"),
                "result": tool_res.get("result"),
                "duration_ms": tool_res.get("duration_ms"),
                "error": tool_res.get("error"),
                "timestamp": time.time(),
            }, topic="tool_result", reliable=True)

        try:
            publish({
                "type": "agent_state",
                "state": "speaking",
                "old_state": "thinking",
                "timestamp": time.time(),
            }, topic="agent_state", reliable=True)

            metrics = await llm_manager.generate_response(
                user_text=user_input,
                on_token=on_token_cb,
                on_clause=on_clause_cb,
                on_tool_call=on_tool_call_cb,
                on_filler=on_filler_cb,
                on_tool_result=on_tool_result_cb,
            )

            if not metrics["interrupted"]:
                log.info(
                    "LLM response complete in %.2fms (TTFT: %sms, tokens: %d, clauses: %d)",
                    metrics["duration_ms"],
                    metrics["ttft_ms"],
                    metrics["token_count"],
                    metrics["clause_count"],
                )
                publish({
                    "type": "agent_reply",
                    "text": metrics["text"],
                    "metrics": metrics,
                    "timestamp": time.time(),
                }, topic="agent_reply", reliable=True)
                publish({
                    "type": "chat_history",
                    "messages": llm_manager.context.to_list(),
                    "timestamp": time.time(),
                }, topic="chat_history", reliable=True)

                # Streaming TTS speech synthesis via session.say
                speech_handle = None
                try:
                    speech_handle = session.say(
                        metrics["text"],
                        allow_interruptions=True,
                    )
                    await speech_handle.wait_for_playout()
                except asyncio.CancelledError:
                    log.info("TTS playback cancelled in turn execution")
                except Exception as play_err:
                    log.error("TTS playback error in turn execution: %s", play_err)
                finally:
                    if not user_is_speaking and (speech_handle is None or not speech_handle.interrupted):
                        publish({
                            "type": "agent_state",
                            "state": "listening",
                            "old_state": "speaking",
                            "timestamp": time.time(),
                        }, topic="agent_state", reliable=True)
            else:
                log.info("LLM response was interrupted by user barge-in.")
        except asyncio.CancelledError:
            log.info("LLM turn task cancelled.")
        except Exception as err:
            log.error("Error during LLM generation: %s", err, exc_info=True)
            publish({
                "type": "agent_state",
                "state": "listening",
                "old_state": "error",
                "timestamp": time.time(),
            }, topic="agent_state", reliable=True)

    def schedule_turn():
        nonlocal turn_process_task
        if turn_process_task and not turn_process_task.done():
            return
        if not current_turn_texts:
            return
        combined = " ".join(current_turn_texts)
        current_turn_texts.clear()
        turn_process_task = asyncio.create_task(execute_llm_turn(combined))
        pending.add(turn_process_task)
        turn_process_task.add_done_callback(pending.discard)

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

        if ev.transcript.strip():
            amd_res = amd_manager.process_transcript(ctx.room.name, ev.transcript)
            if amd_res.state in (AMDState.MACHINE_GREETING, AMDState.VOICEMAIL_BEEP, AMDState.HUMAN):
                publish({
                    "type": "amd_event",
                    "call_id": ctx.room.name,
                    "state": amd_res.state.value,
                    "confidence": amd_res.confidence,
                    "reason": amd_res.reason,
                    "action": amd_res.action.value,
                    "timestamp": time.time(),
                }, topic="amd_event", reliable=True)

        if ev.is_final and ev.transcript.strip():
            current_turn_texts.append(ev.transcript.strip())
            if not user_is_speaking:
                schedule_turn()

    @session.on("user_state_changed")
    def on_user_state(ev):
        nonlocal user_is_speaking
        log.info("VAD speech boundary: %s -> %s", ev.old_state, ev.new_state)
        publish({
            "type": "vad",
            "state": ev.new_state,
            "old_state": ev.old_state,
            "speaker": "caller",
            "timestamp": time.time(),
        }, topic="vad", reliable=True)

        amd_res = amd_manager.process_vad_event(ctx.room.name, is_speech=(ev.new_state == "speaking"), timestamp=time.time())
        if amd_res.state != AMDState.DETECTING:
            publish({
                "type": "amd_event",
                "call_id": ctx.room.name,
                "state": amd_res.state.value,
                "confidence": amd_res.confidence,
                "reason": amd_res.reason,
                "action": amd_res.action.value,
                "greeting_duration_s": amd_res.greeting_duration_s,
                "silence_duration_s": amd_res.silence_duration_s,
                "timestamp": time.time(),
            }, topic="amd_event", reliable=True)

        if ev.new_state == "speaking":
            user_is_speaking = True
            # Caller starts speaking: instant barge-in interruption of any active LLM generation & TTS playback
            interrupted_any = False
            if llm_manager.cancel_active_generation():
                interrupted_any = True
                log.info("Caller barge-in detected via VAD: cancelled in-flight LLM generation.")
            if tts_manager.interrupt_playback():
                interrupted_any = True
                log.info("Caller barge-in detected via VAD: cancelled in-flight TTS playback stream.")
            try:
                if session.current_speech and not session.current_speech.done():
                    session.interrupt(force=True)
                    interrupted_any = True
                    log.info("Caller barge-in detected via VAD: interrupted active speech handle.")
            except Exception as e:
                log.warning("Could not interrupt session speech: %s", e)

            if interrupted_any:
                publish({
                    "type": "interruption",
                    "interrupted": True,
                    "reason": "user_barge_in",
                    "timestamp": time.time(),
                }, topic="interruption", reliable=True)
                publish({
                    "type": "agent_state",
                    "state": "listening",
                    "old_state": "speaking",
                    "timestamp": time.time(),
                }, topic="agent_state", reliable=True)
        elif ev.new_state == "listening":
            user_is_speaking = False
            # Caller finished speaking: small debounce to collect final transcript packet
            async def delayed_schedule():
                await asyncio.sleep(0.12)
                schedule_turn()
            t = asyncio.create_task(delayed_schedule())
            pending.add(t)
            t.add_done_callback(pending.discard)

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
            llm_manager.cancel_active_generation()
            tts_manager.interrupt_playback()
            try:
                session.interrupt(force=True)
            except Exception:
                pass
            publish({
                "type": "interruption",
                "interrupted": True,
                "reason": "overlapping_speech",
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
        action = data.get("action")
        if action == "test_prompt":
            prompt = data.get("text", "Hello, how can you help me today?")
            participant_id = packet.participant.identity if packet.participant else "caller"
            log.info("Test prompt received from %s: '%s'", participant_id, prompt)
            task = asyncio.create_task(execute_llm_turn(prompt))
            pending.add(task)
            task.add_done_callback(pending.discard)
        elif action == "get_history":
            publish({
                "type": "chat_history",
                "messages": llm_manager.context.to_list(),
                "timestamp": time.time(),
            }, topic="chat_history", reliable=True)
        elif action == "clear_history":
            llm_manager.context.clear()
            publish({
                "type": "chat_history",
                "messages": [],
                "timestamp": time.time(),
            }, topic="chat_history", reliable=True)
        elif action in ("test_tts", "speak"):
            text = data.get("text", "Hello! This is Cartesia Sonic streaming audio synthesis with sub-100 millisecond latency.")
            participant_id = packet.participant.identity if packet.participant else "caller"
            log.info("Test TTS requested by %s: '%s'", participant_id, text)
            async def run_tts_say():
                publish({
                    "type": "agent_state",
                    "state": "speaking",
                    "old_state": "listening",
                    "timestamp": time.time(),
                }, topic="agent_state", reliable=True)
                speech_handle = None
                try:
                    speech_handle = session.say(text, allow_interruptions=True)
                    await speech_handle.wait_for_playout()
                except asyncio.CancelledError:
                    log.info("Test TTS playback task cancelled")
                except Exception as err:
                    log.error("Test TTS playback error: %s", err)
                finally:
                    if not user_is_speaking and (speech_handle is None or not speech_handle.interrupted):
                        publish({
                            "type": "agent_state",
                            "state": "listening",
                            "old_state": "speaking",
                            "timestamp": time.time(),
                        }, topic="agent_state", reliable=True)

            task = asyncio.create_task(run_tts_say())
            pending.add(task)
            task.add_done_callback(pending.discard)
        elif action == "measure_ttfa":
            text = data.get("text", "Cartesia Sonic streaming text-to-speech test phrase.")
            async def run_measure():
                res = await tts_manager.measure_ttfa(text)
                log.info("Measured TTFA: %sms (total: %sms, frames: %d)", res["ttfa_ms"], res["duration_ms"], res["frames_count"])
                publish({
                    "type": "tts_metrics",
                    "ttfa_ms": res["ttfa_ms"],
                    "duration_ms": res["duration_ms"],
                    "frames_sent": res["frames_count"],
                    "provider": res["provider"],
                    "model": res["model"],
                    "timestamp": time.time(),
                }, topic="tts_metrics", reliable=True)

            task = asyncio.create_task(run_measure())
            pending.add(task)
            task.add_done_callback(pending.discard)
        elif action == "call_tool":
            tool_name = data.get("tool", "check_availability")
            tool_args = data.get("arguments", {"service_type": "consultation", "date": "tomorrow"})
            log.info("Direct tool invocation requested: %s(%s)", tool_name, tool_args)
            async def run_direct_tool():
                filler = llm_manager.tool_registry.filler_engine.get_filler(tool_name, tool_args)
                publish({
                    "type": "filler_speech",
                    "phrase": filler,
                    "tool": tool_name,
                    "timestamp": time.time(),
                }, topic="filler_speech", reliable=True)
                publish({
                    "type": "tool_call",
                    "tool": tool_name,
                    "arguments": tool_args,
                    "timestamp": time.time(),
                }, topic="tool_call", reliable=True)
                res = await llm_manager.tool_dispatcher.execute_tool(tool_name, tool_args)
                publish({
                    "type": "tool_result",
                    "tool": tool_name,
                    "status": res["status"],
                    "result": res["result"],
                    "duration_ms": res["duration_ms"],
                    "error": res["error"],
                    "timestamp": time.time(),
                }, topic="tool_result", reliable=True)

            task = asyncio.create_task(run_direct_tool())
            pending.add(task)
            task.add_done_callback(pending.discard)
        elif action == "get_tools":
            schemas = llm_manager.tool_registry.get_schemas()
            publish({
                "type": "tools_list",
                "tools": schemas,
                "timestamp": time.time(),
            }, topic="tools_list", reliable=True)
        elif action == "dial_phone":
            dest = data.get("destination", "").strip()
            caller_id = data.get("caller_id", "").strip() or None
            log.info("Outbound dial requested over data channel for: %s", dest)
            async def run_dial():
                try:
                    record = await telephony_manager.dial_phone_number(
                        destination_number=dest,
                        caller_id=caller_id,
                        room_name=ctx.room.name,
                    )
                    publish({
                        "type": "telephony_event",
                        "event": "outbound_call_initiated",
                        "call": asdict(record),
                        "timestamp": time.time(),
                    }, topic="telephony_event", reliable=True)
                except Exception as dial_err:
                    publish({
                        "type": "telephony_event",
                        "event": "outbound_call_failed",
                        "error": str(dial_err),
                        "timestamp": time.time(),
                    }, topic="telephony_event", reliable=True)
            task = asyncio.create_task(run_dial())
            pending.add(task)
            task.add_done_callback(pending.discard)
        elif action == "get_telephony_trunks":
            publish({
                "type": "telephony_trunks",
                "inbound": telephony_manager.list_inbound_trunks(),
                "outbound": telephony_manager.list_outbound_trunks(),
                "rules": telephony_manager.list_dispatch_rules(),
                "timestamp": time.time(),
            }, topic="telephony_trunks", reliable=True)
        elif action == "transfer_call":
            target = data.get("target_number", "").strip() or data.get("destination", "").strip()
            mode_str = str(data.get("mode", data.get("transfer_type", "blind"))).strip().lower()
            dept = data.get("department")
            reason = data.get("reason", "Caller request")
            participant_id = packet.participant.identity if packet.participant else "caller"
            log.info("Call transfer requested over data channel: %s -> %s (mode=%s, dept=%s)", participant_id, target, mode_str, dept)

            async def run_transfer():
                try:
                    publish({
                        "type": "transfer_event",
                        "event": "transfer_initiated",
                        "call_id": ctx.room.name,
                        "target_number": target,
                        "mode": mode_str,
                        "department": dept,
                        "timestamp": time.time(),
                    }, topic="transfer_event", reliable=True)

                    if mode_str == "warm":
                        rec = await transfer_manager.initiate_warm_transfer(
                            call_id=ctx.room.name,
                            target_number=target,
                            source_participant=participant_id,
                            department=dept,
                            reason=reason,
                        )
                    else:
                        rec = await transfer_manager.initiate_blind_transfer(
                            call_id=ctx.room.name,
                            target_number=target,
                            source_participant=participant_id,
                            department=dept,
                            reason=reason,
                        )

                    publish({
                        "type": "transfer_event",
                        "event": f"transfer_{rec.status.value}",
                        "transfer": asdict(rec),
                        "timestamp": time.time(),
                    }, topic="transfer_event", reliable=True)
                except Exception as xfer_err:
                    log.error("Transfer failed: %s", xfer_err)
                    publish({
                        "type": "transfer_event",
                        "event": "transfer_failed",
                        "error": str(xfer_err),
                        "timestamp": time.time(),
                    }, topic="transfer_event", reliable=True)

            task = asyncio.create_task(run_transfer())
            pending.add(task)
            task.add_done_callback(pending.discard)
        elif action == "hold_call":
            hold = bool(data.get("hold", True))
            reason = data.get("reason", "manual_hold")
            if hold:
                st = transfer_manager.put_on_hold(ctx.room.name, reason=reason)
            else:
                st = transfer_manager.remove_from_hold(ctx.room.name)
            publish({
                "type": "hold_state",
                "call_id": ctx.room.name,
                "is_held": st.is_held,
                "reason": st.hold_reason,
                "timestamp": time.time(),
            }, topic="hold_state", reliable=True)
        elif action == "get_transfers":
            publish({
                "type": "transfers_list",
                "transfers": transfer_manager.list_transfers(),
                "timestamp": time.time(),
            }, topic="transfers_list", reliable=True)
        elif action == "send_dtmf":
            digit = str(data.get("digit", "")).strip().upper()
            dur = int(data.get("duration_ms", 160))
            participant_id = packet.participant.identity if packet.participant else "caller"
            log.info("DTMF digit received from %s: '%s' (dur=%dms)", participant_id, digit, dur)

            ivr_res = dtmf_manager.process_dtmf_digit(
                call_id=ctx.room.name,
                digit=digit,
                duration_ms=dur,
                source_participant=participant_id,
            )

            publish({
                "type": "dtmf_event",
                "digit": digit,
                "status": ivr_res.get("status"),
                "action": ivr_res.get("action"),
                "buffered_digits": ivr_res.get("buffered_digits"),
                "prompt": ivr_res.get("prompt"),
                "timestamp": time.time(),
            }, topic="dtmf_event", reliable=True)

            publish({
                "type": "ivr_state",
                "state": dtmf_manager.get_call_state(ctx.room.name),
                "timestamp": time.time(),
            }, topic="ivr_state", reliable=True)

            # If IVR action triggers a transfer automatically
            if ivr_res.get("action") == "transfer":
                target_num = ivr_res.get("transfer_number", "+18885550142")
                dept = ivr_res.get("department", "support")
                async def run_ivr_xfer():
                    rec = await transfer_manager.initiate_warm_transfer(
                        call_id=ctx.room.name,
                        target_number=target_num,
                        source_participant=participant_id,
                        department=dept,
                        reason=f"IVR Menu Keypad '{digit}' selection",
                    )
                    publish({
                        "type": "transfer_event",
                        "event": f"transfer_{rec.status.value}",
                        "transfer": asdict(rec),
                        "timestamp": time.time(),
                    }, topic="transfer_event", reliable=True)
                task = asyncio.create_task(run_ivr_xfer())
                pending.add(task)
                task.add_done_callback(pending.discard)

            elif ivr_res.get("prompt"):
                # Announce prompt to user
                prompt_text = ivr_res["prompt"]
                async def run_ivr_announce():
                    try:
                        session.say(prompt_text, allow_interruptions=True)
                    except Exception as e:
                        log.debug("IVR prompt announce exception: %s", e)
                task = asyncio.create_task(run_ivr_announce())
                pending.add(task)
                task.add_done_callback(pending.discard)

        elif action == "get_ivr_state":
            publish({
                "type": "ivr_state",
                "state": dtmf_manager.get_call_state(ctx.room.name),
                "timestamp": time.time(),
            }, topic="ivr_state", reliable=True)

        elif action == "reset_ivr":
            dtmf_manager.reset_call(ctx.room.name)
            publish({
                "type": "ivr_state",
                "state": dtmf_manager.get_call_state(ctx.room.name),
                "timestamp": time.time(),
            }, topic="ivr_state", reliable=True)

        elif action == "get_amd_state":
            s = amd_manager.get_session(ctx.room.name)
            publish({
                "type": "amd_state",
                "call_id": ctx.room.name,
                "state": s.to_result().dict() if s else None,
                "timestamp": time.time(),
            }, topic="amd_state", reliable=True)

        elif action == "configure_amd":
            cfg_kwargs = {}
            if "enabled" in data:
                cfg_kwargs["enabled"] = bool(data["enabled"])
            if "message" in data:
                cfg_kwargs["message"] = str(data["message"])
            if "action_on_machine" in data:
                cfg_kwargs["action_on_machine"] = AMDAction(data["action_on_machine"])
            if "beep_detection_enabled" in data:
                cfg_kwargs["beep_detection_enabled"] = bool(data["beep_detection_enabled"])
            cfg = VoicemailDropConfig(**cfg_kwargs)
            s = amd_manager.get_or_create_session(ctx.room.name, cfg)
            s.config = cfg
            publish({
                "type": "amd_state",
                "call_id": ctx.room.name,
                "state": s.to_result().dict(),
                "timestamp": time.time(),
            }, topic="amd_state", reliable=True)

        elif action == "trigger_voicemail_drop":
            custom_msg = data.get("message")
            drop_res = amd_manager.trigger_voicemail_drop(ctx.room.name, custom_message=custom_msg)
            publish({
                "type": "amd_event",
                "call_id": ctx.room.name,
                "state": drop_res.get("state"),
                "action": "drop_voicemail",
                "message": drop_res.get("message"),
                "audio_duration_s": drop_res.get("audio_duration_s"),
                "auto_hangup": drop_res.get("auto_hangup"),
                "timestamp": time.time(),
            }, topic="amd_event", reliable=True)

        elif action == "simulate_amd":
            ev_type = data.get("event_type", "machine_greeting")
            s = amd_manager.get_or_create_session(ctx.room.name)
            if ev_type == "human_greeting":
                s.state = AMDState.HUMAN
                s.confidence = 0.92
                s.reason = "Simulated short human greeting ('Hello?')"
                s.total_speech_duration = 1.1
            elif ev_type == "machine_greeting":
                s.state = AMDState.MACHINE_GREETING
                s.confidence = 0.95
                s.reason = "Simulated voicemail greeting ('Please leave a message after the tone...')"
                s.total_speech_duration = 4.8
            elif ev_type == "voicemail_beep":
                s.state = AMDState.VOICEMAIL_BEEP
                s.beep_detected = True
                s.confidence = 0.99
                s.reason = "Simulated 1000 Hz recording beep detected"
            publish({
                "type": "amd_event",
                "call_id": ctx.room.name,
                "state": s.state.value,
                "confidence": s.confidence,
                "reason": s.reason,
                "action": s.to_result().action.value,
                "timestamp": time.time(),
            }, topic="amd_event", reliable=True)

        elif action == "start_recording":
            cm_str = data.get("compliance_mode", "two_party")
            try:
                cm = ComplianceMode(cm_str)
            except Exception:
                cm = ComplianceMode.TWO_PARTY
            cfg = RecordingConfig(
                compliance_mode=cm,
                beep_on_start=bool(data.get("beep_on_start", True)),
                redact_on_pause=bool(data.get("redact_on_pause", True)),
            )
            meta = recording_manager.start_recording(ctx.room.name, config=cfg)
            publish({
                "type": "recording_event",
                "event": "recording_started",
                "call_id": ctx.room.name,
                "recording": meta.dict(),
                "timestamp": time.time(),
            }, topic="recording_event", reliable=True)

        elif action == "pause_recording":
            reason = data.get("reason", "pci_compliance")
            meta = recording_manager.pause_recording(ctx.room.name, reason=reason)
            publish({
                "type": "recording_event",
                "event": "recording_paused",
                "call_id": ctx.room.name,
                "recording": meta.dict(),
                "timestamp": time.time(),
            }, topic="recording_event", reliable=True)

        elif action == "resume_recording":
            meta = recording_manager.resume_recording(ctx.room.name)
            publish({
                "type": "recording_event",
                "event": "recording_resumed",
                "call_id": ctx.room.name,
                "recording": meta.dict(),
                "timestamp": time.time(),
            }, topic="recording_event", reliable=True)

        elif action == "stop_recording":
            meta = recording_manager.stop_recording(ctx.room.name)
            publish({
                "type": "recording_event",
                "event": "recording_stopped",
                "call_id": ctx.room.name,
                "recording": meta.dict(),
                "timestamp": time.time(),
            }, topic="recording_event", reliable=True)

        elif action == "get_recording_state":
            s = recording_manager.get_session(ctx.room.name)
            publish({
                "type": "recording_state",
                "call_id": ctx.room.name,
                "recording": s.to_metadata().dict() if s else None,
                "timestamp": time.time(),
            }, topic="recording_state", reliable=True)

        elif action == "enqueue_post_call":
            call_id = data.get("call_id", ctx.room.name)
            turns = data.get("transcript_turns")
            if not turns:
                turns = []
                for msg in llm_manager.context.to_list():
                    turns.append({"role": msg.get("role", "caller"), "text": msg.get("content", "")})
            audio_path = data.get("audio_path")
            if not audio_path:
                rec_session = recording_manager.get_session(ctx.room.name)
                if rec_session:
                    audio_path = rec_session.file_path
            priority = int(data.get("priority", 5))

            job = pipeline_worker.enqueue_call(
                call_id=call_id,
                room_name=ctx.room.name,
                transcript_turns=turns,
                audio_path=audio_path,
                metadata={"worker_id": ctx.room.name},
                priority=priority,
            )

            async def run_pipeline():
                try:
                    await pipeline_worker.execute_job(job.job_id)
                    publish({
                        "type": "pipeline_event",
                        "event": "job_completed",
                        "call_id": call_id,
                        "job": job.to_dict(),
                        "timestamp": time.time(),
                    }, topic="pipeline_event", reliable=True)
                except Exception as p_err:
                    log.error("Post-call pipeline failed: %s", p_err)
                    publish({
                        "type": "pipeline_event",
                        "event": "job_failed",
                        "call_id": call_id,
                        "error": str(p_err),
                        "timestamp": time.time(),
                    }, topic="pipeline_event", reliable=True)

            task = asyncio.create_task(run_pipeline())
            pending.add(task)
            task.add_done_callback(pending.discard)

            publish({
                "type": "pipeline_event",
                "event": "job_enqueued",
                "call_id": call_id,
                "job": job.to_dict(),
                "timestamp": time.time(),
            }, topic="pipeline_event", reliable=True)

        elif action == "get_pipeline_jobs":
            publish({
                "type": "pipeline_jobs",
                "jobs": pipeline_worker.list_jobs(limit=20),
                "timestamp": time.time(),
            }, topic="pipeline_jobs", reliable=True)

        elif action == "get_pipeline_stats":
            publish({
                "type": "pipeline_stats",
                "stats": pipeline_worker.get_stats(),
                "timestamp": time.time(),
            }, topic="pipeline_stats", reliable=True)

        elif action == "test_speech":
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

    await session.start(agent=VoiceAssistantAgent(), room=ctx.room)
    log.info("dialogue worker active with Silero VAD, Streaming LLM (%s) & Cartesia/TTS (%s)", LLM_MODEL, TTS_MODEL)


if __name__ == "__main__":
    if not os.getenv("DEEPGRAM_API_KEY"):
        raise SystemExit(
            "DEEPGRAM_API_KEY is not set.\n"
            "  Get a key at https://console.deepgram.com (free tier includes credit),\n"
            "  add it to .env as DEEPGRAM_API_KEY=..., then: docker compose up -d agent"
        )
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))

