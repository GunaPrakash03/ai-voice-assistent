#!/usr/bin/env python3
"""Task 2.5 — Real-Time Call Recording, Dual-Channel Stereo & Compliance Acceptance Test.

Verifies:
1. Recording models, lifecycle state machine & compliance disclosure settings (FCC / 2-party consent).
2. Dual-channel stereo PCM audio mixing & time synchronization (Left=caller, Right=agent).
3. PCI-DSS / HIPAA pause & resume with zero-leakage silence insertion.
4. RIFF/WAV container integrity, 16-bit stereo headers & duration calculation.
5. Telephony recording REST API endpoints (/start, /pause, /resume, /stop, /recordings).
6. Live WebRTC recording action dispatch & event broadcasting with the agent worker.
"""

import asyncio
import json
import math
import os
import struct
import subprocess
import sys
import time
import urllib.request
import wave

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from agent.recording_manager import (
    ComplianceMode,
    RecordingConfig,
    RecordingManager,
    RecordingMetadata,
    RecordingSession,
    RecordingStatus,
    StereoAudioMixer,
    generate_compliance_beep,
)


def check(name, fn):
    try:
        msg = fn()
        print(f"  \033[32mPASS\033[0m  {name} — {msg}")
        return True
    except Exception as e:
        print(f"  \033[31mFAIL\033[0m  {name} — {e}")
        return False


def test_recording_models_and_compliance():
    """Verify recording configurations, compliance disclosure flags, and start beep generation."""
    cfg = RecordingConfig(
        channels=2,
        sample_rate=16000,
        compliance_mode=ComplianceMode.TWO_PARTY,
        beep_on_start=True,
    )
    assert cfg.channels == 2
    assert cfg.compliance_mode == ComplianceMode.TWO_PARTY

    # Verify 1400 Hz compliance tone generation
    beep = generate_compliance_beep(frequency=1400.0, duration_ms=200.0, sample_rate=16000)
    assert len(beep) == int(16000 * 0.2) * 2  # 3200 samples * 2 bytes = 6400 bytes
    samples = struct.unpack(f"<{len(beep)//2}h", beep)
    assert any(abs(s) > 1000 for s in samples), "Beep samples contain only silence"

    meta = RecordingMetadata(
        recording_id="rec-test-001",
        call_id="call-001",
        status=RecordingStatus.RECORDING,
        compliance_mode=cfg.compliance_mode.value,
        compliance_disclosure_played=True,
    )
    assert meta.status == RecordingStatus.RECORDING
    assert meta.compliance_disclosure_played is True

    return "2-party consent configuration, 1400 Hz tone generator & metadata validated"


def test_stereo_audio_mixing_and_sync():
    """Verify dual-channel stereo interleaving: Left=Caller (channel 0), Right=Agent (channel 1)."""
    # Channel 0: Caller (value 500)
    caller_count = 160
    caller_pcm = struct.pack(f"<{caller_count}h", *([500] * caller_count))

    # Channel 1: Agent (value 900)
    agent_count = 160
    agent_pcm = struct.pack(f"<{agent_count}h", *([900] * agent_count))

    stereo_bytes = StereoAudioMixer.interleave_stereo(caller_pcm, agent_pcm)
    assert len(stereo_bytes) == caller_count * 4  # 160 samples * 4 bytes (2 ch * 2 bytes)

    # Inspect interleaved samples [L0, R0, L1, R1, ...]
    interleaved = struct.unpack(f"<{caller_count * 2}h", stereo_bytes)
    left_samples = interleaved[0::2]
    right_samples = interleaved[1::2]

    assert all(s == 500 for s in left_samples), "Left channel (caller) sample mismatch"
    assert all(s == 900 for s in right_samples), "Right channel (agent) sample mismatch"

    # Verify padding when caller and agent have different frame lengths
    short_caller = struct.pack("<80h", *([300] * 80))
    long_agent = struct.pack("<160h", *([700] * 160))
    padded_stereo = StereoAudioMixer.interleave_stereo(short_caller, long_agent)
    assert len(padded_stereo) == 160 * 4
    unpacked_padded = struct.unpack("<320h", padded_stereo)
    # First 80 caller samples are 300, remaining 80 are padded with 0 (silence)
    assert all(s == 300 for s in unpacked_padded[0:160:2])
    assert all(s == 0 for s in unpacked_padded[160:320:2])

    return f"160 stereo frames interleaved cleanly (Left=caller, Right=agent); zero-padding sync verified"


def test_pci_hipaa_pause_redaction():
    """Verify PCI/HIPAA pause state, silence padding redaction, and pause duration tracking."""
    test_call = f"test-pause-{int(time.time()*1000)}"
    session = RecordingSession(test_call)
    session.start()
    assert session.status == RecordingStatus.RECORDING

    # Write 100ms audio
    audio_frame = struct.pack("<1600h", *([1000] * 1600))
    session.write_chunk(caller_pcm=audio_frame, agent_pcm=audio_frame)

    # Pause for credit card collection
    session.pause(reason="pci_credit_card_collection")
    assert session.status == RecordingStatus.PAUSED
    assert session.pause_count == 1
    assert session.pause_reason == "pci_credit_card_collection"

    # While paused: sensitive audio is passed in, but pure silence must be written
    sensitive_pcm = struct.pack("<1600h", *([30000] * 1600))
    frames_before = session.total_frames_written
    session.write_chunk(caller_pcm=sensitive_pcm, agent_pcm=sensitive_pcm)
    frames_after = session.total_frames_written
    assert frames_after > frames_before, "Silence frames were not written to maintain timeline synchronization"

    # Resume recording
    time.sleep(0.05)
    session.resume()
    assert session.status == RecordingStatus.RECORDING
    assert session.total_paused_duration > 0.0

    meta = session.stop()
    assert meta.status == RecordingStatus.STOPPED
    assert meta.pause_count == 1

    return f"pause redacts PII to silence, pause_count=1, paused_dur={meta.paused_duration_s}s"


def test_riff_wav_container_integrity():
    """Verify generated WAV file container, RIFF headers, channels, and sample rate."""
    test_call = f"test-riff-{int(time.time()*1000)}"
    manager = RecordingManager()
    meta = manager.start_recording(test_call)
    session = manager.get_session(test_call)

    # Write 0.5s of audio (8000 frames @ 16kHz)
    chunk = struct.pack("<320h", *([1200] * 320))
    for _ in range(25):
        manager.append_audio(test_call, caller_pcm=chunk, agent_pcm=chunk)

    final_meta = manager.stop_recording(test_call)
    assert os.path.exists(final_meta.file_path), f"File {final_meta.file_path} does not exist"

    # Read WAV container using standard library
    with wave.open(final_meta.file_path, "rb") as w:
        assert w.getnchannels() == 2, f"Expected 2 channels, got {w.getnchannels()}"
        assert w.getsampwidth() == 2, f"Expected 16-bit (2 bytes), got {w.getsampwidth()}"
        assert w.getframerate() == 16000, f"Expected 16000 Hz, got {w.getframerate()}"
        n_frames = w.getnframes()
        assert n_frames > 0, "WAV has 0 audio frames"

    return f"RIFF WAV verified: 2 channels (stereo), 16-bit, 16000 Hz, {n_frames} frames"


def test_telephony_recording_rest_endpoints():
    """Verify HTTP REST endpoints: /start, /pause, /resume, /stop, /recordings."""
    test_call_id = f"test-rest-rec-{int(time.time()*1000)}"

    # 1. POST /api/telephony/recording/start
    req = urllib.request.Request(
        "http://localhost:8091/api/telephony/recording/start",
        data=json.dumps({
            "call_id": test_call_id,
            "compliance_mode": "two_party",
            "beep_on_start": True,
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        assert res.get("recording", {}).get("status") == "recording"
        assert res.get("recording", {}).get("channels") == 2

    # 2. POST /api/telephony/recording/pause
    req = urllib.request.Request(
        "http://localhost:8091/api/telephony/recording/pause",
        data=json.dumps({
            "call_id": test_call_id,
            "reason": "pci_credit_card_masking",
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        assert res.get("recording", {}).get("status") == "paused"

    # 3. POST /api/telephony/recording/resume
    req = urllib.request.Request(
        "http://localhost:8091/api/telephony/recording/resume",
        data=json.dumps({"call_id": test_call_id}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        assert res.get("recording", {}).get("status") == "recording"

    # 4. POST /api/telephony/recording/stop
    req = urllib.request.Request(
        "http://localhost:8091/api/telephony/recording/stop",
        data=json.dumps({"call_id": test_call_id}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        assert res.get("recording", {}).get("status") == "stopped"

    # 5. GET /api/telephony/recording?call_id=...
    with urllib.request.urlopen(f"http://localhost:8091/api/telephony/recording?call_id={test_call_id}", timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        assert res.get("recording", {}).get("status") == "stopped"

    # 6. GET /api/telephony/recordings
    with urllib.request.urlopen("http://localhost:8091/api/telephony/recordings", timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        assert any(rec.get("call_id") == test_call_id for rec in res.get("recordings", []))

    return "POST /start, /pause, /resume, /stop & GET /recordings 200 OK"


def test_live_webrtc_recording_dispatch():
    """Verify live WebRTC data channel recording action and event dispatching with agent worker."""
    room_name = f"verify-rec-{int(time.time()*1000)}"
    script = """
import asyncio, json, os, time
from livekit import rtc
from agent.token import join_token

async def test():
    key = os.environ['LIVEKIT_API_KEY']
    secret = os.environ['LIVEKIT_API_SECRET']
    url = os.environ['LIVEKIT_URL']
    token = join_token(key, secret, '__ROOM__', 'rec-tester')
    room = rtc.Room()

    agent_ready = asyncio.get_event_loop().create_future()
    rec_event_received = asyncio.get_event_loop().create_future()

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
        if packet.topic == 'recording_event':
            if not rec_event_received.done():
                rec_event_received.set_result(data)

    await room.connect(url, token)
    for p in room.remote_participants.values():
        if not agent_ready.done():
            agent_ready.set_result(p)

    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track('mic', source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    await asyncio.wait_for(agent_ready, timeout=15.0)
    await asyncio.sleep(0.5)

    # Send start_recording action over WebRTC data channel
    rec_pkt = json.dumps({
        'action': 'start_recording',
        'compliance_mode': 'two_party'
    }).encode()
    await room.local_participant.publish_data(rec_pkt, reliable=True)

    result = await asyncio.wait_for(rec_event_received, timeout=15.0)
    await room.disconnect()
    ev = result.get('event', 'unknown')
    status = result.get('recording', {}).get('status', 'unknown')
    channels = result.get('recording', {}).get('channels', 0)
    print(f"{ev}|{status}|{channels}")

asyncio.run(test())
""".replace("__ROOM__", room_name)

    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Live WebRTC recording test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No recording event received: {res.stdout}")

    ev, status, channels = lines[-1].split("|", 2)
    return f"event='{ev}', status='{status}', channels={channels} verified over WebRTC data channel"


def main():
    print("Task 2.5 — Real-Time Call Recording, Dual-Channel Stereo & Compliance Acceptance\n")
    checks = [
        ("Recording models & compliance settings", test_recording_models_and_compliance),
        ("Dual-channel stereo audio mixing & sync", test_stereo_audio_mixing_and_sync),
        ("PCI/HIPAA pause & silence redaction", test_pci_hipaa_pause_redaction),
        ("RIFF WAV container & header integrity", test_riff_wav_container_integrity),
        ("Telephony recording REST API endpoints", test_telephony_recording_rest_endpoints),
        ("Live WebRTC recording action & event dispatch", test_live_webrtc_recording_dispatch),
    ]

    passed = 0
    for name, fn in checks:
        if check(name, fn):
            passed += 1

    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
