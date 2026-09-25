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
from typing import Set, Any, Dict, List, Optional, Tuple

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
    workspace_id: str = "ws-default"


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
        self._waveform_cache: Dict[Tuple[str, int], Dict[str, Any]] = {}

    def _is_call_transferred(self, call_id: str) -> bool:
        """Checks if a call was transferred according to live transfer manager state."""
        try:
            from agent.transfer_manager import transfer_manager
            for t in transfer_manager.list_transfers():
                if t.get("call_id") == call_id and t.get("status") in ("completed", "bridged", "initiated"):
                    return True
        except Exception:
            pass
        return False

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
        jobs_mtime = os.path.getmtime(self.jobs_file) if os.path.isfile(self.jobs_file) else 0.0
        if not force and self._cache and (time.time() - self._loaded_at < 1.0) and (jobs_mtime <= self._loaded_at):
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
        # Prefer actual call start / end timestamps from metadata or telephony over pipeline enqueue time
        started = float(metadata.get("started_at") or metadata.get("call_started_at") or tel.get("started_at") or job.get("created_at", 0.0) or 0.0)
        duration = float(
            metadata.get("duration_seconds")
            or tel.get("duration_seconds")
            or metrics.get("call_duration_seconds")
            or 0.0
        )
        ended = float(
            metadata.get("ended_at")
            or metadata.get("call_ended_at")
            or tel.get("ended_at")
            or job.get("completed_at")
            or (started + duration if (started and duration) else 0.0)
        )
        if duration == 0.0 and ended > started:
            duration = round(ended - started, 2)

        outcome = summary.get("resolution_status", "unknown")
        # Real transfer signals rather than dead clause on outcome string
        transfer_meta = metadata.get("transfer") or {}
        transferred = bool(
            metadata.get("transferred")
            or metadata.get("transfer_id")
            or (isinstance(transfer_meta, dict) and transfer_meta.get("status") in ("completed", "bridged", "initiated"))
            or tel.get("transferred")
            or self._is_call_transferred(call_id)
            or outcome == "escalated"
        )

        return CallRecord(
            call_id=call_id,
            job_id=job.get("job_id"),
            room_name=job.get("room_name", ""),
            started_at=started,
            ended_at=ended,
            duration_seconds=round(duration, 2),
            # Sandbox test calls carry their own direction/numbers in metadata (no SIP leg).
            direction=tel.get("direction") or metadata.get("direction", "inbound"),
            from_number=tel.get("from_number") or metadata.get("from_number", ""),
            to_number=tel.get("to_number") or metadata.get("to_number", ""),
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
            archive_url=job.get("archive_url") or (f"s3://voice-archive/recordings/{os.path.basename(audio_path)}" if audio_path else None),
            extracted_fields=self._first_crm_payload(metadata),
            status=job.get("status", "completed"),
            workspace_id=str(metadata.get("workspace_id") or job.get("workspace_id") or tel.get("workspace_id") or "ws-default"),
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
        visible_agents: Optional[Set[str]] = None,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        rows = self._visible_rows(visible_agents)

        if workspace_id:
            rows = [r for r in rows if r.workspace_id == workspace_id]


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
                "sentiments": sorted({r.sentiment for r in self._visible_rows(visible_agents)}),
                "agents": sorted({r.agent_name for r in self._visible_rows(visible_agents)}),
                "outcomes": sorted({r.outcome for r in self._visible_rows(visible_agents)}),
            },
        }

    def _visible_rows(self, visible_agents: Optional[Set[str]]) -> List[CallRecord]:
        """Members only see calls handled by their own agents; None means no restriction."""
        rows = self.load()
        if visible_agents is None:
            return rows
        return [r for r in rows if r.agent_name in visible_agents]

    def can_view(self, call_id: str, visible_agents: Optional[Set[str]]) -> bool:
        if visible_agents is None:
            return True
        self.load()
        rec = self._cache.get(call_id)
        return bool(rec and rec.agent_name in visible_agents)

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

        Turns carry a wall-clock timestamp (and, from the sandbox, an end timestamp) and a
        word count. The call's own start (metadata.started_at, which is also where the
        recording begins) anchors t=0; without it the first turn does. A turn with an end
        timestamp keeps its real length, otherwise its word count implies one.
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
        call_start = (job.get("metadata") or {}).get("started_at")
        origin = float(call_start) if isinstance(call_start, (int, float)) and call_start else (min(stamps) if stamps else 0.0)

        timeline, cursor = [], 0.0
        last_raw_stamp: Optional[float] = None
        last_interval: Optional[float] = None
        for index, turn in enumerate(turns):
            words = int(turn.get("word_count") or len(str(turn.get("text", "")).split()))
            spoken = max(words / WORDS_PER_SECOND, MIN_TURN_SECONDS)
            stamp = turn.get("timestamp")
            end_stamp = turn.get("end_timestamp")
            has_explicit_end = False
            if isinstance(stamp, (int, float)) and isinstance(end_stamp, (int, float)) and end_stamp > stamp:
                spoken = max(round(end_stamp - stamp, 2), 0.3)
                has_explicit_end = True

            has_real_stamp = isinstance(stamp, (int, float)) and stamp > 0
            if has_real_stamp and origin:
                calc_start = max(round(stamp - origin, 2), 0.0)
                # Only apply the running cursor to same-timestamp collisions, avoiding highlight drift
                if last_raw_stamp is not None and abs(stamp - last_raw_stamp) < 0.001:
                    start = max(calc_start, cursor)
                else:
                    start = calc_start
                last_raw_stamp = stamp
            else:
                start = max(0.0, cursor)
                last_raw_stamp = None

            # Prevent turn overlap when next turn has a known subsequent start timestamp
            if not has_explicit_end:
                if index + 1 < len(turns):
                    next_turn = turns[index + 1]
                    next_stamp = next_turn.get("timestamp")
                    if isinstance(next_stamp, (int, float)) and origin and next_stamp > (stamp or 0):
                        next_start = max(round(next_stamp - origin, 2), 0.0)
                        if next_start > start:
                            last_interval = round(next_start - start, 2)
                            spoken = max(0.3, min(spoken, last_interval))
                elif last_interval is not None:
                    spoken = max(0.3, min(spoken, last_interval))

            turn_sentiment = sentiment_by_turn.get(turn.get("turn_index", index + 1), {})
            timeline.append({
                "turn_index": turn.get("turn_index", index + 1),
                "role": str(turn.get("role", "caller")),
                "speaker": str(turn.get("speaker", "Customer" if turn.get("role") == "caller" else "AI Agent")),
                "text": str(turn.get("text", "")),
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
        buckets = max(10, min(int(buckets), MAX_WAVEFORM_BUCKETS))
        cache_key = (call_id, buckets)
        if cache_key in self._waveform_cache:
            return self._waveform_cache[cache_key]

        path = self.audio_path(call_id)
        if not path or not os.path.isfile(path):
            return None

        try:
            with wave.open(path, "rb") as wav:
                channels = wav.getnchannels()
                rate = wav.getframerate()
                width = wav.getsampwidth()
                frames = wav.getnframes()
                if width != 2 or frames == 0:
                    return None

                per_bucket = max(1, frames // buckets)
                peaks: List[List[float]] = [[] for _ in range(channels)]

                # Read chunk by chunk per bucket to avoid loading whole WAV file into memory
                for _ in range(min(buckets, frames)):
                    raw_chunk = wav.readframes(per_bucket)
                    if not raw_chunk:
                        break
                    chunk_samples = array("h")
                    chunk_samples.frombytes(raw_chunk)
                    sample_count = len(chunk_samples)
                    for ch in range(channels):
                        ch_slice = chunk_samples[ch:sample_count:channels]
                        peak = max((abs(s) for s in ch_slice), default=0) / 32768.0
                        peaks[ch].append(round(peak, 4))

        except Exception as e:
            log.warning("Failed to read waveform for %s: %s", call_id, e)
            return None

        result = {
            "call_id": call_id,
            "channels": channels,
            "sample_rate": rate,
            "duration_seconds": round(frames / rate, 2) if rate else 0.0,
            "buckets": len(peaks[0]) if peaks else 0,
            "caller": peaks[0] if peaks else [],
            "agent": peaks[1] if channels > 1 else [],
            "peak_level": round(max((max(ch, default=0.0) for ch in peaks), default=0.0), 4),
        }

        if len(self._waveform_cache) > 200:
            self._waveform_cache.pop(next(iter(self._waveform_cache)))
        self._waveform_cache[cache_key] = result
        return result

    # ── Aggregates & export ──────────────────────────────────────────────────
    def stats(self, visible_agents: Optional[Set[str]] = None) -> Dict[str, Any]:
        rows = self._visible_rows(visible_agents)
        if not rows:
            return {
                "calls": 0,
                "with_audio": 0,
                "avg_duration_seconds": 0.0,
                "sentiment": {},
                "outcomes": {},
                "agents": {},
                "resolution_rate": 0.0,
                "frustrated_calls": 0,
                "total_turns": 0,
                "total_words": 0,
                "transferred_calls": 0,
                "newest_call_at": None,
            }

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
        # Un-capped CSV export: iterate all pages of filtered results
        filters_copy = dict(filters)
        filters_copy["page_size"] = MAX_PAGE_SIZE
        filters_copy["page"] = 1
        first_page = self.list_calls(**filters_copy)
        rows = list(first_page.get("items", []))
        total_pages = first_page.get("pages", 1)
        for p in range(2, total_pages + 1):
            filters_copy["page"] = p
            rows.extend(self.list_calls(**filters_copy).get("items", []))

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
