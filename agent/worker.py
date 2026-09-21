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

from __future__ import annotations
import asyncio
import json
import logging
import math
import os
import re
import time
import wave
from typing import AsyncIterable, Optional

from dotenv import load_dotenv
try:
    from livekit import agents, rtc
    from livekit.agents import Agent, AgentSession
    from livekit.plugins import deepgram, silero
except ImportError:
    agents, rtc, Agent, AgentSession, deepgram, silero = None, None, None, None, None, None

from agent.llm_manager import (
    ClauseBoundarySplitter,
    ConversationContextBuffer,
    StreamingDialogueManager,
)
from agent.tts_manager import StreamingTTSManager
from agent.gemini_stt import AUDIO_MARK, concat_wavs
from agent import storage as _storage
_storage.bootstrap("agent worker")
from agent.telephony_manager import telephony_manager, asdict
from agent.transfer_manager import transfer_manager
from agent.dtmf_manager import dtmf_manager
from agent.amd_manager import AMDManager, AMDState, AMDAction, VoicemailDropConfig
from agent.recording_manager import recording_manager, RecordingConfig, ComplianceMode, RecordingStatus
from agent.pipeline_worker import pipeline_worker
from agent.webhook_dispatcher import webhook_dispatcher
webhook_dispatcher.attach_to_pipeline(pipeline_worker)

load_dotenv()
log = logging.getLogger("dialogue-worker")
amd_manager = AMDManager()

# Model configuration
# STT_PROVIDER=deepgram (default, Retell-style streaming pipeline: partial transcripts while the caller talks,
# ~0.3 s to a final). STT_PROVIDER=gemini: the Gemini model hears the audio itself (no STT vendor, ~1.5 s slower).
STT_PROVIDER = os.getenv("STT_PROVIDER", "deepgram").strip().lower()
STT_MODEL = os.getenv("STT_MODEL", "nova-3")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
TTS_MODEL = os.getenv("TTS_MODEL", "sonic-3")
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "f786b574-daa5-4673-aa0c-cbe3e8534c02")
TTS_SAMPLE_RATE = int(os.getenv("TTS_SAMPLE_RATE", "24000"))


_AgentBase = Agent if Agent else object


class VoiceAssistantAgent(_AgentBase):
    """Voice assistant agent with dialogue management capabilities."""

    def __init__(self) -> None:
        if Agent:
            super().__init__(
                instructions=(
                "You are a friendly, helpful, and concise AI voice assistant. "
                "Speak in conversational English without bullet points or emojis."
            )
        )
        self.turn_hook = None   # set by the entrypoint: async (text) -> None

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """LiveKit decided the caller's turn is over (VAD silence + endpointing delay, with every
        final transcript piece of the turn merged). This is the only place a dialogue turn starts."""
        if self.turn_hook is not None:
            await self.turn_hook(new_message.text_content or "")


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


def resolve_agent_for_call(ctx: agents.JobContext):
    """Which Agent Builder persona should take this call.

    Inbound SIP participants carry the dialled DID in their attributes. If that number is assigned
    to an agent on the Call Desk (SIP Trunks & DIDs tab), that agent answers, so several agents can
    each own a phone number. Otherwise the agent marked live in the builder answers.
    Outbound calls can also choose a specific persona via room name, room metadata, or dial parameters.
    """
    from agent.agent_builder import agent_builder
    try:
        from agent.telephony_manager import telephony_manager, normalize_phone_number
    except Exception:
        telephony_manager, normalize_phone_number = None, None

    # 1. Agent Builder test calls or test room naming: test-<agent_id>-<stamp>
    m = re.match(r"^test-(.+?)-[a-z0-9]{4,}$", ctx.room.name or "")
    if m:
        cfg = agent_builder.get_agent(m.group(1))
        if cfg:
            log.info("Builder test call for agent '%s'", cfg.name)
            return cfg, ""
        # Check by name if ID was not matched
        wanted = m.group(1).strip().lower()
        for a in agent_builder.list_agents():
            if a["name"].strip().lower() == wanted or a["agent_id"] == wanted:
                cfg = agent_builder.get_agent(a["agent_id"])
                if cfg:
                    log.info("Builder test call matched agent '%s'", cfg.name)
                    return cfg, ""
        log.warning("Test room names unknown agent id %s; checking other indicators", m.group(1))

    # 2. Softphone / Outbound room naming: (softphone|outbound)-<agent_id>-<stamp>
    m_out = re.match(r"^(?:softphone|outbound)-([a-zA-Z0-9_-]+?)-(?:\d+|[a-z0-9]{4,})$", ctx.room.name or "")
    if m_out:
        candidate = m_out.group(1).strip()
        # Ensure candidate is not a generic timestamp or string like 'user'
        if candidate not in ("user", "call", "test") and not candidate.isdigit():
            cfg = agent_builder.get_agent(candidate)
            if not cfg:
                wanted = candidate.lower()
                for a in agent_builder.list_agents():
                    if a["name"].strip().lower() == wanted or a["agent_id"] == wanted:
                        cfg = agent_builder.get_agent(a["agent_id"])
                        break
            if cfg:
                log.info("Room name '%s' specifies agent '%s'; using it", ctx.room.name, cfg.name)
                return cfg, ""

    # 3. Room metadata: outbound_agent, agent_id, or agent
    try:
        meta = json.loads(ctx.room.metadata or "{}")
    except Exception:
        meta = {}
    outbound_agent = meta.get("outbound_agent") or meta.get("agent_id") or meta.get("agent")
    if outbound_agent:
        wanted = str(outbound_agent).strip().lower()
        for a in agent_builder.list_agents():
            if a["name"].strip().lower() == wanted or a["agent_id"] == wanted:
                cfg = agent_builder.get_agent(a["agent_id"])
                if cfg:
                    log.info("Room metadata specifies agent '%s'; using it", cfg.name)
                    return cfg, ""
        log.warning("Outbound call room asks for agent '%s' but no such agent exists; checking DID routing", outbound_agent)

    # 4. Participant attributes
    for p in list(ctx.room.remote_participants.values()):
        attrs = getattr(p, "attributes", {}) or {}
        p_agent = attrs.get("agent") or attrs.get("agent_id") or attrs.get("outbound_agent")
        if p_agent:
            wanted = str(p_agent).strip().lower()
            for a in agent_builder.list_agents():
                if a["name"].strip().lower() == wanted or a["agent_id"] == wanted:
                    cfg = agent_builder.get_agent(a["agent_id"])
                    if cfg:
                        log.info("Participant attribute specifies agent '%s'; using it", cfg.name)
                        return cfg, ""

    # 5. Inbound SIP participant attributes (dialed DID)
    dialled = ""
    for p in list(ctx.room.remote_participants.values()):
        attrs = getattr(p, "attributes", {}) or {}
        dialled = (
            attrs.get("sip.trunkPhoneNumber")
            or attrs.get("sip.calledNumber")
            or attrs.get("sip.toNumber")
            or attrs.get("sip.dialedNumber")
            or dialled
        )
        if dialled:
            break
    if not dialled:
        # Only an explicit E.164 number in the room name counts; "softphone-1789381564553" is a timestamp.
        m_num = re.search(r"(\+\d{10,15})", ctx.room.name or "")
        dialled = m_num.group(1) if m_num else ""

    if dialled and telephony_manager is not None:
        try:
            norm = normalize_phone_number(dialled)
            # Direct owned number match
            rec = telephony_manager._owned_numbers.get(norm)
            if rec and rec.status == "active" and rec.assigned_agent:
                wanted = rec.assigned_agent.strip().lower()
                for a in agent_builder.list_agents():
                    if a["name"].strip().lower() == wanted or a["agent_id"] == wanted:
                        cfg = agent_builder.get_agent(a["agent_id"])
                        if cfg:
                            log.info("DID %s is assigned to agent '%s'; using it for this call", norm, cfg.name)
                            return cfg, norm
                log.warning("DID %s is assigned to '%s' but no such agent exists; using the live agent", norm, rec.assigned_agent)
            else:
                # Inbound trunk / dispatch rule match fallback
                for trunk in telephony_manager._inbound_trunks.values():
                    if trunk.matches_number(norm):
                        for rule in telephony_manager._dispatch_rules.values():
                            if trunk.trunk_id in rule.trunk_ids and rule.agent_name:
                                wanted = rule.agent_name.strip().lower()
                                for a in agent_builder.list_agents():
                                    if a["name"].strip().lower() == wanted or a["agent_id"] == wanted:
                                        cfg = agent_builder.get_agent(a["agent_id"])
                                        if cfg:
                                            log.info("Trunk %s / Rule %s maps DID %s to agent '%s'", trunk.trunk_id, rule.rule_id, norm, cfg.name)
                                            return cfg, norm
        except Exception as e:
            log.warning("DID → agent lookup failed (%s); using the live agent", e)
    return agent_builder.get_active_agent(), dialled


def sip_caller_number(ctx: agents.JobContext) -> str:
    """The caller's number on an inbound SIP call (LiveKit sets sip.phoneNumber), else ''."""
    for p in list(ctx.room.remote_participants.values()):
        attrs = getattr(p, "attributes", {}) or {}
        num = attrs.get("sip.phoneNumber") or attrs.get("sip.callerNumber") or attrs.get("sip.fromNumber")
        if num:
            return num
    return ""


def call_direction(room_name: str) -> str:
    """How the call was placed, from the room naming the Call Desk uses."""
    name = room_name or ""
    if name.startswith("test-"):
        return "sandbox"
    if name.startswith(("outbound-", "softphone-", "dial-", "dispatch-")):
        return "outbound"
    return "inbound"


def build_stt(hand_off_audio: bool):
    """The caller's ears: Deepgram Nova streaming unless STT_PROVIDER=gemini.

    hand_off_audio=True (dialogue backend is Gemini): the utterance audio goes straight into the
    dialogue call, which transcribes and answers in one round trip. False (OpenAI dialogue): Gemini
    transcribes first and the text is handed to the dialogue model.
    """
    from agent.gemini_stt import GeminiSTT, gemini_api_key
    if STT_PROVIDER == "gemini":
        if gemini_api_key():
            engine = GeminiSTT(language="en", hand_off_audio=hand_off_audio)
            log.info("STT: Gemini listens to the caller directly (%s, %s)", engine.model,
                     "audio handed to the dialogue call" if hand_off_audio else "transcribe then answer")
            return engine
        log.warning("STT_PROVIDER=%s but no GEMINI_API_KEY; falling back to Deepgram %s", STT_PROVIDER, STT_MODEL)
    log.info("STT: Deepgram %s streaming", STT_MODEL)
    return deepgram.STT(model=STT_MODEL, language="en", interim_results=True, punctuate=True)


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()
    log.info("joined room %s", ctx.room.name)

    # Local Silero VAD model running on CPU via ONNX Runtime.
    vad = silero.VAD.load(
        min_speech_duration=float(os.getenv("VAD_MIN_SPEECH", "0.12")),   # a click or breath is not speech
        min_silence_duration=0.45,                                        # 450ms silence marks end-of-speech
        prefix_padding_duration=0.3,                                      # audio kept from before speech start
        activation_threshold=float(os.getenv("VAD_THRESHOLD", "0.6")),    # 0.5 default fires on room noise
    )

    tts_manager = StreamingTTSManager(
        model=TTS_MODEL,
        voice=CARTESIA_VOICE_ID,
        sample_rate=TTS_SAMPLE_RATE,
    )

    # The Agent Builder's active persona decides the prompt *and* the voice for real calls.
    # Without this the picker's choice never left the browser: every call spoke Deepgram's default.
    from agent.agent_builder import agent_builder
    # An inbound SIP caller may still be joining when the job starts; resolving the agent before
    # they arrive reads no sip.* attributes and the wrong persona answers. Bounded so test/verify
    # rooms without a participant yet do not hang.
    if not ctx.room.remote_participants:
        try:
            await asyncio.wait_for(ctx.wait_for_participant(), timeout=float(os.getenv("PARTICIPANT_WAIT_S", "10")))
        except asyncio.TimeoutError:
            log.warning("No participant joined %s within the wait window; resolving agent without SIP attributes", ctx.room.name)
        except Exception as e:
            log.debug("wait_for_participant: %s", e)
    active_cfg, dialled_number = resolve_agent_for_call(ctx)
    caller_number = sip_caller_number(ctx)
    direction = call_direction(ctx.room.name)
    if active_cfg:
        voice_opt = agent_builder.get_voice(active_cfg.voice_id)
        tts_manager.apply_voice(
            active_cfg.voice_id,
            provider=voice_opt.provider if voice_opt else "",
            gender=voice_opt.gender if voice_opt else "female",
            voice_name=voice_opt.name if voice_opt else "",
        )
        log.info("Agent '%s' loaded for this call (dialled=%s): voice=%s engine=%s",
                 active_cfg.name, dialled_number or "n/a", active_cfg.voice_id, tts_manager.provider)

    llm_manager = StreamingDialogueManager(model=(active_cfg.llm_model if active_cfg and active_cfg.llm_model else LLM_MODEL))
    stt_engine = build_stt(hand_off_audio=(llm_manager.backend == "gemini"))
    session = AgentSession(
        vad=vad,
        stt=stt_engine,
        tts=tts_manager.tts,
        turn_handling={
            "turn_detection": "vad",
            # Retell-style turn-taking: commit the caller's turn quickly after they stop, but give a
            # clearly unfinished sentence ("my name is...") up to 1.2 s before the model answers.
            "endpointing": {
                "mode": "dynamic",
                "min_delay": float(os.getenv("ENDPOINT_MIN_DELAY", "0.8")),
                "max_delay": float(os.getenv("ENDPOINT_MAX_DELAY", "2.0")),
            },
            # An interruption needs real words from the recogniser (min_words), not just VAD energy:
            # room noise, breaths and echo no longer cut her off, and a false interruption resumes.
            "interruption": {
                "enabled": True,
                "mode": "vad",
                "min_duration": float(os.getenv("INTERRUPT_MIN_DURATION", "0.4")),
                # 2 words: "yeah" / "okay" while she talks is a backchannel, not an interruption. If a
                # pause turns out to be a false alarm she resumes after 1 s instead of the default 2.
                "min_words": int(os.getenv("INTERRUPT_MIN_WORDS", "2")),
                "resume_false_interruption": True,
                "false_interruption_timeout": float(os.getenv("FALSE_INTERRUPT_TIMEOUT", "1.0")),
                "discard_audio_if_uninterruptible": True,
            },
        },
    )

    assistant_agent: Optional[VoiceAssistantAgent] = None
    if active_cfg:
        llm_manager.system_instruction = active_cfg.system_prompt
        llm_manager.context.system_instruction = active_cfg.system_prompt
        llm_manager.temperature = active_cfg.temperature
        llm_manager.enabled_tools = set(active_cfg.tools or [])
    log.info("Dialogue backend: %s (%s)", llm_manager.backend, llm_manager.model)
    current_turn_texts: list[str] = []
    current_turn_audio: list[bytes] = []      # WAV clips of this turn when Gemini hears the caller directly
    user_is_speaking = False
    call_started_at = time.time()
    user_speech_started_at = 0.0
    user_speech_ended_at = 0.0
    call_turns: list[dict] = []               # what was said and when; becomes the Call History transcript
    in_flight = {"audio": None, "text": "", "heard": False, "replied": False}   # the turn being answered
    turn_seq = {"n": 0}                        # bumps on every started turn
    finalized = {"done": False}

    def note_turn(role: str, text: str, start: float, end: float, **extra):
        text = (text or "").strip()
        if not text:
            return
        turn = {"role": role, "text": text, "timestamp": float(start), "end_timestamp": float(max(end, start))}
        turn.update({k: v for k, v in extra.items() if v is not None})
        call_turns.append(turn)

    def caller_turn_window():
        """When the caller's current utterance was spoken, from the VAD; falls back to 'just now'."""
        now = time.time()
        start = user_speech_started_at or (now - 2.0)
        end = user_speech_ended_at if user_speech_ended_at >= start else now
        return start, end
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

    def bind_tts_metrics(engine):
        try:
            engine.on("metrics_collected", on_tts_metrics)
        except Exception as e:
            log.debug("TTS metrics hook not attached: %s", e)

    bind_tts_metrics(tts_manager.tts)

    def publish_voice_state(agent_cfg):
        info = getattr(tts_manager, "voice_info", {}) or {}
        publish({
            "type": "agent_config_event",
            "event": "voice_engine",
            "agent_id": agent_cfg.agent_id if agent_cfg else None,
            "voice_id": info.get("requested") or tts_manager.voice,
            "voice_name": info.get("voice_name", ""),
            "requested_provider": info.get("requested_provider", ""),
            "engine": info.get("engine") or tts_manager.provider,
            "model": info.get("model", ""),
            "fallback_reason": info.get("fallback_reason", ""),
            "timestamp": time.time(),
        }, topic="agent_config_event", reliable=True)

    def publish_caller_transcript(text: str):
        """What the caller said, as reported by the model that heard the audio."""
        publish({"type": "transcript", "text": text, "is_final": True, "speaker": "caller",
                 "timestamp": time.time()}, topic="transcript", reliable=True)
        log.info("FINAL (heard by Gemini) %s", text)
        in_flight["heard"] = True
        st, en = caller_turn_window()
        note_turn("user", text, st, en)
        if text.strip():
            amd_res = amd_manager.process_transcript(ctx.room.name, text)
            if amd_res.state in (AMDState.MACHINE_GREETING, AMDState.VOICEMAIL_BEEP, AMDState.HUMAN):
                publish({"type": "amd_event", "call_id": ctx.room.name, "state": amd_res.state.value,
                         "confidence": amd_res.confidence, "reason": amd_res.reason, "action": amd_res.action.value,
                         "timestamp": time.time()}, topic="amd_event", reliable=True)

    async def execute_llm_turn(user_input: str, user_audio: Optional[bytes] = None):
        if (not user_input.strip() and not user_audio) or finalized["done"]:
            return
        log.info("Starting streaming LLM turn for input: '%s'%s", user_input, " + caller audio" if user_audio else "")
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
            in_flight["replied"] = True
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
                user_audio=user_audio,
                on_transcript=publish_caller_transcript,
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
                    "backend": llm_manager.backend,
                    "timestamp": time.time(),
                }, topic="agent_reply", reliable=True)
                publish({
                    "type": "chat_history",
                    "messages": llm_manager.context.to_list(),
                    "timestamp": time.time(),
                }, topic="chat_history", reliable=True)

                # Streaming TTS speech synthesis via session.say
                speech_handle = None
                say_at = time.time()
                try:
                    speech_handle = session.say(
                        metrics["text"],
                        allow_interruptions=True,
                    )
                    await speech_handle.wait_for_playout()
                    if speech_handle and speech_handle.interrupted:
                        publish({"type": "interruption", "interrupted": True, "reason": "user_barge_in",
                                 "timestamp": time.time()}, topic="interruption", reliable=True)
                    note_turn("assistant", metrics["text"], say_at + 0.25, time.time(), backend=llm_manager.backend,
                              interrupted=bool(speech_handle and speech_handle.interrupted),
                              tool=(metrics.get("tool_calls") or [{}])[0].get("tool") if metrics.get("tool_calls") else None)
                    from agent.agent_builder import looks_like_closing
                    if llm_manager.backend != "mock" and looks_like_closing(metrics["text"]) \
                            and not (speech_handle and speech_handle.interrupted):
                        # The agent signed off: end the call instead of sitting on an open line.
                        publish({"type": "call_end", "reason": "agent_closed", "text": metrics["text"],
                                 "timestamp": time.time()}, topic="agent_state", reliable=True)
                        await asyncio.sleep(1.0)
                        try:
                            from livekit import api as lk_api
                            await ctx.api.room.delete_room(lk_api.DeleteRoomRequest(room=ctx.room.name))
                            log.info("Call closed by agent sign-off; room %s deleted", ctx.room.name)
                        except Exception as end_err:
                            log.warning("Could not delete room after sign-off (%s); shutting the job down", end_err)
                            ctx.shutdown(reason="agent closed the call")
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
                "type": "agent_error",
                "error": str(err)[:300],
                "backend": llm_manager.backend,
                "model": llm_manager.model,
                "timestamp": time.time(),
            }, topic="agent_state", reliable=True)
            # Never leave a live caller in silence when the model call fails.
            try:
                await session.say("I'm sorry, I'm having a little trouble on my end. Could you say that once more?",
                                  allow_interruptions=True).wait_for_playout()
            except Exception as say_err:
                log.error("Recovery line failed too: %s", say_err)
            publish({
                "type": "agent_state",
                "state": "listening",
                "old_state": "error",
                "timestamp": time.time(),
            }, topic="agent_state", reliable=True)

    async def on_turn_committed(text: str):
        """LiveKit merged the caller's finals into one turn; answer it (carrying over anything an
        interrupted previous turn never got to answer)."""
        nonlocal turn_process_task
        if finalized["done"]:
            return
        tokens = text.split()
        handles = [t for t in tokens if t.startswith(AUDIO_MARK)]
        words = " ".join(t for t in tokens if not t.startswith(AUDIO_MARK)).strip()
        for h in handles:
            parked = stt_engine.take_audio(h) if hasattr(stt_engine, "take_audio") else None
            if parked:
                current_turn_audio.append(parked[0])
        if words:
            current_turn_texts.append(words)
        if not current_turn_texts and not current_turn_audio:
            return
        if turn_process_task and not turn_process_task.done():
            if in_flight["replied"]:
                # She has already started answering the previous turn: this is a new question.
                llm_manager.cancel_active_generation()
                try:
                    turn_process_task.cancel()
                except Exception:
                    pass
            else:
                # Previous turn was cut off before any reply: fold its words/audio into this one.
                llm_manager.cancel_active_generation()
                try:
                    turn_process_task.cancel()
                except Exception:
                    pass
                if in_flight["text"]:
                    current_turn_texts.insert(0, in_flight["text"])
                if in_flight["audio"] and not in_flight["heard"]:
                    current_turn_audio.insert(0, in_flight["audio"])
        combined = " ".join(current_turn_texts).strip()
        current_turn_texts.clear()
        audio = concat_wavs(current_turn_audio) if current_turn_audio else None
        current_turn_audio.clear()
        in_flight.update(audio=audio, text=combined, heard=False, replied=False)
        turn_seq["n"] += 1
        turn_process_task = asyncio.create_task(execute_llm_turn(combined, audio))
        pending.add(turn_process_task)
        turn_process_task.add_done_callback(pending.discard)

    @session.on("user_input_transcribed")
    def on_transcript(ev):
        if ev.transcript.startswith(AUDIO_MARK):
            return   # a handle to utterance audio; collected when LiveKit commits the turn
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
            st, en = caller_turn_window()
            note_turn("user", ev.transcript.strip(), st, en)

    @session.on("user_state_changed")
    def on_user_state(ev):
        nonlocal user_is_speaking, user_speech_started_at, user_speech_ended_at
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
            user_speech_started_at = time.time()
            # Interruption of her speech is LiveKit's job (turn_handling.interruption: needs words, resumes
            # if it was a false alarm). A turn she has not answered yet keeps generating; if the caller's
            # new words commit a new turn, on_turn_committed cancels and merges it.
        elif ev.new_state == "listening":
            user_is_speaking = False
            user_speech_ended_at = time.time()
            if current_turn_texts or current_turn_audio:
                # A barge-in parked the caller's words. If that sound turns out to be nothing (no
                # transcript, so LiveKit commits no turn), answer the parked words ourselves.
                seq = turn_seq["n"]

                async def answer_parked():
                    await asyncio.sleep(1.0)
                    if finalized["done"] or turn_seq["n"] != seq or user_is_speaking:
                        return
                    if (current_turn_texts or current_turn_audio) and not (turn_process_task and not turn_process_task.done()):
                        log.info("No new words after the barge-in; answering what the caller already said")
                        await on_turn_committed("")
                t = asyncio.create_task(answer_parked())
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
            # LiveKit judged this a real interruption (words, not noise) and is stopping her itself;
            # drop any reply still being generated so she does not answer the old turn.
            llm_manager.cancel_active_generation()
            tts_manager.interrupt_playback()
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
            note_turn("user", prompt, time.time(), time.time(), typed=True)
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

        elif action == "analyze_sentiment":
            from agent.sentiment_analyzer import sentiment_analyzer
            call_id = data.get("call_id", ctx.room.name)
            turns = data.get("transcript_turns")
            if not turns:
                turns = []
                for msg in llm_manager.context.to_list():
                    turns.append({"role": msg.get("role", "caller"), "text": msg.get("content", "")})
            analytics = sentiment_analyzer.analyze_and_summarize(
                call_id=call_id,
                transcript_turns=turns,
                metadata=data.get("metadata", {}),
            )
            publish({
                "type": "analytics_event",
                "event": "sentiment_analyzed",
                "call_id": call_id,
                "analytics": analytics,
                "timestamp": time.time(),
            }, topic="analytics_event", reliable=True)

        elif action == "extract_schema":
            from agent.schema_extractor import schema_extractor, list_schemas
            call_id = data.get("call_id", ctx.room.name)
            schema_id = data.get("schema_id", "legal_intake")
            turns = data.get("transcript_turns")
            if not turns:
                turns = []
                for msg in llm_manager.context.to_list():
                    turns.append({"role": msg.get("role", "caller"), "text": msg.get("content", "")})
            try:
                result = schema_extractor.extract(
                    schema_id=schema_id,
                    call_id=call_id,
                    transcript_turns=turns,
                    metadata=data.get("metadata", {}),
                )
                publish({
                    "type": "extraction_event",
                    "event": "extraction_completed",
                    "call_id": call_id,
                    "schema_id": schema_id,
                    "extraction": result.to_dict(),
                    "crm_payload": result.to_crm_payload(),
                    "timestamp": time.time(),
                }, topic="extraction_event", reliable=True)
            except KeyError as ke:
                publish({
                    "type": "extraction_event",
                    "event": "extraction_error",
                    "call_id": call_id,
                    "error": str(ke),
                    "available_schemas": list_schemas(),
                    "timestamp": time.time(),
                }, topic="extraction_event", reliable=True)

        elif action == "list_agents":
            from agent.agent_builder import agent_builder
            active_cfg = agent_builder.get_active_agent()
            publish({
                "type": "agent_config_event",
                "event": "agents_listed",
                "agents": agent_builder.list_agents(),
                "active_agent": active_cfg.agent_id if active_cfg else None,
                "timestamp": time.time(),
            }, topic="agent_config_event", reliable=True)

        elif action == "apply_agent_config":
            from agent.agent_builder import agent_builder
            agent_id = data.get("agent_id", "")
            cfg = agent_builder.get_agent(agent_id)
            if not cfg:
                publish({
                    "type": "agent_config_event",
                    "event": "agent_config_error",
                    "error": f"Unknown agent '{agent_id}'",
                    "agents": [a["agent_id"] for a in agent_builder.list_agents()],
                    "timestamp": time.time(),
                }, topic="agent_config_event", reliable=True)
            else:
                # Swap the live persona: prompt and sampling on the dialogue
                # manager, voice on the TTS stream, for the next turn onwards.
                llm_manager.system_instruction = cfg.system_prompt
                llm_manager.context.system_instruction = cfg.system_prompt
                llm_manager.temperature = cfg.temperature
                if cfg.llm_model:
                    llm_manager.configure_backend(model=cfg.llm_model)
                llm_manager.enabled_tools = set(cfg.tools or [])
                voice_opt = agent_builder.get_voice(cfg.voice_id)
                tts_manager.apply_voice(
                    cfg.voice_id,
                    provider=voice_opt.provider if voice_opt else "",
                    gender=voice_opt.gender if voice_opt else "female",
                    voice_name=voice_opt.name if voice_opt else "",
                )
                bind_tts_metrics(tts_manager.tts)
                if assistant_agent is not None:
                    try:
                        assistant_agent.update_options(tts=tts_manager.tts)
                    except Exception as e:
                        log.warning("Could not swap live TTS engine: %s", e)
                publish_voice_state(cfg)
                if data.get("activate"):
                    agent_builder.set_active(agent_id)
                publish({
                    "type": "agent_config_event",
                    "event": "agent_config_applied",
                    "agent_id": cfg.agent_id,
                    "agent_name": cfg.name,
                    "revision": cfg.revision,
                    "voice_id": cfg.voice_id,
                    "llm_backend": llm_manager.backend,
                    "llm_model": llm_manager.model,
                    "voice_engine": tts_manager.provider,
                    "voice_fallback_reason": (getattr(tts_manager, "voice_info", {}) or {}).get("fallback_reason", ""),
                    "temperature": cfg.temperature,
                    "tools": cfg.tools,
                    "first_message": cfg.first_message,
                    "prompt_chars": len(cfg.system_prompt),
                    "timestamp": time.time(),
                }, topic="agent_config_event", reliable=True)

        elif action == "test_agent_config":
            from agent.agent_builder import agent_builder
            try:
                preview = agent_builder.test_run(
                    data.get("agent_id", ""), data.get("utterances", []))
                publish({
                    "type": "agent_config_event",
                    "event": "agent_test_completed",
                    "result": preview,
                    "timestamp": time.time(),
                }, topic="agent_config_event", reliable=True)
            except KeyError as ke:
                publish({
                    "type": "agent_config_event",
                    "event": "agent_config_error",
                    "error": str(ke),
                    "timestamp": time.time(),
                }, topic="agent_config_event", reliable=True)

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

    assistant_agent = VoiceAssistantAgent()
    assistant_agent.turn_hook = on_turn_committed
    # LiveKit's own RecorderIO writes a dual-channel OGG (caller left, agent right, agent placed
    # where it was actually played); it is converted to WAV for Call History when the call ends.
    record_audio = not ctx.room.name.startswith("verify-")
    await session.start(agent=assistant_agent, room=ctx.room,
                        record={"audio": record_audio, "traces": False, "logs": False, "transcript": False})
    # Jitter buffer in front of the room output: TTS audio arrives in bursts over the network and the
    # room plays through a 200 ms queue, so late bursts became audible gaps in her voice.
    try:
        from agent.audio_buffer import BufferedAudioOutput
        prebuffer_ms = int(os.getenv("TTS_PREBUFFER_MS", "1000"))
        if prebuffer_ms > 0 and session.output.audio is not None:
            session.output.audio = BufferedAudioOutput(session.output.audio, prebuffer_ms=prebuffer_ms)
            log.info("Playout jitter buffer: %d ms", prebuffer_ms)
    except Exception as e:
        log.warning("Playout jitter buffer not installed: %s", e)
    publish_voice_state(active_cfg)

    # ── Call History for every LiveKit call (test rooms and real ones) ─────────────────────────
    is_test_room = ctx.room.name.startswith("test-")

    def export_recording() -> Optional[str]:
        """The framework's audio.ogg → recordings/rec-<room>-<ts>.wav (16 kHz stereo) for the dashboard."""
        src = None
        try:
            src = os.path.join(str(ctx.session_directory), "audio.ogg")
        except Exception:
            return None
        if not src or not os.path.isfile(src) or os.path.getsize(src) < 200:
            return None
        try:
            from agent.call_history import call_history as _ch
            rec_dir = _ch.recordings_dir
        except Exception:
            rec_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recordings")
        os.makedirs(rec_dir, exist_ok=True)
        dst = os.path.join(rec_dir, f"rec-{ctx.room.name}-{int(call_started_at)}.wav")
        try:
            import av, wave
            container = av.open(src)
            stream = next(s for s in container.streams if s.type == "audio")
            resampler = av.AudioResampler(format="s16", layout="stereo", rate=16000)
            with wave.open(dst, "wb") as w:
                w.setnchannels(2); w.setsampwidth(2); w.setframerate(16000)
                for frame in container.decode(stream):
                    for out in resampler.resample(frame):
                        w.writeframes(out.to_ndarray().tobytes())
                for out in resampler.resample(None):
                    w.writeframes(out.to_ndarray().tobytes())
            container.close()
            try:
                from agent import storage
                storage.save_recording(dst, ctx.room.name)
            except Exception:
                pass
            return dst
        except Exception as e:
            log.warning("Recording export failed (%s); keeping %s", e, src)
            return None

    async def finalize_call(reason: str = "caller_left"):
        """Close the recording and file the call in Call History (runs once)."""
        if finalized["done"] or ctx.room.name.startswith("verify-"):
            return
        finalized["done"] = True
        ended = time.time()
        try:
            await session.aclose()          # flushes the recorder so audio.ogg is complete
        except Exception as e:
            log.debug("session close during finalize: %s", e)
        audio_path = (await asyncio.to_thread(export_recording)) if record_audio else None   # decode/encode off the loop
        if not call_turns:
            log.info("Call %s ended with nothing said; not filed", ctx.room.name)
            return
        turns = []
        for i, t in enumerate(sorted(call_turns, key=lambda x: x["timestamp"]), start=1):
            turns.append({
                "turn_index": i,
                "role": t["role"],
                "speaker": "Customer" if t["role"] == "user" else (active_cfg.name if active_cfg else "AI Agent"),
                "text": t["text"],
                "timestamp": t["timestamp"],
                "end_timestamp": t.get("end_timestamp"),
                "word_count": len(t["text"].split()),
                "tool": t.get("tool"),
                "backend": t.get("backend"),
            })
        try:
            job = pipeline_worker.enqueue_call(
                call_id=ctx.room.name,
                room_name=ctx.room.name,
                transcript_turns=turns,
                audio_path=audio_path,
                metadata={
                    "source": "livekit-test-call" if is_test_room else "livekit",
                    "agent_name": active_cfg.name if active_cfg else "AI Agent",
                    "agent_id": active_cfg.agent_id if active_cfg else "",
                    "direction": direction,
                    # Inbound: caller -> our DID. Outbound: our DID -> the number dialled from the desk.
                    "from_number": "Agent Builder test call" if is_test_room else (
                        caller_number if direction == "inbound" else dialled_number or ""),
                    "to_number": (dialled_number or (active_cfg.name if active_cfg else "")) if direction == "inbound"
                                 else (caller_number or dialled_number or ""),
                    "voice_id": active_cfg.voice_id if active_cfg else "",
                    "llm_backend": f"{llm_manager.backend}:{llm_manager.model}",
                    "stt": getattr(stt_engine, "label", STT_PROVIDER),
                    "started_at": call_started_at,
                    "ended_at": ended,
                    "duration_seconds": max(0.0, ended - call_started_at),
                    "end_reason": reason,
                },
            )
            await pipeline_worker.execute_job(job.job_id)
            log.info("Call %s filed in Call History (%d turns, audio=%s)", ctx.room.name, len(turns), bool(audio_path))
        except Exception as e:
            log.error("Could not file call %s: %s", ctx.room.name, e)

    @ctx.room.on("participant_disconnected")
    def on_participant_left(participant):
        if not [p for p in ctx.room.remote_participants.values() if p.identity != participant.identity]:
            t = asyncio.create_task(finalize_call("caller_left"))
            pending.add(t)
            t.add_done_callback(pending.discard)
            t.add_done_callback(lambda _t: ctx.shutdown(reason="caller left"))

    async def _on_shutdown():
        await finalize_call("shutdown")
    ctx.add_shutdown_callback(_on_shutdown)

    # Real callers expect the agent to speak first (Agent Builder "Who speaks first" = AI). Play the
    # configured first message as soon as a caller is in the room; test/verify rooms that drive the
    # agent over the data channel opt out with the "verify-" room prefix.
    if active_cfg and getattr(active_cfg, "speak_first", "ai") == "ai" and active_cfg.first_message \
            and not ctx.room.name.startswith("verify-"):
        greeting = active_cfg.first_message
        llm_manager.context.add_assistant_message(greeting)

        async def greet():
            try:
                await asyncio.sleep(0.6)
                publish({"type": "agent_reply", "text": greeting, "metrics": {"greeting": True},
                         "backend": llm_manager.backend, "timestamp": time.time()}, topic="agent_reply", reliable=True)
                say_at = time.time()
                await session.say(greeting, allow_interruptions=True).wait_for_playout()
                note_turn("assistant", greeting, say_at + 0.25, time.time(), backend=llm_manager.backend, greeting=True)
            except Exception as e:
                log.warning("Greeting playback failed: %s", e)

        gtask = asyncio.create_task(greet())
        pending.add(gtask)
        gtask.add_done_callback(pending.discard)
    log.info("dialogue worker active with Silero VAD, Streaming LLM (%s) & TTS engine %s (%s)",
             LLM_MODEL, tts_manager.provider, getattr(tts_manager, "voice_info", {}).get("model") or TTS_MODEL)


if __name__ == "__main__":
    if not os.getenv("DEEPGRAM_API_KEY"):
        raise SystemExit(
            "DEEPGRAM_API_KEY is not set.\n"
            "  Get a key at https://console.deepgram.com (free tier includes credit),\n"
            "  add it to .env as DEEPGRAM_API_KEY=..., then: docker compose up -d agent"
        )
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))

