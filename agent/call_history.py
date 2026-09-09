"""Task 4.2 — Call History, Audio Player & Inspector.

Turns the artefacts each finished call leaves behind — a pipeline job, a
telephony record, a stereo WAV — into one browsable history:
1. Unified call records merged from every source, newest first.
2. Paginated, filtered, sorted queries (sentiment, agent, outcome, duration, text).
3. Call detail with a time-aligned transcript and per-turn sentiment.
4. Per-channel waveform peaks (left = caller, right = agent) read from the WAV.
5. Byte-range audio reads so the browser player can seek without a full download.
6. CSV export and dashboard aggregates for the Call Desk.
"""

import csv
import io
import json
import logging
import os
import re
import time
import wave
from array import array
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("voice-agent.history")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDINGS_DIR = os.path.join(ROOT_DIR, "recordings")
ARCHIVE_DIR = os.path.join(RECORDINGS_DIR, "archive")
JOBS_FILE = os.path.join(RECORDINGS_DIR, "pipeline_jobs.json")

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200
DEFAULT_WAVEFORM_BUCKETS = 240
MAX_WAVEFORM_BUCKETS = 2000
# Spoken pacing used to place turns on the timeline when the transcript
# carries word counts but no per-turn audio offsets.
WORDS_PER_SECOND = 2.5
MIN_TURN_SECONDS = 0.8

SORT_FIELDS = {"started_at", "duration_seconds", "sentiment_score", "turns", "words"}


@dataclass
class CallRecord:
    call_id: str
    job_id: Optional[str] = None
    room_name: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_seconds: float = 0.0
    direction: str = "inbound"
    from_number: str = ""
    to_number: str = ""
    agent_name: str = "AI Agent"
    sentiment: str = "unknown"
    sentiment_score: float = 0.0
    frustration_detected: bool = False
    outcome: str = "unknown"
    summary: str = ""
    key_intent: str = ""
    turns: int = 0
    words: int = 0
    caller_talk_ratio: float = 0.0
    agent_talk_ratio: float = 0.0
    transferred: bool = False
    has_audio: bool = False
    audio_path: Optional[str] = None
    audio_bytes: int = 0
    archive_url: Optional[str] = None
    extracted_fields: Dict[str, Any] = field(default_factory=dict)
    status: str = "completed"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # The absolute path is a server detail; the browser only needs the flag.
        data.pop("audio_path", None)
        return data


class CallHistoryStore:
    """Reads the pipeline's own output and presents it as a call log."""

    def __init__(self, jobs_file: str = JOBS_FILE, recordings_dir: str = RECORDINGS_DIR):
        self.jobs_file = jobs_file
        self.recordings_dir = recordings_dir
        self._cache: Dict[str, CallRecord] = {}
        self._raw_jobs: Dict[str, Dict[str, Any]] = {}
        self._loaded_at = 0.0

    # ── Loading ──────────────────────────────────────────────────────────────
    def _audio_index(self) -> Dict[str, str]:
        """Maps call_id → newest WAV on disk, matching the recorder's naming."""
        index: Dict[str, Tuple[float, str]] = {}
        if not os.path.isdir(self.recordings_dir):
            return {}
        for name in os.listdir(self.recordings_dir):
            if not name.endswith(".wav"):
                continue
            path = os.path.join(self.recordings_dir, name)
            # rec-<call_id>-<timestamp>.wav, with the call_id itself often
            # ending in a timestamp, so strip only the trailing suffix.
            stem = re.sub(r"^rec-", "", name[:-4])
            candidates = {stem, re.sub(r"-\d{10,}$", "", stem)}
            mtime = os.path.getmtime(path)
            for call_id in candidates:
                if call_id and (call_id not in index or index[call_id][0] < mtime):
                    index[call_id] = (mtime, path)
        return {cid: path for cid, (_, path) in index.items()}

    def load(self, force: bool = False) -> List[CallRecord]:
        """Rebuilds the record set from disk (cheap enough to do per request)."""
        if not force and self._cache and time.time() - self._loaded_at < 1.0:
            return list(self._cache.values())

        jobs: List[Dict[str, Any]] = []
        if os.path.isfile(self.jobs_file):
            try:
                with open(self.jobs_file, "r", encoding="utf-8") as f:
                    jobs = json.load(f).get("jobs", [])
            except Exception as e:
                log.warning("Failed to read pipeline jobs: %s", e)

        audio = self._audio_index()
        telephony = self._telephony_index()

        records: Dict[str, CallRecord] = {}
        raw: Dict[str, Dict[str, Any]] = {}
        for job in jobs:
            record = self._record_from_job(job, audio, telephony)
            records[record.call_id] = record
            raw[record.call_id] = job

        self._cache = dict(sorted(records.items(), key=lambda kv: kv[1].started_at, reverse=True))
        self._raw_jobs = raw
        self._loaded_at = time.time()
        return list(self._cache.values())

    def _telephony_index(self) -> Dict[str, Dict[str, Any]]:
        """Phone numbers and direction, when the call arrived over SIP."""
        try:
            from agent.telephony_manager import telephony_manager
            return {c["call_id"]: c for c in telephony_manager.list_calls()}
        except Exception:
            return {}

    def _record_from_job(self, job: Dict[str, Any], audio: Dict[str, str],
                         telephony: Dict[str, Dict[str, Any]]) -> CallRecord:
        call_id = job.get("call_id", job.get("job_id", "unknown"))
        metadata = job.get("metadata") or {}
        metrics = job.get("metrics") or {}
        sentiment = metadata.get("sentiment") or {}
        summary = metadata.get("summary") or {}
        tel = telephony.get(call_id, {})

        audio_path = audio.get(call_id)
        started = job.get("created_at", 0.0) or 0.0
        duration = float(metrics.get("call_duration_seconds") or tel.get("duration_seconds") or 0.0)

        outcome = summary.get("resolution_status", "unknown")
        transferred = outcome == "escalated" or bool(sentiment.get("frustration_detected")) and outcome == "escalated"

        return CallRecord(
            call_id=call_id,
            job_id=job.get("job_id"),
            room_name=job.get("room_name", ""),
            started_at=started,
            ended_at=job.get("completed_at") or started + duration,
            duration_seconds=round(duration, 2),
            direction=tel.get("direction", "inbound"),
            from_number=tel.get("from_number", ""),
            to_number=tel.get("to_number", ""),
            agent_name=metadata.get("agent_name", "AI Agent"),
            sentiment=sentiment.get("overall_polarity", "unknown"),
            sentiment_score=round(float(sentiment.get("overall_score", 0.0)), 4),
            frustration_detected=bool(sentiment.get("frustration_detected", False)),
            outcome=outcome,
            summary=summary.get("executive_summary", ""),
            key_intent=summary.get("key_intent", ""),
            turns=int(metrics.get("total_turns", len(job.get("transcript_turns", [])))),
            words=int(metrics.get("total_words", 0)),
            caller_talk_ratio=float(metrics.get("caller_talk_ratio", 0.0)),
            agent_talk_ratio=float(metrics.get("agent_talk_ratio", 0.0)),
            transferred=transferred,
            has_audio=bool(audio_path),
            audio_path=audio_path,
            audio_bytes=os.path.getsize(audio_path) if audio_path and os.path.isfile(audio_path) else 0,
            archive_url=job.get("archive_url"),
            extracted_fields=self._first_crm_payload(metadata),
            status=job.get("status", "completed"),
        )

    @staticmethod
    def _first_crm_payload(metadata: Dict[str, Any]) -> Dict[str, Any]:
        payloads = metadata.get("crm_payloads") or {}
        for fields in payloads.values():
            if fields:
                return fields
        return {}

    # ── Queries ──────────────────────────────────────────────────────────────
    def list_calls(
        self,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        sentiment: Optional[str] = None,
        agent: Optional[str] = None,
        outcome: Optional[str] = None,
        direction: Optional[str] = None,
        min_duration: Optional[float] = None,
        max_duration: Optional[float] = None,
        transferred: Optional[bool] = None,
        has_audio: Optional[bool] = None,
        search: Optional[str] = None,
        sort: str = "started_at",
        order: str = "desc",
    ) -> Dict[str, Any]:
        rows = self.load()

        if sentiment:
            rows = [r for r in rows if r.sentiment == sentiment]
        if agent:
            rows = [r for r in rows if r.agent_name == agent]
        if outcome:
            rows = [r for r in rows if r.outcome == outcome]
        if direction:
            rows = [r for r in rows if r.direction == direction]
        if min_duration is not None:
            rows = [r for r in rows if r.duration_seconds >= min_duration]
        if max_duration is not None:
            rows = [r for r in rows if r.duration_seconds <= max_duration]
        if transferred is not None:
            rows = [r for r in rows if r.transferred is transferred]
        if has_audio is not None:
            rows = [r for r in rows if r.has_audio is has_audio]
        if search:
            needle = search.lower()
            rows = [r for r in rows if needle in " ".join([
                r.call_id, r.summary, r.key_intent, r.from_number, r.agent_name]).lower()]

        sort_key = sort if sort in SORT_FIELDS else "started_at"
        rows = sorted(rows, key=lambda r: getattr(r, sort_key), reverse=(order != "asc"))

        page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        total = len(rows)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(int(page), pages))
        start = (page - 1) * page_size
        window = rows[start:start + page_size]

        return {
            "items": [r.to_dict() for r in window],
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": pages,
            "has_next": page < pages,
            "has_prev": page > 1,
            "filters": {
                "sentiments": sorted({r.sentiment for r in self.load()}),
                "agents": sorted({r.agent_name for r in self.load()}),
                "outcomes": sorted({r.outcome for r in self.load()}),
            },
        }

    def get_call(self, call_id: str) -> Optional[Dict[str, Any]]:
        """Full inspector payload: record, timeline, sentiment, extractions."""
        self.load()
        record = self._cache.get(call_id)
        if not record:
            return None
        job = self._raw_jobs.get(call_id, {})
        metadata = job.get("metadata") or {}
        sentiment = metadata.get("sentiment") or {}

        return {
            "call": record.to_dict(),
            "timeline": self.timeline(call_id),
            "summary": metadata.get("summary") or {},
            "sentiment": {k: v for k, v in sentiment.items() if k != "turn_sentiments"},
            "extractions": metadata.get("extractions") or {},
            "crm_payloads": metadata.get("crm_payloads") or {},
            "metrics": job.get("metrics") or {},
            "stages": {
                name: {"status": st.get("status"), "duration_ms": st.get("duration_ms")}
                for name, st in (job.get("stages") or {}).items()
            },
            "audio": {
                "available": record.has_audio,
                "bytes": record.audio_bytes,
                "url": f"/api/calls/audio?call_id={call_id}" if record.has_audio else None,
                "waveform_url": f"/api/calls/waveform?call_id={call_id}" if record.has_audio else None,
            },
        }

    def timeline(self, call_id: str) -> List[Dict[str, Any]]:
        """Places each transcript turn on the audio timeline, in seconds.

        Turns carry a wall-clock timestamp and a word count but no audio
        offsets, so the first turn anchors t=0 and each turn is given the
        duration its own word count implies.
        """
        job = self._raw_jobs.get(call_id) or {}
        turns = job.get("transcript_turns") or []
        if not turns:
            return []

        sentiment_by_turn = {
            t.get("turn_index"): t
            for t in ((job.get("metadata") or {}).get("sentiment") or {}).get("turn_sentiments", [])
        }

        stamps = [t.get("timestamp") for t in turns if isinstance(t.get("timestamp"), (int, float))]
        origin = min(stamps) if stamps else 0.0

        timeline, cursor = [], 0.0
        for index, turn in enumerate(turns):
            words = int(turn.get("word_count") or len(str(turn.get("text", "")).split()))
            spoken = max(words / WORDS_PER_SECOND, MIN_TURN_SECONDS)
            stamp = turn.get("timestamp")
            start = round(stamp - origin, 2) if isinstance(stamp, (int, float)) and origin else 0.0
            # Timestamps written in the same millisecond collapse to 0, so the
            # running cursor keeps turns in order and non-overlapping.
            start = max(start, cursor)
            turn_sentiment = sentiment_by_turn.get(turn.get("turn_index", index + 1), {})
            timeline.append({
                "turn_index": turn.get("turn_index", index + 1),
                "role": turn.get("role", "caller"),
                "speaker": turn.get("speaker", "Customer" if turn.get("role") == "caller" else "AI Agent"),
                "text": turn.get("text", ""),
                "start_s": round(start, 2),
                "end_s": round(start + spoken, 2),
                "duration_s": round(spoken, 2),
                "word_count": words,
                "sentiment": turn_sentiment.get("polarity"),
                "sentiment_score": turn_sentiment.get("score"),
                "is_frustrated": turn_sentiment.get("is_frustrated", False),
            })
            cursor = start + spoken
        return timeline

    # ── Audio ────────────────────────────────────────────────────────────────
    def audio_path(self, call_id: str) -> Optional[str]:
        self.load()
        record = self._cache.get(call_id)
        return record.audio_path if record and record.has_audio else None

    def read_audio_range(self, call_id: str, start: int = 0, end: Optional[int] = None
                         ) -> Optional[Tuple[bytes, int, int, int]]:
        """Returns (chunk, start, end, total) for a byte range, or None.

        The player seeks by asking for ranges; serving them keeps a long
        recording from being downloaded in full before playback starts.
        """
        path = self.audio_path(call_id)
        if not path or not os.path.isfile(path):
            return None
        total = os.path.getsize(path)
        start = max(0, min(int(start), max(total - 1, 0)))
        end = total - 1 if end is None else min(int(end), total - 1)
        if end < start:
            end = start
        with open(path, "rb") as f:
            f.seek(start)
            chunk = f.read(end - start + 1)
        return chunk, start, end, total

    def waveform(self, call_id: str, buckets: int = DEFAULT_WAVEFORM_BUCKETS) -> Optional[Dict[str, Any]]:
        """Per-channel peak envelope: left = caller, right = agent."""
        path = self.audio_path(call_id)
        if not path or not os.path.isfile(path):
            return None
        buckets = max(10, min(int(buckets), MAX_WAVEFORM_BUCKETS))

        try:
            with wave.open(path, "rb") as wav:
                channels = wav.getnchannels()
                rate = wav.getframerate()
                width = wav.getsampwidth()
                frames = wav.getnframes()
                raw = wav.readframes(frames)
        except Exception as e:
            log.warning("Failed to read waveform for %s: %s", call_id, e)
            return None

        if width != 2 or frames == 0:
            return None

        samples = array("h")
        samples.frombytes(raw[: frames * channels * width])
        per_bucket = max(1, frames // buckets)

        peaks: List[List[float]] = [[] for _ in range(channels)]
        for bucket in range(min(buckets, frames)):
            head = bucket * per_bucket
            tail = min(head + per_bucket, frames)
            for ch in range(channels):
                window = samples[head * channels + ch: tail * channels: channels]
                peak = max((abs(s) for s in window), default=0) / 32768.0
                peaks[ch].append(round(peak, 4))

        return {
            "call_id": call_id,
            "channels": channels,
            "sample_rate": rate,
            "duration_seconds": round(frames / rate, 2) if rate else 0.0,
            "buckets": len(peaks[0]) if peaks else 0,
            "caller": peaks[0] if peaks else [],
            "agent": peaks[1] if channels > 1 else [],
            "peak_level": round(max((max(ch, default=0.0) for ch in peaks), default=0.0), 4),
        }

    # ── Aggregates & export ──────────────────────────────────────────────────
    def stats(self) -> Dict[str, Any]:
        rows = self.load()
        if not rows:
            return {"calls": 0, "with_audio": 0, "avg_duration_seconds": 0.0,
                    "sentiment": {}, "outcomes": {}, "agents": {},
                    "resolution_rate": 0.0, "frustrated_calls": 0, "total_turns": 0}

        sentiment_counts: Dict[str, int] = {}
        outcome_counts: Dict[str, int] = {}
        agent_counts: Dict[str, int] = {}
        for r in rows:
            sentiment_counts[r.sentiment] = sentiment_counts.get(r.sentiment, 0) + 1
            outcome_counts[r.outcome] = outcome_counts.get(r.outcome, 0) + 1
            agent_counts[r.agent_name] = agent_counts.get(r.agent_name, 0) + 1

        resolved = sum(1 for r in rows if r.outcome == "resolved")
        return {
            "calls": len(rows),
            "with_audio": sum(1 for r in rows if r.has_audio),
            "avg_duration_seconds": round(sum(r.duration_seconds for r in rows) / len(rows), 2),
            "total_turns": sum(r.turns for r in rows),
            "total_words": sum(r.words for r in rows),
            "sentiment": sentiment_counts,
            "outcomes": outcome_counts,
            "agents": agent_counts,
            "resolution_rate": round(resolved / len(rows), 4),
            "frustrated_calls": sum(1 for r in rows if r.frustration_detected),
            "transferred_calls": sum(1 for r in rows if r.transferred),
            "newest_call_at": max(r.started_at for r in rows),
        }

    def export_csv(self, **filters) -> str:
        """Flat CSV of the filtered log, for spreadsheets and BI imports."""
        filters.setdefault("page_size", MAX_PAGE_SIZE)
        rows = self.list_calls(**filters)["items"]
        columns = ["call_id", "started_at", "duration_seconds", "direction", "from_number",
                   "agent_name", "sentiment", "sentiment_score", "outcome", "turns",
                   "words", "transferred", "has_audio", "key_intent", "summary"]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
        return buffer.getvalue()


# Module-level singleton used by the REST API and acceptance tests.
call_history = CallHistoryStore()
