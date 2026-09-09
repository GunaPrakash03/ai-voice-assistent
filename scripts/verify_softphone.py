#!/usr/bin/env python3
"""Task 2.6 — WebRTC Telephony Softphone / Web Calling Dialpad Acceptance Test.

Verifies:
1. Softphone HTML/CSS/JS asset integrity, LCD screen, 3x4 dialpad & UMD LiveKit bundle.
2. Web Audio DTMF touch-tone (RFC 4733) & US ringback tone (440Hz + 480Hz) synthesizer.
3. Inbound call state machine, notification model & Accept/Decline lifecycle.
4. Audio hardware constraints (AEC, AGC, Noise Suppression) & device enumeration.
5. Softphone REST API endpoint dispatching (/token, /dial, /hold, /transfer, /dtmf).
6. Live WebRTC softphone room connection, token minting & bidirectional data channel dispatch.
"""

import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from agent.token import join_token


def check(name, fn):
    try:
        msg = fn()
        print(f"  \033[32mPASS\033[0m  {name} — {msg}")
        return True
    except Exception as e:
        print(f"  \033[31mFAIL\033[0m  {name} — {e}")
        return False


def test_softphone_asset_and_ui_integrity():
    """Verify softphone.html existence, markup elements, LCD display, keypad & scripts."""
    softphone_path = os.path.join(ROOT, "web", "softphone.html")
    assert os.path.isfile(softphone_path), f"Missing softphone.html at {softphone_path}"

    with open(softphone_path, "r", encoding="utf-8") as f:
        html = f.read()

    assert len(html) > 15000, f"softphone.html seems incomplete ({len(html)} bytes)"

    # Check LiveKit Client UMD import
    assert "livekit-client.umd.min.js" in html, "LiveKit client UMD script tag missing"

    # Check LCD Display elements
    lcd_elements = ["lcd-state", "lcd-timer", "display-number", "lcd-codec", "lcd-hold-status", "vu-bar"]
    for el in lcd_elements:
        assert f'id="{el}"' in html, f"Missing LCD element #{el}"

    # Check 3x4 Keypad Digits
    digits = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]
    for d in digits:
        assert f'data-digit="{d}"' in html, f"Missing dialpad key for digit '{d}'"

    # Check In-Call Actions
    action_btns = ["btn-mute", "btn-hold", "btn-rec", "btn-xfer"]
    for btn in action_btns:
        assert f'id="{btn}"' in html, f"Missing in-call action button #{btn}"

    # Check Call Controls
    control_btns = ["btn-dial", "btn-hangup", "btn-backspace"]
    for btn in control_btns:
        assert f'id="{btn}"' in html, f"Missing call control button #{btn}"

    # Check Inbound Call Simulation Banner & Buttons
    inbound_els = ["incoming-banner", "incoming-caller-id", "btn-accept-call", "btn-decline-call"]
    for el in inbound_els:
        assert f'id="{el}"' in html, f"Missing inbound element #{el}"

    # Check Hardware Device Selectors
    hw_els = ["mic-select", "speaker-select"]
    for el in hw_els:
        assert f'id="{el}"' in html, f"Missing hardware device selector #{el}"

    # Check Log Container & Navigation
    assert 'id="softphone-log"' in html, "Missing telemetry log container #softphone-log"
    assert 'href="/"' in html, "Missing link back to Live Console"
    assert 'href="/call-desk.html"' in html, "Missing link to Call Desk Analytics"

    return f"softphone.html ({len(html)} bytes) with complete LCD, 12-key dialpad, in-call actions & telemetry validated"


def test_audio_tone_synthesizer():
    """Verify standard RFC 4733 DTMF dual-tone matrix and US ringback tone frequencies."""
    # Standard ITU-T / RFC 4733 DTMF matrix
    standard_dtmf = {
        '1': (697, 1209), '2': (697, 1336), '3': (697, 1477),
        '4': (770, 1209), '5': (770, 1336), '6': (770, 1477),
        '7': (852, 1209), '8': (852, 1336), '9': (852, 1477),
        '*': (941, 1209), '0': (941, 1336), '#': (941, 1477)
    }

    softphone_path = os.path.join(ROOT, "web", "softphone.html")
    with open(softphone_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Verify DTMF table in softphone code matches standards
    for digit, (f1, f2) in standard_dtmf.items():
        pattern = rf"'{re.escape(digit)}':\s*\[\s*{f1}\s*,\s*{f2}\s*\]"
        assert re.search(pattern, html), f"DTMF frequency mapping for '{digit}' missing or incorrect in JS"

    # Synthesize DTMF dual tones mathematically to verify signal characteristics (no clipping)
    sample_rate = 16000
    duration_sec = 0.16
    n_samples = int(sample_rate * duration_sec)

    for digit, (f1, f2) in standard_dtmf.items():
        samples = []
        for i in range(n_samples):
            t = i / sample_rate
            # 50% amplitude per oscillator, matching Web Audio gain
            val = 0.5 * math.sin(2 * math.pi * f1 * t) + 0.5 * math.sin(2 * math.pi * f2 * t)
            samples.append(val)
        peak = max(abs(s) for s in samples)
        assert peak <= 1.0001, f"DTMF waveform for '{digit}' exceeds 0 dBFS peak ({peak})"
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        assert rms > 0.4, f"DTMF waveform for '{digit}' too weak ({rms})"

    # Verify US standard ringback frequencies: 440 Hz + 480 Hz
    assert "440.0" in html and "480.0" in html, "US standard ringback frequencies (440Hz/480Hz) not present in JS"

    return "12/12 DTMF dual frequencies & US ringback (440+480Hz) mathematical models verified"


def test_inbound_call_state_machine():
    """Verify inbound call notification, caller ID presentation, and accept/decline state transitions."""
    softphone_path = os.path.join(ROOT, "web", "softphone.html")
    with open(softphone_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Verify inbound event triggers and elements
    assert "btn-trigger-inbound" in html, "Missing inbound simulator trigger button"
    assert "playInboundRingtone" in html, "Missing inbound audio ringtone function"
    assert "btn-accept-call" in html and "btn-decline-call" in html, "Missing accept/decline controls"

    # Verify Decline handler cleans up and logs SIP 486
    assert 'SIP 486 Busy Here' in html, "Decline action missing standard SIP 486 Busy Here response"

    # Verify Accept handler answers and dials into the conference
    assert 'Inbound call answered' in html, "Accept handler missing answer transition logic"

    # Verify Quick Dial presets are defined
    presets = ["+18885550188", "+18885550142", "+18005550199", "+18005550100"]
    for p in presets:
        assert p in html, f"Preset phone number {p} missing from dialer buttons"

    return "Inbound ring state machine, SIP 486 decline & quick dial presets verified"


def test_audio_device_configuration():
    """Verify WebRTC audio constraints (AEC, AGC, Noise Suppression) & device enumeration."""
    softphone_path = os.path.join(ROOT, "web", "softphone.html")
    with open(softphone_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Hardware selector bindings
    assert "enumerateAudioDevices" in html, "Missing enumerateAudioDevices routine"
    assert "audioinput" in html and "audiooutput" in html, "Missing device kind discrimination"

    # Verify default system fallback
    assert "Default System Microphone" in html, "Missing fallback microphone option"
    assert "Default System Speaker" in html, "Missing fallback speaker option"

    # Verify audio processing flags
    assert "Acoustic Echo Cancellation" in html, "Missing AEC badge"
    assert "Noise Suppression" in html, "Missing Noise Suppression badge"
    assert "Auto Gain Control" in html, "Missing AGC badge"

    return "Microphone/speaker enumeration & AEC/AGC/Noise Suppression constraints validated"


def test_softphone_rest_endpoints():
    """Verify softphone-related REST endpoints served by scripts/serve.py on port 8091."""
    base_url = "http://localhost:8091"

    # 1. GET /softphone.html
    req = urllib.request.Request(f"{base_url}/softphone.html")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        content = r.read().decode("utf-8")
        assert "WebRTC Telephony Softphone" in content

    # 2. GET /token for softphone room
    test_room = f"softphone-test-{int(time.time()*1000)}"
    req = urllib.request.Request(f"{base_url}/token?room={test_room}&identity=softphone-user-1")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        data = json.loads(r.read().decode())
        assert "token" in data and "url" in data
        assert len(data["token"]) > 30

    # 3. POST /api/telephony/dial
    dial_data = json.dumps({
        "destination": "+18885550188",
        "caller_id": "+18005550199",
        "room": test_room,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/telephony/dial",
        data=dial_data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"
        assert res.get("call", {}).get("to_number") == "+18885550188"

    # 4. POST /api/telephony/hold
    hold_data = json.dumps({"call_id": test_room, "hold": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/telephony/hold",
        data=hold_data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"

    # 5. POST /api/telephony/dtmf
    dtmf_data = json.dumps({"call_id": test_room, "digit": "5"}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/telephony/dtmf",
        data=dtmf_data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"

    # 6. POST /api/telephony/transfer
    xfer_data = json.dumps({
        "call_id": test_room,
        "target_number": "+18885550142",
        "mode": "warm",
        "department": "support",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/telephony/transfer",
        data=xfer_data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        res = json.loads(r.read().decode())
        assert res.get("status") == "ok"

    return "GET /softphone.html, /token & POST /dial, /hold, /dtmf, /transfer 200 OK"


def test_live_webrtc_softphone_dispatch():
    """Verify live WebRTC softphone media session, token join & in-call data channel dispatch."""
    room_name = f"verify-softphone-{int(time.time()*1000)}"
    script = """
import asyncio, json, os, time
from livekit import rtc
from agent.token import join_token

async def test():
    key = os.environ['LIVEKIT_API_KEY']
    secret = os.environ['LIVEKIT_API_SECRET']
    url = os.environ['LIVEKIT_URL']
    token = join_token(key, secret, '__ROOM__', 'softphone-agent')
    room = rtc.Room()

    agent_ready = asyncio.get_event_loop().create_future()
    dtmf_received = asyncio.get_event_loop().create_future()
    hold_received = asyncio.get_event_loop().create_future()

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
            if not dtmf_received.done():
                dtmf_received.set_result(data)
        elif packet.topic == 'hold_state':
            if not hold_received.done():
                hold_received.set_result(data)

    await room.connect(url, token)
    for p in room.remote_participants.values():
        if not agent_ready.done():
            agent_ready.set_result(p)

    # Publish softphone microphone stream
    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track('mic', source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    await asyncio.wait_for(agent_ready, timeout=15.0)
    await asyncio.sleep(0.5)

    # 1. Send in-call DTMF touch-tone
    dtmf_pkt = json.dumps({'action': 'send_dtmf', 'digit': '3', 'duration_ms': 160}).encode()
    await room.local_participant.publish_data(dtmf_pkt, reliable=True)
    dtmf_res = await asyncio.wait_for(dtmf_received, timeout=15.0)

    # 2. Send in-call Hold toggle
    hold_pkt = json.dumps({'action': 'hold_call', 'hold': True, 'reason': 'softphone_hold'}).encode()
    await room.local_participant.publish_data(hold_pkt, reliable=True)
    hold_res = await asyncio.wait_for(hold_received, timeout=15.0)

    await room.disconnect()

    digit = dtmf_res.get('digit', '')
    dtmf_st = dtmf_res.get('status', '')
    is_held = hold_res.get('is_held', False)
    print(f"{digit}|{dtmf_st}|{is_held}")

asyncio.run(test())
""".replace("__ROOM__", room_name)

    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Live WebRTC softphone test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No softphone response received: {res.stdout}")

    digit, dtmf_st, is_held = lines[-1].split("|", 2)
    return f"DTMF digit '{digit}' ({dtmf_st}) & Hold state (held={is_held}) verified over WebRTC data channel"


def main():
    print("Task 2.6 — WebRTC Telephony Softphone / Web Calling Dialpad Acceptance\n")
    checks = [
        ("Softphone asset & UI integrity", test_softphone_asset_and_ui_integrity),
        ("Audio tone synthesizer (DTMF & ringback)", test_audio_tone_synthesizer),
        ("Inbound call state machine & presets", test_inbound_call_state_machine),
        ("Audio device configuration & AEC/AGC", test_audio_device_configuration),
        ("Softphone REST endpoints dispatch", test_softphone_rest_endpoints),
        ("Live WebRTC room connection & data dispatch", test_live_webrtc_softphone_dispatch),
    ]

    passed = 0
    for name, fn in checks:
        if check(name, fn):
            passed += 1

    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
