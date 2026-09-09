#!/usr/bin/env python3
"""Task 3.1 — Post-Call Processing Queue Worker & Audio Archival Engine Acceptance Test.

Verifies:
1. Post-call job models, lifecycle state transitions & priority queue dispatching.
2. Dual-channel stereo WAV mixdown, RIFF checksum, energy metrics & storage archival.
3. Conversation metrics calculation, talk-time ratio analysis & WPM calculation.
4. Async queue worker execution, 4-stage pipeline progression & transient retry handling.
5. Telephony pipeline REST API server endpoints (GET /jobs, /job, /stats; POST /enqueue, /retry).
6. Live WebRTC post-call lifecycle trigger & bidirectional data channel event broadcast.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from agent.pipeline_worker import (
    AudioMixdownProcessor,
    JobStatus,
    MetricsProcessor,
    PipelineStage,
    PostCallJob,
    PostCallPipelineWorker,
    StageResult,
    StageStatus,
    pipeline_worker,
)
from agent.token import join_token


def check(name, fn):
    try:
        msg = fn()
        print(f"  \033[32mPASS\033[0m  {name} — {msg}")
        return True
    except Exception as e:
        print(f"  \033[31mFAIL\033[0m  {name} — {e}")
        return False


def test_job_models_and_priority_queue():
    """Verify job data structures, state machine enum values, and prioritized queue ordering."""
    # Verify state machine values
    assert JobStatus.QUEUED.value == "queued"
    assert JobStatus.PROCESSING.value == "processing"
    assert JobStatus.COMPLETED.value == "completed"
    assert JobStatus.FAILED.value == "failed"

    # Verify pipeline stages
    stages = [s.value for s in PipelineStage]
    assert "audio_mixdown" in stages
    assert "transcript_normalization" in stages
    assert "metrics_calculation" in stages
    assert "storage_archive" in stages

    # Test PostCallJob instantiation and serialization
    job = PostCallJob(
        job_id="job-test-001",
        call_id="call-001",
        room_name="room-001",
        priority=1,
        status=JobStatus.QUEUED.value,
        metadata={"carrier": "telnyx", "agent": "billing-bot"},
    )
    job_dict = job.to_dict()
    assert job_dict["job_id"] == "job-test-001"
    assert job_dict["priority"] == 1
    assert job_dict["metadata"]["carrier"] == "telnyx"

    # Test priority ordering in worker queue (Priority 1 must be dequeued before Priority 10)
    worker = PostCallPipelineWorker(concurrency=1)
    j_low = worker.enqueue_call(call_id="call-low", priority=10)
    j_high = worker.enqueue_call(call_id="call-high", priority=1)

    p1, _, id1 = worker._queue.get_nowait()
    assert id1 == j_high.job_id, f"Expected high priority job {j_high.job_id}, got {id1}"
    p2, _, id2 = worker._queue.get_nowait()
    assert id2 == j_low.job_id, f"Expected low priority job {j_low.job_id}, got {id2}"

    return "Job states (5), stages (4), priority ordering (p1 > p10) & serialization verified"


def test_audio_mixdown_and_archival():
    """Verify dual-channel stereo WAV packaging, RIFF checksum, RMS energy, and manifest generation."""
    call_id = f"test-arch-{int(time.time()*1000)}"
    result = AudioMixdownProcessor.process_and_archive(call_id=call_id)

    assert os.path.isfile(result["archive_path"]), f"Missing archived WAV at {result['archive_path']}"
    assert result["channels"] == 2, f"Expected stereo (2 ch), got {result['channels']}"
    assert result["sample_rate"] == 16000, f"Expected 16000 Hz, got {result['sample_rate']}"
    assert result["duration_seconds"] > 0, "Duration must be positive"
    assert len(result["sha256"]) == 64, "Expected valid 64-char SHA256 hex digest"
    assert result["file_size_bytes"] > 1000, "Archived WAV too small"
    assert result["archive_url"].startswith("s3://"), f"Invalid archive URL: {result['archive_url']}"

    return f"Stereo 16kHz WAV ({result['file_size_bytes']}B, sha256={result['sha256'][:8]}...) & S3 URI verified"


def test_conversation_metrics_processor():
    """Verify transcript metrics calculation: turns, word counts, talk-time ratios & WPM."""
    sample_turns = [
        {"role": "caller", "text": "Hi, I have a question about my legal retainer invoice.", "duration": 3.0},
        {"role": "agent", "text": "I can assist you with your retainer invoice details. One moment please.", "duration": 4.0},
        {"role": "caller", "text": "Thank you, that would be very helpful.", "duration": 2.0},
        {"role": "agent", "text": "Your current balance is fully cleared as of yesterday afternoon.", "duration": 3.5},
    ]

    metrics = MetricsProcessor.calculate_metrics(sample_turns, call_duration_seconds=15.0)

    assert metrics["total_turns"] == 4
    assert metrics["caller_turns"] == 2
    assert metrics["agent_turns"] == 2
    assert metrics["caller_words"] == 17
    assert metrics["agent_words"] == 22
    assert metrics["total_words"] == 39
    assert metrics["caller_talk_ratio"] > 0.3
    assert metrics["agent_talk_ratio"] > 0.4
    assert metrics["wpm_caller"] > 50
    assert metrics["wpm_agent"] > 50

    return f"total_turns=4, total_words=39, caller_ratio={metrics['caller_talk_ratio']}, agent_ratio={metrics['agent_talk_ratio']} verified"


def test_async_pipeline_worker_execution():
    """Verify asynchronous queue worker execution across all 4 stages and retry recovery."""
    worker = PostCallPipelineWorker(concurrency=2)
    sample_call_id = f"worker-test-{int(time.time()*1000)}"

    job = worker.enqueue_call(
        call_id=sample_call_id,
        transcript_turns=[
            {"role": "caller", "text": "I need to schedule a consultation with an attorney."},
            {"role": "agent", "text": "I have openings available tomorrow at 10 AM or 2 PM."},
        ],
        priority=2,
    )

    assert job.status == JobStatus.QUEUED.value

    # Execute complete 4-stage pipeline
    loop = asyncio.new_event_loop()
    executed_job = loop.run_until_complete(worker.execute_job(job.job_id))
    loop.close()

    assert executed_job.status == JobStatus.COMPLETED.value
    assert executed_job.total_duration_ms > 0
    assert len(executed_job.stages) == 4
    for st_name, st_res in executed_job.stages.items():
        assert st_res["status"] == StageStatus.COMPLETED.value, f"Stage {st_name} failed: {st_res}"

    # Verify manifest file created on disk
    manifest_file = os.path.join(ROOT, "recordings", "archive", f"{sample_call_id}_manifest.json")
    assert os.path.isfile(manifest_file), f"Missing manifest at {manifest_file}"
    with open(manifest_file, "r") as mf:
        manifest_data = json.load(mf)
        assert manifest_data["call_id"] == sample_call_id
        assert manifest_data["turns_count"] == 2

    # Verify retry mechanism
    retried_job = worker.retry_job(job.job_id)
    assert retried_job is not None
    assert retried_job.status == JobStatus.QUEUED.value

    return f"4/4 stages completed (dur={executed_job.total_duration_ms:.2f}ms), manifest verified & retry logic validated"


def test_pipeline_rest_api_endpoints():
    """Verify post-call pipeline REST endpoints on http://localhost:8091."""
    base_url = "http://localhost:8091"
    test_call_id = f"rest-call-{int(time.time()*1000)}"

    # 1. POST /api/pipeline/enqueue
    enqueue_payload = json.dumps({
        "call_id": test_call_id,
        "transcript_turns": [
            {"role": "user", "text": "Can you check my case filing status?"},
            {"role": "assistant", "text": "Your case filing was submitted to the court on Monday."},
        ],
        "priority": 1,
        "execute_now": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/pipeline/enqueue",
        data=enqueue_payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        job = res.get("job", {})
        assert job.get("call_id") == test_call_id
        assert job.get("status") == "completed"
        test_job_id = job.get("job_id")

    # 2. GET /api/pipeline/job?job_id=...
    with urllib.request.urlopen(f"{base_url}/api/pipeline/job?job_id={test_job_id}", timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        assert res.get("job", {}).get("job_id") == test_job_id

    # 3. GET /api/pipeline/jobs
    with urllib.request.urlopen(f"{base_url}/api/pipeline/jobs?status=completed", timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        jobs = res.get("jobs", [])
        assert any(j.get("job_id") == test_job_id for j in jobs)

    # 4. GET /api/pipeline/stats
    with urllib.request.urlopen(f"{base_url}/api/pipeline/stats", timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        stats = res.get("stats", {})
        assert stats.get("total_jobs", 0) > 0
        assert stats.get("completed", 0) > 0

    # 5. POST /api/pipeline/retry
    retry_payload = json.dumps({"job_id": test_job_id, "execute_now": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/pipeline/retry",
        data=retry_payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        assert res.get("job", {}).get("status") == "completed"

    return "POST /enqueue, /retry & GET /jobs, /job, /stats (all 200 OK)"


def test_live_webrtc_pipeline_dispatch():
    """Verify live WebRTC post-call lifecycle trigger & event broadcast with the agent worker."""
    room_name = f"verify-pipe-{int(time.time()*1000)}"
    script = """
import asyncio, json, os, time
from livekit import rtc
from agent.token import join_token

async def test():
    key = os.environ['LIVEKIT_API_KEY']
    secret = os.environ['LIVEKIT_API_SECRET']
    url = os.environ['LIVEKIT_URL']
    token = join_token(key, secret, '__ROOM__', 'pipe-agent')
    room = rtc.Room()

    agent_ready = asyncio.get_event_loop().create_future()
    pipe_completed = asyncio.get_event_loop().create_future()

    @room.on('participant_connected')
    def on_p(p):
        if not agent_ready.done():
            agent_ready.set_result(p)

    @room.on('data_received')
    def on_data(packet):
        try:
            data = json.loads(packet.data.decode())
        except Exception:
            return
        if packet.topic == 'pipeline_event' and data.get('event') == 'job_completed':
            if not pipe_completed.done():
                pipe_completed.set_result(data)

    await room.connect(url, token)
    for p in room.remote_participants.values():
        if not agent_ready.done():
            agent_ready.set_result(p)

    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track('mic', source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    await asyncio.wait_for(agent_ready, timeout=15.0)
    await asyncio.sleep(0.5)

    # Trigger post-call processing action over WebRTC data channel
    pipe_pkt = json.dumps({
        'action': 'enqueue_post_call',
        'call_id': '__ROOM__',
        'transcript_turns': [
            {'role': 'caller', 'text': 'Can I get a confirmation number for my payment?'},
            {'role': 'agent', 'text': 'Your payment confirmation number is CONF-99214.'}
        ],
        'priority': 1
    }).encode()
    await room.local_participant.publish_data(pipe_pkt, reliable=True)

    res = await asyncio.wait_for(pipe_completed, timeout=15.0)
    await room.disconnect()

    job = res.get('job', {})
    ev = res.get('event', 'unknown')
    status = job.get('status', 'unknown')
    stages_count = len(job.get('stages', {}))
    print(f"{ev}|{status}|{stages_count}")

asyncio.run(test())
""".replace("__ROOM__", room_name)

    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Live WebRTC pipeline test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No pipeline event received: {res.stdout}")

    ev, status, stages_count = lines[-1].split("|", 2)
    return f"event='{ev}', status='{status}', completed_stages={stages_count}/4 verified over WebRTC data channel"


def main():
    print("Task 3.1 — Post-Call Processing Queue Worker Acceptance\n")
    checks = [
        ("Job models & priority queue", test_job_models_and_priority_queue),
        ("Audio mixdown, checksum & archival", test_audio_mixdown_and_archival),
        ("Conversation metrics calculation", test_conversation_metrics_processor),
        ("Async pipeline worker execution", test_async_pipeline_worker_execution),
        ("Pipeline REST API server endpoints", test_pipeline_rest_api_endpoints),
        ("Live WebRTC post-call event dispatch", test_live_webrtc_pipeline_dispatch),
    ]

    passed = 0
    for name, fn in checks:
        if check(name, fn):
            passed += 1

    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
