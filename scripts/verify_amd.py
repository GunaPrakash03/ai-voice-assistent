#!/usr/bin/env python3
"""Task 2.4 — Answering Machine Detection (AMD) & Voicemail Drop Acceptance Test.

Verifies:
1. AMD models, thresholds, and temporal cadence classifier (human vs machine greeting cadence).
2. Voicemail beep audio DSP frequency detection (1000 Hz / 800 Hz Goertzel tone filter).
3. Transcript semantic keyword classifier (voicemail phrases vs live human greetings).
4. Automated voicemail drop audio synthesis & post-beep drop lifecycle.
5. Telephony AMD REST API server endpoints (/api/telephony/amd, /api/telephony/voicemail-drop).
6. Live WebRTC AMD event and data channel dispatch with the running agent worker.
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

from agent.amd_manager import (
    AMDAction,
    AMDManager,
    AMDResult,
    AMDSession,
    AMDState,
    SemanticKeywordDetector,
    VoicemailBeepDetector,
    VoicemailDropConfig,
    generate_beep_audio_pcm,
)


def check(name, fn):
    try:
        msg = fn()
        print(f"  \033[32mPASS\033[0m  {name} — {msg}")
        return True
    except Exception as e:
        print(f"  \033[31mFAIL\033[0m  {name} — {e}")
        return False


def test_amd_models_and_cadence_classifier():
    """Verify AMD states, thresholds, and temporal speech/silence cadence classification."""
    config = VoicemailDropConfig(
        max_human_speech_duration_s=2.4,
        min_machine_speech_duration_s=3.2,
        human_silence_threshold_s=0.7,
    )
    manager = AMDManager(default_config=config)

    # 1. Human Greeting Simulation: Short speech (1.2s: 'Hello?') followed by pause
    human_call = f"test-human-{int(time.time()*1000)}"
    t0 = 100.0
    manager.process_vad_event(human_call, is_speech=True, timestamp=t0)
    # Speech ends at 1.2s
    manager.process_vad_event(human_call, is_speech=False, timestamp=t0 + 1.2)
    res_human = manager.get_session(human_call).to_result()
    assert res_human.state == AMDState.HUMAN, f"Expected HUMAN, got {res_human.state}"
    assert res_human.action == AMDAction.CONTINUE_DIALOGUE
    assert res_human.greeting_duration_s == 1.2

    # 2. Machine Greeting Simulation: Continuous uninterrupted speech (>3.5s: 'Hi you have reached...')
    machine_call = f"test-machine-{int(time.time()*1000)}"
    manager.process_vad_event(machine_call, is_speech=True, timestamp=t0)
    # Still speaking at 3.6s
    manager.process_vad_event(machine_call, is_speech=True, timestamp=t0 + 3.6)
    res_machine = manager.get_session(machine_call).to_result()
    assert res_machine.state == AMDState.MACHINE_GREETING, f"Expected MACHINE_GREETING, got {res_machine.state}"
    assert res_machine.confidence >= 0.85

    return f"human cadence (1.2s -> {res_human.state.value}) & machine cadence (3.6s -> {res_machine.state.value}) verified"


def test_voicemail_beep_dsp_detection():
    """Verify Goertzel tone DSP detects 1000 Hz / 800 Hz voicemail prompt beeps without false positives."""
    detector = VoicemailBeepDetector(sample_rate=16000, target_frequencies=[1000.0, 800.0, 440.0])

    # 1. Test 1000 Hz pure sine wave beep (standard North American voicemail beep)
    beep_pcm = generate_beep_audio_pcm(frequency=1000.0, duration_ms=250.0, sample_rate=16000)
    chunk_size = 320 * 2  # 20ms @ 16kHz
    beep_detected = False
    detected_freq = None

    for i in range(0, len(beep_pcm), chunk_size):
        chunk = beep_pcm[i:i + chunk_size]
        detected, freq = detector.process_pcm_chunk(chunk, sample_rate=16000)
        if detected:
            beep_detected = True
            detected_freq = freq
            break

    assert beep_detected, "1000 Hz voicemail beep tone was not detected"
    assert detected_freq == 1000.0, f"Expected 1000.0 Hz, got {detected_freq}"

    # 2. Test Silence / Low noise (should NOT detect beep)
    detector_noise = VoicemailBeepDetector(sample_rate=16000)
    noise_pcm = bytes(320 * 2 * 10)  # 200ms of zero PCM
    noise_detected = False
    for i in range(0, len(noise_pcm), chunk_size):
        d, _ = detector_noise.process_pcm_chunk(noise_pcm[i:i + chunk_size], sample_rate=16000)
        if d:
            noise_detected = True

    assert not noise_detected, "Silence triggered false positive beep detection"
    return f"1000 Hz beep detected at {detected_freq:.0f} Hz; noise rejection verified"


def test_semantic_keyword_classification():
    """Verify STT transcript semantic pattern matching for voicemail vs human greetings."""
    detector = SemanticKeywordDetector()

    # Machine phrases
    vm_samples = [
        "Please leave a message after the tone and I will call you back.",
        "You have reached John Doe. I am not available right now, leave your name and number.",
        "Your call has been forwarded to an automated voice mail system.",
        "At the beep please record your message.",
    ]
    for phrase in vm_samples:
        cls, conf, match = detector.classify_transcript(phrase)
        assert cls == "machine", f"Phrase '{phrase}' classified as '{cls}', expected 'machine'"
        assert conf >= 0.85

    # Human phrases
    human_samples = [
        "Hello?",
        "Hi",
        "Speaking.",
        "This is Robert.",
    ]
    for phrase in human_samples:
        cls, conf, match = detector.classify_transcript(phrase)
        assert cls == "human", f"Phrase '{phrase}' classified as '{cls}', expected 'human'"

    return f"4 voicemail phrases matched ({vm_samples[0][:28]}...) & 4 human phrases verified"


def test_voicemail_drop_execution():
    """Verify voicemail drop message synthesis, state transitions, and audio duration metadata."""
    manager = AMDManager()
    call_id = f"test-vm-drop-{int(time.time()*1000)}"

    # Simulate transition to beep
    session = manager.get_or_create_session(call_id)
    session.state = AMDState.VOICEMAIL_BEEP
    session.beep_detected = True

    custom_message = "Hi Jane, this is Northgate Clinic confirming your appointment tomorrow at 10 AM. Please call 800-555-0199."
    drop_result = manager.trigger_voicemail_drop(call_id, custom_message=custom_message)

    assert drop_result["status"] == "ok"
    assert drop_result["state"] in (AMDState.VOICEMAIL_COMPLETED.value, AMDState.HANGUP.value)
    assert drop_result["message"] == custom_message
    assert drop_result["audio_duration_s"] > 1.0
    assert drop_result["auto_hangup"] is True

    # Check updated session
    final_state = session.to_result()
    assert final_state.state in (AMDState.VOICEMAIL_COMPLETED, AMDState.HANGUP)

    return f"drop executed: state={drop_result['state']}, dur={drop_result['audio_duration_s']}s, auto_hangup=True"


def test_telephony_amd_rest_endpoints():
    """Verify GET and POST /api/telephony/amd and /api/telephony/voicemail-drop REST endpoints."""
    # 1. GET /api/telephony/amd (default config)
    try:
        with urllib.request.urlopen("http://localhost:8091/api/telephony/amd", timeout=5) as r:
            assert r.status == 200
            data = json.loads(r.read().decode())
            assert data.get("status") == "ok"
            assert "default_config" in data
            assert data["default_config"]["enabled"] is True
    except Exception as e:
        raise AssertionError(f"GET /api/telephony/amd failed: {e}")

    test_call_id = f"test-rest-amd-{int(time.time()*1000)}"

    # 2. POST /api/telephony/amd/configure
    try:
        req = urllib.request.Request(
            "http://localhost:8091/api/telephony/amd/configure",
            data=json.dumps({
                "call_id": test_call_id,
                "message": "Special voicemail message for REST test.",
                "action_on_machine": "drop_voicemail",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            res = json.loads(r.read().decode())
            assert res.get("status") == "ok"
            assert res.get("config", {}).get("message") == "Special voicemail message for REST test."
    except Exception as e:
        raise AssertionError(f"POST /api/telephony/amd/configure failed: {e}")

    # 3. POST /api/telephony/amd/simulate (simulate voicemail greeting)
    try:
        req = urllib.request.Request(
            "http://localhost:8091/api/telephony/amd/simulate",
            data=json.dumps({
                "call_id": test_call_id,
                "event_type": "machine_greeting",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            res = json.loads(r.read().decode())
            assert res.get("status") == "ok"
            assert res.get("amd", {}).get("state") == "machine_greeting"
    except Exception as e:
        raise AssertionError(f"POST /api/telephony/amd/simulate failed: {e}")

    # 4. POST /api/telephony/voicemail-drop
    try:
        req = urllib.request.Request(
            "http://localhost:8091/api/telephony/voicemail-drop",
            data=json.dumps({
                "call_id": test_call_id,
                "message": "Voicemail drop executed via REST API",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            res = json.loads(r.read().decode())
            assert res.get("status") == "ok"
            assert res.get("drop", {}).get("state") in ("voicemail_completed", "hangup")
    except Exception as e:
        raise AssertionError(f"POST /api/telephony/voicemail-drop failed: {e}")

    # 5. GET /api/telephony/amd?call_id=...
    try:
        with urllib.request.urlopen(f"http://localhost:8091/api/telephony/amd?call_id={test_call_id}", timeout=5) as r:
            assert r.status == 200
            data = json.loads(r.read().decode())
            assert data.get("status") == "ok"
            assert data.get("amd", {}).get("state") in ("voicemail_completed", "hangup")
    except Exception as e:
        raise AssertionError(f"GET /api/telephony/amd?call_id=... failed: {e}")

    return "GET/POST /api/telephony/amd & POST /api/telephony/voicemail-drop 200 OK"


def test_live_webrtc_amd_dispatch():
    """Verify WebRTC data channel AMD event broadcasting and voicemail drop action with worker."""
    room_name = f"verify-amd-{int(time.time()*1000)}"
    script = """
import asyncio, json, os, time
from livekit import rtc
from agent.token import join_token

async def test():
    key = os.environ['LIVEKIT_API_KEY']
    secret = os.environ['LIVEKIT_API_SECRET']
    url = os.environ['LIVEKIT_URL']
    token = join_token(key, secret, '__ROOM__', 'amd-tester')
    room = rtc.Room()

    agent_ready = asyncio.get_event_loop().create_future()
    amd_event_received = asyncio.get_event_loop().create_future()

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
        if packet.topic == 'amd_event':
            if not amd_event_received.done():
                amd_event_received.set_result(data)

    await room.connect(url, token)
    for p in room.remote_participants.values():
        if not agent_ready.done():
            agent_ready.set_result(p)

    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track('mic', source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    await asyncio.wait_for(agent_ready, timeout=10.0)
    await asyncio.sleep(0.5)

    # Publish simulate_amd action over WebRTC data channel
    amd_pkt = json.dumps({
        'action': 'simulate_amd',
        'event_type': 'machine_greeting'
    }).encode()
    await room.local_participant.publish_data(amd_pkt, reliable=True)

    result = await asyncio.wait_for(amd_event_received, timeout=10.0)
    await room.disconnect()
    state = result.get('state', 'unknown')
    action = result.get('action', 'unknown')
    conf = result.get('confidence', 0.0)
    print(f"{state}|{action}|{conf}")

asyncio.run(test())
""".replace("__ROOM__", room_name)

    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Live WebRTC AMD test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No AMD event received: {res.stdout}")

    state, action, conf = lines[-1].split("|", 2)
    return f"state='{state}', action='{action}', conf={conf} verified over WebRTC data channel"


def main():
    print("Task 2.4 — Answering Machine Detection (AMD) & Voicemail Drop Acceptance\n")
    checks = [
        ("AMD models, thresholds & cadence classifier", test_amd_models_and_cadence_classifier),
        ("Voicemail beep audio DSP frequency detection", test_voicemail_beep_dsp_detection),
        ("Transcript semantic keyword classification", test_semantic_keyword_classification),
        ("Automated voicemail drop execution & audio synthesis", test_voicemail_drop_execution),
        ("Telephony AMD & voicemail drop REST endpoints", test_telephony_amd_rest_endpoints),
        ("Live WebRTC AMD action & event dispatching", test_live_webrtc_amd_dispatch),
    ]

    passed = 0
    for name, fn in checks:
        if check(name, fn):
            passed += 1

    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
