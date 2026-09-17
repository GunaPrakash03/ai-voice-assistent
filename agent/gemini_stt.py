"""
Gemini as the ears of the call: speech-to-text done by the language model itself.

The worker's Silero VAD cuts the caller's audio into utterances (via LiveKit's StreamAdapter, which
wraps any non-streaming STT). Each utterance is sent as inline WAV to Gemini ``generateContent`` with
a transcription-only instruction, and the text comes back as one FINAL transcript. No third-party
speech-recognition service is involved; Deepgram / ElevenLabs are used only for the voice.

Trade-off vs a streaming recogniser: there are no interim (partial) transcripts, and the transcript
arrives ~0.4-0.9 s after the caller stops talking (Gemini Flash Lite on a short clip). Interruption
handling is unaffected because it is driven by the VAD, not by the transcript.

Configuration: GEMINI_API_KEY (or GOOGLE_API_KEY); GEMINI_STT_MODEL overrides the model
(default: the workspace's Gemini chat model, resolved by agent_builder.resolve_gemini_model).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import time
import wave
from typing import Any, Optional

import aiohttp
try:
    from livekit import rtc
    from livekit.agents import APIConnectOptions, APIConnectionError, APIStatusError, stt, utils
    from livekit.agents.types import NOT_GIVEN, NotGivenOr
    from livekit.agents.utils import AudioBuffer
except ImportError:
    rtc, APIConnectOptions, APIConnectionError, APIStatusError, stt, utils, NOT_GIVEN, NotGivenOr, AudioBuffer = (
        None, None, None, None, None, None, None, None, None
    )

_STTBase = getattr(stt, "STT", object) if stt else object

log = logging.getLogger("gemini-stt")

TRANSCRIBE_INSTRUCTION = (
    "You are a speech-to-text engine for a phone call. Transcribe the caller's speech in this audio "
    "verbatim, in the language spoken. Output only the transcript text: no quotes, no labels, no "
    "commentary, no timestamps. Keep numbers, names and spellings exactly as spoken. If the audio "
    "contains no intelligible speech, output an empty string."
)


def gemini_api_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def _generation_config(model: str) -> dict:
    """Deterministic, no thinking: a transcript is not something to reason about."""
    cfg = {"temperature": 0.0, "maxOutputTokens": 512, "candidateCount": 1}
    if model.startswith("gemini-2.5"):
        cfg["thinkingConfig"] = {"thinkingBudget": 0}
    elif model.startswith("gemini-3"):
        cfg["thinkingConfig"] = {"thinkingLevel": "minimal"}
    return cfg


UPLOAD_RATE = 16000   # phone-grade; a 48 kHz room clip is 3x the upload for no transcription gain


def _to_wav(buffer: AudioBuffer) -> tuple[bytes, float]:
    """Merges the utterance frames into one mono 16 kHz 16-bit PCM WAV; returns (bytes, seconds)."""
    frame = utils.merge_frames(buffer)
    if frame.sample_rate != UPLOAD_RATE:
        resampler = rtc.AudioResampler(input_rate=frame.sample_rate, output_rate=UPLOAD_RATE,
                                       num_channels=frame.num_channels)
        frames = resampler.push(frame) + resampler.flush()
        if frames:
            frame = utils.merge_frames(frames)
    pcm = frame.data.tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(frame.num_channels)
        w.setsampwidth(2)
        w.setframerate(frame.sample_rate)
        w.writeframes(pcm)
    seconds = frame.samples_per_channel / float(frame.sample_rate or 1)
    return out.getvalue(), seconds


def concat_wavs(clips: "list[bytes]", max_seconds: float = 30.0) -> bytes:
    """Joins mono 16-bit WAV clips of the same rate into one (a turn the VAD split into pieces)."""
    if len(clips) == 1:
        return clips[0]
    pcm = b""
    rate = UPLOAD_RATE
    for clip in clips:
        with wave.open(io.BytesIO(clip), "rb") as w:
            rate = w.getframerate()
            pcm += w.readframes(w.getnframes())
    limit = int(max_seconds * rate) * 2
    if len(pcm) > limit:
        pcm = pcm[-limit:]
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


AUDIO_MARK = "\u2063audio:"   # invisible separator + tag: a FINAL "transcript" that is really an audio handle


class GeminiSTT(_STTBase):
    """Non-streaming STT: one VAD-segmented utterance at a time.

    Two modes, switchable per call via ``hand_off_audio``:
      * hand_off_audio=True  – no transcription here. The utterance WAV is parked in ``take_audio`` and
        the "transcript" is an AUDIO_MARK handle; the worker passes the audio to the Gemini dialogue
        call, which hears it and answers in one round trip (agent/llm_manager.py).
      * hand_off_audio=False – classic: Gemini transcribes the utterance and the text comes back.
    """

    def __init__(self, *, model: Optional[str] = None, language: str = "en", api_key: Optional[str] = None,
                 min_audio_seconds: float = 0.25, hand_off_audio: bool = False) -> None:
        if stt:
            super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        from agent.agent_builder import resolve_gemini_model
        self._model = resolve_gemini_model(model or os.getenv("GEMINI_STT_MODEL") or os.getenv("GEMINI_MODEL") or "")
        self._language = language
        self._api_key = (api_key or gemini_api_key())
        self._min_audio_seconds = min_audio_seconds
        if not self._api_key:
            raise ValueError("GeminiSTT needs GEMINI_API_KEY (or GOOGLE_API_KEY)")
        self._http: Optional[aiohttp.ClientSession] = None   # kept open: TLS handshake once per call, not per utterance
        self.hand_off_audio = hand_off_audio
        self._parked: "dict[str, tuple[bytes, float]]" = {}

    def take_audio(self, handle: str) -> Optional[tuple[bytes, float]]:
        """Returns (wav_bytes, seconds) for an AUDIO_MARK transcript, once."""
        return self._parked.pop(handle, None)

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300, keepalive_timeout=75))
        return self._http

    async def aclose(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()
        await super().aclose()

    @property
    def label(self) -> str:
        return f"gemini-stt:{self._model}"

    @property
    def model(self) -> str:
        return self._model

    async def _recognize_impl(self, buffer: AudioBuffer, *, language: NotGivenOr[str] = NOT_GIVEN,
                              conn_options: APIConnectOptions) -> stt.SpeechEvent:
        lang = language if language is not NOT_GIVEN and language else self._language
        wav, seconds = _to_wav(buffer)
        request_id = utils.shortuuid("gstt_")
        if seconds < self._min_audio_seconds:
            return self._event(request_id, "", lang, seconds)
        if self.hand_off_audio:
            handle = AUDIO_MARK + request_id
            self._parked[handle] = (wav, seconds)
            if len(self._parked) > 8:                       # never let an abandoned turn pile up audio
                self._parked.pop(next(iter(self._parked)))
            return self._event(request_id, handle, lang, seconds)

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent"
        payload = {
            "system_instruction": {"parts": [{"text": TRANSCRIBE_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode("ascii")}},
                {"text": f"Transcribe this {seconds:.1f} second clip."},
            ]}],
            "generationConfig": _generation_config(self._model),
        }
        started = time.perf_counter()
        timeout = aiohttp.ClientTimeout(total=conn_options.timeout)
        try:
            async with self._session().post(url, json=payload, timeout=timeout,
                                            headers={"x-goog-api-key": self._api_key,
                                                     "Content-Type": "application/json"}) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    raise APIStatusError(f"Gemini STT HTTP {resp.status}: {body}", status_code=resp.status,
                                         request_id=request_id, body=body)
                data = await resp.json()
        except APIStatusError:
            raise
        except Exception as ex:  # DNS, timeout, reset: let the framework retry per conn_options
            raise APIConnectionError(f"Gemini STT request failed: {ex}") from ex

        text = ""
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                text += part.get("text") or ""
        text = text.strip().strip('"').strip()
        if text.lower() in ("(no speech)", "[no speech]", "no speech", "empty string", '""'):
            text = ""
        log.info("Gemini STT %.1fs audio -> %d chars in %d ms", seconds, len(text),
                 int((time.perf_counter() - started) * 1000))
        return self._event(request_id, text, lang, seconds)

    @staticmethod
    def _event(request_id: str, text: str, lang: str, seconds: float) -> stt.SpeechEvent:
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=request_id,
            alternatives=[stt.SpeechData(language=lang, text=text, start_time=0.0, end_time=seconds,
                                         confidence=1.0 if text else 0.0)],
        )
