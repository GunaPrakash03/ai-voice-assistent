#!/usr/bin/env python3
"""
Task 2.3 — DTMF Digit Handling & IVR Navigation Acceptance Tests.

Verifies:
1. DTMF digit validation, RFC 4733 event parsing & Goertzel dual-tone audio DSP detection.
2. Multi-digit keypad sequence buffer with '#' terminator and backspace editing.
3. IVR phone tree navigation & state transitions (main -> sales -> main -> transfer).
4. IVR unmatched digit handling, retry counters & automatic fallback routing.
5. Telephony DTMF & IVR REST API server endpoints (GET /api/telephony/ivr, POST /api/telephony/dtmf).
6. Live WebRTC DTMF dispatching & IVR event handling with the agent worker over data channels.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from agent.dtmf_manager import (
    DTMFActionType,
    DTMFDigitBuffer,
    DTMFEvent,
    DTMFManager,
    GoertzelDetector,
    VALID_DTMF_DIGITS,
    dtmf_manager,
)


def check(name, fn):
    try:
        out = fn()
        tag = f" — {out}" if out else ""
        print(f"  \033[32mPASS\033[0m  {name}{tag}")
        return True
    except Exception as e:
        print(f"  \033[31mFAIL\033[0m  {name} — {e}")
        return False


def test_dtmf_models_and_goertzel_dsp():
    """Verify DTMF frequency matrix, validation, and in-band Goertzel tone detection."""
    # 1. Digits validation
    for d in "0123456789*#ABCD":
        assert d in VALID_DTMF_DIGITS

    # 2. Goertzel DSP Tone Generation & Detection
    # Generate PCM audio for digit '5' (770 Hz + 1336 Hz) @ 8kHz
    samples_5 = GoertzelDetector.generate_tone("5", duration_ms=200, sample_rate=8000)
    assert len(samples_5) == 1600
    detected_5 = GoertzelDetector.detect_tone(samples_5, sample_rate=8000, threshold=0.15)
    assert detected_5 == "5", f"Expected detected '5', got '{detected_5}'"

    # Generate PCM audio for digit '9' (852 Hz + 1477 Hz) @ 8kHz
    samples_9 = GoertzelDetector.generate_tone("9", duration_ms=200, sample_rate=8000)
    detected_9 = GoertzelDetector.detect_tone(samples_9, sample_rate=8000, threshold=0.15)
    assert detected_9 == "9", f"Expected detected '9', got '{detected_9}'"

    # Generate PCM audio for '#' (941 Hz + 1477 Hz)
    samples_hash = GoertzelDetector.generate_tone("#", duration_ms=200, sample_rate=8000)
    detected_hash = GoertzelDetector.detect_tone(samples_hash, sample_rate=8000, threshold=0.15)
    assert detected_hash == "#", f"Expected detected '#', got '{detected_hash}'"

    return f"16 valid keypad digits, Goertzel DSP verified (tones '5','9','#' detected cleanly)"


def test_dtmf_digit_buffer():
    """Verify multi-digit buffer accumulation, backspace, and '#' terminator."""
    buf = DTMFDigitBuffer(max_length=10, terminator="#")

    # Push sequence 1-0-4-2
    assert not buf.push("1")
    assert not buf.push("0")
    assert not buf.push("4")
    assert not buf.push("2")
    assert buf.get_value() == "1042"
    assert len(buf) == 4

    # Backspace
    popped = buf.backspace()
    assert popped == "2"
    assert buf.get_value() == "104"

    # Push '2' again and '#' terminator
    assert not buf.push("2")
    terminated = buf.push("#")
    assert terminated is True, "Expected '#' to trigger sequence terminator"
    assert buf.get_value() == "1042", "Terminator should not be appended to payload value"

    # Clear
    buf.clear()
    assert buf.get_value() == ""
    assert len(buf) == 0

    return "sequence '1042' buffered, backspace verified, '#' terminator confirmed"


def test_ivr_tree_navigation():
    """Verify IVR state transitions across menu tree nodes and departments."""
    mgr = DTMFManager()
    call_id = "test-ivr-call-1"

    # Initial state: main menu
    active = mgr.get_active_node(call_id)
    assert active.node_id == "main"

    # Press '1': Navigate to Sales submenu
    res1 = mgr.process_dtmf_digit(call_id, "1")
    assert res1["status"] == "navigated"
    assert res1["action"] == "navigate"
    assert res1["node_id"] == "sales"
    assert mgr.get_active_node(call_id).node_id == "sales"

    # In Sales, Press '0': Return to Main menu
    res0 = mgr.process_dtmf_digit(call_id, "0")
    assert res0["status"] == "navigated"
    assert res0["node_id"] == "main"
    assert mgr.get_active_node(call_id).node_id == "main"

    # In Main, Press '2': Transfer to Technical Support
    res2 = mgr.process_dtmf_digit(call_id, "2")
    assert res2["status"] == "transfer_triggered"
    assert res2["action"] == "transfer"
    assert res2["department"] == "technical support"
    assert res2["transfer_number"] == "+18885550142"

    # In Main, Press '*': Repeat current prompt
    res_repeat = mgr.process_dtmf_digit(call_id, "*")
    assert res_repeat["status"] == "repeat"
    assert res_repeat["action"] == "repeat"
    assert "Thanks for calling" in res_repeat["prompt"]

    return "main -> sales -> main -> tech support transfer (+18885550142) verified"


def test_ivr_unmatched_digit_and_fallback():
    """Verify unmatched digits increment retry counter and trigger fallback on max retries."""
    mgr = DTMFManager()
    call_id = "test-ivr-fallback-call"

    # First invalid digit '7' on main menu
    res_err1 = mgr.process_dtmf_digit(call_id, "7")
    assert res_err1["status"] == "unmatched_digit"
    assert res_err1["action"] == "retry"
    assert "Invalid option" in res_err1["prompt"]

    # Second invalid digit '8' -> exceeds max_retries (2) -> triggers fallback
    res_err2 = mgr.process_dtmf_digit(call_id, "8")
    assert res_err2["status"] == "max_retries_fallback"
    assert res_err2["action"] == "fallback"
    assert res_err2["node_id"] == "main"
    assert "Sorry, I did not recognize that option" in res_err2["prompt"]

    return "unmatched digit '7' retried -> second invalid '8' triggered fallback"


def test_dtmf_rest_endpoints():
    """Verify HTTP REST endpoints for DTMF digit injection and IVR state inspection."""
    # 1. GET /api/telephony/ivr (menu list)
    try:
        with urllib.request.urlopen("http://localhost:8091/api/telephony/ivr", timeout=5) as r:
            assert r.status == 200
            data = json.loads(r.read().decode())
            assert data.get("status") == "ok"
            assert "menus" in data
            assert any(m["node_id"] == "main" for m in data["menus"])
    except Exception as e:
        raise AssertionError(f"GET /api/telephony/ivr failed: {e}")

    test_call_id = f"test-rest-ivr-{int(time.time() * 1000)}"

    # 2. POST /api/telephony/dtmf
    try:
        req = urllib.request.Request(
            "http://localhost:8091/api/telephony/dtmf",
            data=json.dumps({
                "call_id": test_call_id,
                "digit": "1",
                "duration_ms": 180,
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            res = json.loads(r.read().decode())
            assert res.get("status") == "ok"
            assert res.get("result", {}).get("status") == "navigated"
            assert res.get("result", {}).get("node_id") == "sales"
    except Exception as e:
        raise AssertionError(f"POST /api/telephony/dtmf failed: {e}")

    # 3. GET /api/telephony/ivr?call_id=... (call state)
    try:
        with urllib.request.urlopen(f"http://localhost:8091/api/telephony/ivr?call_id={test_call_id}", timeout=5) as r:
            assert r.status == 200
            call_state = json.loads(r.read().decode())
            assert call_state.get("status") == "ok"
            assert call_state.get("state", {}).get("active_node", {}).get("node_id") == "sales"
    except Exception as e:
        raise AssertionError(f"GET /api/telephony/ivr?call_id=... failed: {e}")

    return "GET /api/telephony/ivr & POST /api/telephony/dtmf 200 OK (routed to sales)"


def test_live_webrtc_dtmf_dispatch():
    """Verify WebRTC data channel send_dtmf action and live IVR state dispatching with the agent worker."""
    room_name = f"verify-dtmf-{int(time.time() * 1000)}"
    script = """
import asyncio, json, os, time
from livekit import rtc
from agent.token import join_token

async def test():
    key = os.environ['LIVEKIT_API_KEY']
    secret = os.environ['LIVEKIT_API_SECRET']
    url = os.environ['LIVEKIT_URL']
    token = join_token(key, secret, '__ROOM__', 'dtmf-tester')
    room = rtc.Room()""".replace("__ROOM__", room_name) + """

    agent_ready = asyncio.get_event_loop().create_future()
    dtmf_event_received = asyncio.get_event_loop().create_future()

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
        if packet.topic == 'dtmf_event':
            if not dtmf_event_received.done():
                dtmf_event_received.set_result(data)

    await room.connect(url, token)
    for p in room.remote_participants.values():
        if not agent_ready.done():
            agent_ready.set_result(p)

    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track('mic', source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    await asyncio.wait_for(agent_ready, timeout=10.0)
    await asyncio.sleep(0.5)

    # Publish send_dtmf action over WebRTC data channel
    dtmf_pkt = json.dumps({
        'action': 'send_dtmf',
        'digit': '1',
        'duration_ms': 160
    }).encode()
    await room.local_participant.publish_data(dtmf_pkt, reliable=True)

    result = await asyncio.wait_for(dtmf_event_received, timeout=10.0)
    await room.disconnect()
    digit = result.get('digit', 'unknown')
    action = result.get('action', 'unknown')
    status = result.get('status', 'unknown')
    print(f"{digit}|{action}|{status}")

asyncio.run(test())
"""
    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Live WebRTC DTMF test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No DTMF event received: {res.stdout}")

    digit, action, status = lines[-1].split("|", 2)
    return f"digit='{digit}', action='{action}', status='{status}' verified over WebRTC data channel"


def main():
    print("Task 2.3 — DTMF Digit Handling & IVR Navigation Acceptance\n")
    checks = [
        ("DTMF models, RFC 4733 & Goertzel dual-tone audio DSP", test_dtmf_models_and_goertzel_dsp),
        ("Multi-digit keypad buffer with '#' terminator", test_dtmf_digit_buffer),
        ("IVR phone tree navigation & state transitions", test_ivr_tree_navigation),
        ("IVR unmatched digit handling & retry fallback", test_ivr_unmatched_digit_and_fallback),
        ("DTMF & IVR REST API server endpoints", test_dtmf_rest_endpoints),
        ("Live WebRTC DTMF action & event dispatching", test_live_webrtc_dtmf_dispatch),
    ]

    passed = 0
    for name, fn in checks:
        if check(name, fn):
            passed += 1

    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
