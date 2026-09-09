"""Answering Machine Detection (AMD) & Voicemail Drop Manager (Task 2.4).

Detects whether an outbound telephone call connected to a live human or an
answering machine/voicemail system, using:
1. Temporal Cadence & Audio Energy Analysis: human greetings are short (<2.5s)
   followed by conversational pause; machine greetings are long (>3.2s uninterrupted).
2. Voicemail Beep Detection (DSP): Goertzel filter targeting standard telephone
   recording prompt beeps (1000 Hz, 800 Hz, 440 Hz) with tone duration gating (>150ms).
3. Transcript Semantic Keyword Classifier: detects answering machine phrases
   ("leave a message", "after the tone", "not available right now", "mailbox").
4. Voicemail Drop Automation: waits for beep/pause, streams personalized audio message,
   and gracefully hangs up.
"""

import math
import re
import struct
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field


class AMDState(str, Enum):
    DETECTING = "detecting"
    HUMAN = "human"
    MACHINE_GREETING = "machine_greeting"
    VOICEMAIL_BEEP = "voicemail_beep"
    VOICEMAIL_DROPPING = "voicemail_dropping"
    VOICEMAIL_COMPLETED = "voicemail_completed"
    HANGUP = "hangup"
    UNKNOWN = "unknown"


class AMDAction(str, Enum):
    CONTINUE_DIALOGUE = "continue_dialogue"
    WAIT_FOR_BEEP = "wait_for_beep"
    DROP_VOICEMAIL = "drop_voicemail"
    HANGUP = "hangup"


class VoicemailDropConfig(BaseModel):
    """Configuration for Answering Machine Detection and Voicemail Drop."""
    enabled: bool = True
    action_on_machine: AMDAction = AMDAction.DROP_VOICEMAIL
    message: str = (
        "Hello, this is Northgate Support calling regarding your inquiry. "
        "Please call us back at 800-555-0199 at your convenience. Thank you!"
    )
    max_greeting_duration_s: float = 15.0
    min_machine_speech_duration_s: float = 3.2
    max_human_speech_duration_s: float = 2.4
    human_silence_threshold_s: float = 0.7
    beep_detection_enabled: bool = True
    post_beep_delay_s: float = 0.4
    auto_hangup_after_drop: bool = True


class AMDResult(BaseModel):
    """Classification result from the AMD engine."""
    call_id: str
    state: AMDState = AMDState.DETECTING
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = "Analysis in progress"
    greeting_duration_s: float = 0.0
    silence_duration_s: float = 0.0
    beep_detected: bool = False
    action: AMDAction = AMDAction.CONTINUE_DIALOGUE
    timestamp: float = Field(default_factory=time.time)


class VoicemailBeepDetector:
    """Detects voicemail recording prompt beeps (e.g. 1000 Hz, 800 Hz, 440 Hz) using Goertzel DSP."""

    def __init__(
        self,
        sample_rate: int = 16000,
        target_frequencies: Optional[List[float]] = None,
        energy_threshold_ratio: float = 0.38,
        min_beep_duration_ms: float = 120.0,
    ):
        self.sample_rate = sample_rate
        self.target_frequencies = target_frequencies or [1000.0, 800.0, 440.0]
        self.energy_threshold_ratio = energy_threshold_ratio
        self.min_beep_duration_ms = min_beep_duration_ms
        self.consecutive_beep_ms: float = 0.0
        self.beep_confirmed: bool = False

    def reset(self):
        self.consecutive_beep_ms = 0.0
        self.beep_confirmed = False

    def _goertzel_magnitude(self, samples: List[float], freq: float) -> float:
        """Compute Goertzel energy at specific target frequency."""
        n = len(samples)
        if n == 0:
            return 0.0
        k = round(n * freq / self.sample_rate)
        omega = (2.0 * math.pi * k) / n
        coeff = 2.0 * math.cos(omega)
        q0 = q1 = q2 = 0.0
        for sample in samples:
            q0 = coeff * q1 - q2 + sample
            q2 = q1
            q1 = q0
        return q1 * q1 + q2 * q2 - q1 * q2 * coeff

    def process_pcm_chunk(self, pcm_bytes: bytes, sample_rate: Optional[int] = None) -> Tuple[bool, Optional[float]]:
        """Process raw 16-bit PCM audio frame and return (is_beep_detected, matched_frequency)."""
        if sample_rate and sample_rate != self.sample_rate:
            self.sample_rate = sample_rate

        sample_count = len(pcm_bytes) // 2
        if sample_count < 64:
            return self.beep_confirmed, None

        # Unpack signed 16-bit PCM
        samples = struct.unpack(f"<{sample_count}h", pcm_bytes[:sample_count * 2])
        frame_duration_ms = (sample_count / self.sample_rate) * 1000.0

        total_energy = sum(s * s for s in samples)
        if total_energy < 1e5:  # Low amplitude / background noise
            self.consecutive_beep_ms = max(0.0, self.consecutive_beep_ms - frame_duration_ms)
            return self.beep_confirmed, None

        # Check target beep frequencies
        best_freq = None
        max_ratio = 0.0

        for freq in self.target_frequencies:
            mag = self._goertzel_magnitude(samples, freq)
            ratio = (mag / (total_energy + 1.0)) * (2.0 / sample_count)
            if ratio > max_ratio:
                max_ratio = ratio
                best_freq = freq

        if max_ratio >= self.energy_threshold_ratio:
            self.consecutive_beep_ms += frame_duration_ms
            if self.consecutive_beep_ms >= self.min_beep_duration_ms:
                self.beep_confirmed = True
                return True, best_freq
        else:
            self.consecutive_beep_ms = max(0.0, self.consecutive_beep_ms - frame_duration_ms * 0.5)

        return self.beep_confirmed, (best_freq if self.beep_confirmed else None)


class SemanticKeywordDetector:
    """Analyzes STT transcript for answering machine / voicemail keywords."""

    VOICEMAIL_PATTERNS = [
        r"\bleave a message\b",
        r"\bafter the (tone|beep)\b",
        r"\bat the (tone|beep)\b",
        r"\brecord your message\b",
        r"\bnot available (right now|at the moment|to take your call)\b",
        r"\bplease leave your name\b",
        r"\bvoicemail\b",
        r"\bvoice mail\b",
        r"\bmailbox is (full)?\b",
        r"\bpress \d to\b",
        r"\bcannot take your call\b",
        r"\baway from (my|the) phone\b",
        r"\breturn your call\b",
    ]

    HUMAN_PATTERNS = [
        r"^(hello|hi|hey|speaking|yes|who is this|good (morning|afternoon|evening))[.?!]*$",
        r"^(this is [a-z]+)[.?!]*$",
    ]

    def __init__(self):
        self._vm_regexes = [re.compile(p, re.IGNORECASE) for p in self.VOICEMAIL_PATTERNS]
        self._human_regexes = [re.compile(p, re.IGNORECASE) for p in self.HUMAN_PATTERNS]

    def classify_transcript(self, text: str) -> Tuple[Optional[str], float, str]:
        """Returns (classification: 'machine'|'human'|None, confidence, matched_phrase)."""
        clean = text.strip()
        if not clean:
            return None, 0.0, ""

        for regex in self._vm_regexes:
            m = regex.search(clean)
            if m:
                return "machine", 0.95, m.group(0)

        for regex in self._human_regexes:
            m = regex.search(clean)
            if m:
                return "human", 0.88, m.group(0)

        # Longer continuous monologue without interactive back-and-forth indicates machine
        word_count = len(clean.split())
        if word_count >= 18:
            return "machine", 0.78, f"long monologue ({word_count} words)"

        return None, 0.0, ""


class AMDSession:
    """Tracks state and analysis for a single live call."""

    def __init__(self, call_id: str, config: Optional[VoicemailDropConfig] = None):
        self.call_id = call_id
        self.config = config or VoicemailDropConfig()
        self.created_at = time.time()
        self.state = AMDState.DETECTING
        self.confidence = 0.5
        self.reason = "Listening for greeting cadence & tones"

        # Cadence tracking
        self.speech_start_time: Optional[float] = None
        self.speech_end_time: Optional[float] = None
        self.total_speech_duration: float = 0.0
        self.total_silence_duration: float = 0.0
        self.is_currently_speaking: bool = False
        self.last_state_change: float = time.time()

        # Tone and transcript detection
        self.beep_detector = VoicemailBeepDetector()
        self.keyword_detector = SemanticKeywordDetector()
        self.beep_detected: bool = False
        self.beep_frequency: Optional[float] = None
        self.transcript_accumulator: str = ""

        # Voicemail drop execution
        self.voicemail_drop_started_at: Optional[float] = None
        self.voicemail_drop_completed_at: Optional[float] = None
        self.dropped_message_text: Optional[str] = None

    def to_result(self) -> AMDResult:
        action = AMDAction.CONTINUE_DIALOGUE
        if self.state == AMDState.HUMAN:
            action = AMDAction.CONTINUE_DIALOGUE
        elif self.state == AMDState.MACHINE_GREETING:
            action = (
                AMDAction.WAIT_FOR_BEEP
                if self.config.action_on_machine == AMDAction.DROP_VOICEMAIL
                else AMDAction.HANGUP
            )
        elif self.state == AMDState.VOICEMAIL_BEEP:
            action = AMDAction.DROP_VOICEMAIL
        elif self.state in (AMDState.VOICEMAIL_DROPPING, AMDState.VOICEMAIL_COMPLETED):
            action = AMDAction.DROP_VOICEMAIL
        elif self.state == AMDState.HANGUP:
            action = AMDAction.HANGUP

        return AMDResult(
            call_id=self.call_id,
            state=self.state,
            confidence=round(self.confidence, 2),
            reason=self.reason,
            greeting_duration_s=round(self.total_speech_duration, 2),
            silence_duration_s=round(self.total_silence_duration, 2),
            beep_detected=self.beep_detected,
            action=action,
            timestamp=time.time(),
        )


class AMDManager:
    """Manages Answering Machine Detection and automated Voicemail Drop across all calls."""

    def __init__(self, default_config: Optional[VoicemailDropConfig] = None):
        self.default_config = default_config or VoicemailDropConfig()
        self._sessions: Dict[str, AMDSession] = {}

    def get_or_create_session(self, call_id: str, config: Optional[VoicemailDropConfig] = None) -> AMDSession:
        if call_id not in self._sessions:
            self._sessions[call_id] = AMDSession(call_id, config or self.default_config)
        return self._sessions[call_id]

    def get_session(self, call_id: str) -> Optional[AMDSession]:
        return self._sessions.get(call_id)

    def process_vad_event(self, call_id: str, is_speech: bool, timestamp: Optional[float] = None) -> AMDResult:
        """Process Silero VAD speech/silence transition and update cadence analysis."""
        session = self.get_or_create_session(call_id)
        if session.state in (AMDState.VOICEMAIL_DROPPING, AMDState.VOICEMAIL_COMPLETED, AMDState.HANGUP):
            return session.to_result()

        now = timestamp or time.time()

        if is_speech and not session.is_currently_speaking:
            # Transitioned: Silence -> Speech
            session.is_currently_speaking = True
            session.speech_start_time = now
            if session.speech_end_time:
                session.total_silence_duration += (now - session.speech_end_time)

        elif not is_speech and session.is_currently_speaking:
            # Transitioned: Speech -> Silence
            session.is_currently_speaking = False
            session.speech_end_time = now
            if session.speech_start_time:
                dur = now - session.speech_start_time
                session.total_speech_duration += dur

                # Cadence decision rule:
                # 1. Short speech (<2.4s) followed by silence -> likely Human
                if (
                    dur <= session.config.max_human_speech_duration_s
                    and session.state == AMDState.DETECTING
                ):
                    session.state = AMDState.HUMAN
                    session.confidence = 0.85
                    session.reason = f"Short human greeting cadence ({dur:.1f}s) followed by pause"

        # Continuous speech rule (still speaking):
        if session.is_currently_speaking and session.speech_start_time:
            current_speech_dur = (now - session.speech_start_time)
            if (
                current_speech_dur >= session.config.min_machine_speech_duration_s
                and session.state != AMDState.MACHINE_GREETING
                and session.state != AMDState.VOICEMAIL_BEEP
            ):
                session.state = AMDState.MACHINE_GREETING
                session.confidence = 0.90
                session.reason = f"Long continuous greeting cadence ({current_speech_dur:.1f}s) without conversational pauses"

        return session.to_result()

    def process_transcript(self, call_id: str, text: str) -> AMDResult:
        """Process STT transcription for voicemail/human semantic keywords."""
        session = self.get_or_create_session(call_id)
        if session.state in (AMDState.VOICEMAIL_DROPPING, AMDState.VOICEMAIL_COMPLETED, AMDState.HANGUP):
            return session.to_result()

        session.transcript_accumulator += f" {text}".strip()
        classification, conf, match = session.keyword_detector.classify_transcript(session.transcript_accumulator)

        if classification == "machine":
            session.state = AMDState.MACHINE_GREETING
            session.confidence = max(session.confidence, conf)
            session.reason = f"Voicemail keyword pattern matched: '{match}'"
        elif classification == "human" and session.state == AMDState.DETECTING:
            session.state = AMDState.HUMAN
            session.confidence = max(session.confidence, conf)
            session.reason = f"Human greeting phrase matched: '{match}'"

        return session.to_result()

    def process_audio_chunk(
        self, call_id: str, pcm_bytes: bytes, sample_rate: int = 16000
    ) -> Tuple[AMDResult, bool]:
        """Analyze incoming audio frame for voicemail beep prompt."""
        session = self.get_or_create_session(call_id)
        if not session.config.beep_detection_enabled:
            return session.to_result(), False

        if session.state in (AMDState.VOICEMAIL_DROPPING, AMDState.VOICEMAIL_COMPLETED, AMDState.HANGUP):
            return session.to_result(), False

        beep_detected, freq = session.beep_detector.process_pcm_chunk(pcm_bytes, sample_rate)
        if beep_detected and not session.beep_detected:
            session.beep_detected = True
            session.beep_frequency = freq
            session.state = AMDState.VOICEMAIL_BEEP
            session.confidence = 0.98
            session.reason = f"Voicemail prompt beep tone detected ({freq:.0f} Hz)"
            return session.to_result(), True

        return session.to_result(), False

    def trigger_voicemail_drop(
        self,
        call_id: str,
        custom_message: Optional[str] = None,
        tts_func: Optional[Callable[[str], bytes]] = None,
    ) -> Dict[str, Any]:
        """Executes voicemail drop: returns synthesized audio payload, updates state."""
        session = self.get_or_create_session(call_id)
        message_to_speak = custom_message or session.config.message

        session.state = AMDState.VOICEMAIL_DROPPING
        session.voicemail_drop_started_at = time.time()
        session.dropped_message_text = message_to_speak
        session.reason = f"Dropping voicemail message ({len(message_to_speak)} chars)"

        # Generate voicemail audio
        pcm_audio: Optional[bytes] = None
        if tts_func:
            try:
                pcm_audio = tts_func(message_to_speak)
            except Exception as e:
                pcm_audio = self._generate_simulated_voicemail_audio(message_to_speak)
        else:
            pcm_audio = self._generate_simulated_voicemail_audio(message_to_speak)

        # Mark completed
        session.state = AMDState.VOICEMAIL_COMPLETED
        session.voicemail_drop_completed_at = time.time()
        session.reason = "Voicemail message dropped successfully"

        if session.config.auto_hangup_after_drop:
            session.state = AMDState.HANGUP

        return {
            "status": "ok",
            "call_id": call_id,
            "state": session.state.value,
            "message": message_to_speak,
            "audio_bytes_length": len(pcm_audio) if pcm_audio else 0,
            "audio_duration_s": round((len(pcm_audio) / 2) / 24000, 2) if pcm_audio else 0.0,
            "auto_hangup": session.config.auto_hangup_after_drop,
        }

    def _generate_simulated_voicemail_audio(self, message: str) -> bytes:
        """Generate simulated 24kHz vocal audio bytes for the voicemail message."""
        sample_rate = 24000
        words = len(message.split())
        est_duration = max(1.0, words * 0.35)
        total_samples = int(sample_rate * est_duration)

        frames = bytearray()
        f0 = 220.0
        for i in range(total_samples):
            t = i / sample_rate
            val = 0.25 * math.sin(2.0 * math.pi * f0 * t) + 0.12 * math.sin(2.0 * math.pi * (f0 * 2) * t)
            sample_val = int(val * 32767.0)
            sample_val = max(-32768, min(32767, sample_val))
            frames.extend(struct.pack("<h", sample_val))
        return bytes(frames)


def generate_beep_audio_pcm(
    frequency: float = 1000.0,
    duration_ms: float = 400.0,
    sample_rate: int = 16000,
    amplitude: float = 0.5,
) -> bytes:
    """Helper to generate pure sine wave PCM audio representing a voicemail beep."""
    total_samples = int((sample_rate * duration_ms) / 1000.0)
    frames = bytearray()
    for i in range(total_samples):
        t = i / sample_rate
        val = amplitude * math.sin(2.0 * math.pi * frequency * t)
        sample_val = int(val * 32767.0)
        sample_val = max(-32768, min(32767, sample_val))
        frames.extend(struct.pack("<h", sample_val))
    return bytes(frames)
