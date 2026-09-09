#!/usr/bin/env python3
"""
Task 2.1 — SIP Gateway & Telephony Integration Acceptance Tests.

Verifies:
1. SIP Trunk models & E.164 phone number validation and normalization.
2. Inbound DID dispatch rule routing engine (phone number -> room mapping).
3. Outbound dialer programmatic API & call record lifecycle tracking.
4. Telephony REST endpoints on web server (trunks, dial, simulate).
5. Live WebRTC data channel telephony action dispatching with the agent worker.
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

from agent.telephony_manager import (
    CallDirection,
    CallStatus,
    SIPDispatchRule,
    SIPInboundTrunk,
    SIPOutboundTrunk,
    TelephonyManager,
    is_valid_phone_number,
    normalize_phone_number,
    telephony_manager,
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


def test_phone_number_and_trunk_models():
    """Verify E.164 phone number normalization and SIP trunk model structures."""
    # 1. Normalization checks
    assert normalize_phone_number("8005550199") == "+18005550199", "Failed standard 10-digit US normalization"
    assert normalize_phone_number("+1 (800) 555-0199") == "+18005550199", "Failed formatted E.164 normalization"
    assert normalize_phone_number("+44 20 7946 0991") == "+442079460991", "Failed UK international normalization"

    # 2. Validation checks
    assert is_valid_phone_number("+18005550199"), "Expected valid E.164"
    assert is_valid_phone_number("8005550199"), "Expected normalized valid"
    assert not is_valid_phone_number("abc"), "Expected invalid phone"
    assert not is_valid_phone_number("0"), "Expected invalid short number"

    # 3. Trunk number matching
    trunk = SIPInboundTrunk(
        trunk_id="test-trunk",
        name="Test Inbound",
        numbers=["+18005550199", "+18885550142"],
    )
    assert trunk.matches_number("(800) 555-0199"), "Expected trunk to match normalized format"
    assert not trunk.matches_number("+15551234567"), "Expected mismatch for non-configured DID"

    return "E.164 normalization & SIP trunk models verified"


def test_inbound_did_routing():
    """Verify inbound phone call dispatch rules and room name generation."""
    mgr = TelephonyManager()

    # Route call for primary registered DID
    routed = mgr.route_inbound_call(
        dialed_number="+18005550199",
        caller_number="+15559876543",
    )

    assert routed is not None, "Failed to route inbound call"
    assert "room_name" in routed, "Missing room_name in routing result"
    assert routed["room_name"].startswith("call-"), f"Unexpected room prefix: {routed['room_name']}"
    assert routed["participant_identity"] == "sip-+15559876543", "Incorrect participant identity"
    assert routed["trunk"]["trunk_id"] == "trunk-inbound-primary", "Mismatched trunk assignment"

    return f"DID +18005550199 routed to room '{routed['room_name']}'"


def test_outbound_dialer_api():
    """Verify programmatic outbound dialing with room dispatch and call record tracking."""
    mgr = TelephonyManager()

    async def run_dial():
        record = await mgr.dial_phone_number(
            destination_number="+15552345678",
            caller_id="+18005550199",
            metadata={"campaign": "appointment_reminder"},
        )
        assert record.call_id.startswith("sip-out-"), "Invalid call_id format"
        assert record.status in (CallStatus.ACTIVE, CallStatus.RINGING), f"Unexpected status: {record.status}"
        assert record.to_number == "+15552345678", "Mismatched destination"
        assert record.from_number == "+18005550199", "Mismatched caller ID"

        # End call
        ended = mgr.end_call(record.call_id)
        assert ended.status == CallStatus.COMPLETED, "Call not marked completed"
        return record.call_id, ended.duration_seconds

    loop = asyncio.new_event_loop()
    call_id, dur = loop.run_until_complete(run_dial())
    loop.close()

    return f"outbound call '{call_id}' initiated & ended (dur={dur}s)"


def test_telephony_rest_endpoints():
    """Verify REST endpoints on the server (/api/telephony/trunks and /api/telephony/dial)."""
    base_url = "http://localhost:8091"

    # 1. GET /api/telephony/trunks
    try:
        req = urllib.request.Request(f"{base_url}/api/telephony/trunks")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
            assert "inbound" in data and len(data["inbound"]) > 0, "No inbound trunks returned"
            assert "outbound" in data and len(data["outbound"]) > 0, "No outbound trunks returned"
            assert "rules" in data and len(data["rules"]) > 0, "No dispatch rules returned"
    except Exception as e:
        raise AssertionError(f"GET /api/telephony/trunks failed: {e}")

    # 2. POST /api/telephony/dial
    try:
        body = json.dumps({"destination": "+15553334444"}).encode()
        req = urllib.request.Request(
            f"{base_url}/api/telephony/dial",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            res = json.loads(r.read().decode())
            assert res.get("status") == "ok", f"Dial endpoint failed: {res}"
            assert "call" in res, "Missing call record in response"
    except Exception as e:
        raise AssertionError(f"POST /api/telephony/dial failed: {e}")

    # 3. POST /api/telephony/inbound/simulate
    try:
        sim_body = json.dumps({"from": "+15557778888", "to": "+18005550199"}).encode()
        req = urllib.request.Request(
            f"{base_url}/api/telephony/inbound/simulate",
            data=sim_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            sim_res = json.loads(r.read().decode())
            assert sim_res.get("status") == "ok", f"Inbound simulation failed: {sim_res}"
            assert "routed" in sim_res, "Missing routed info in response"
    except Exception as e:
        raise AssertionError(f"POST /api/telephony/inbound/simulate failed: {e}")

    return "GET /api/telephony/trunks & POST /api/telephony/dial 200 OK"


def test_live_webrtc_telephony_data_channel():
    """Verify live agent worker receives dial_phone action over WebRTC and emits telephony_event."""
    script = """
import asyncio, json, os, time
from livekit import rtc
from agent.token import join_token

async def test():
    key = os.environ['LIVEKIT_API_KEY']
    secret = os.environ['LIVEKIT_API_SECRET']
    url = os.environ['LIVEKIT_URL']
    token = join_token(key, secret, 'verify-telephony-room', 'telephony-tester')
    room = rtc.Room()

    agent_ready = asyncio.get_event_loop().create_future()
    telephony_event_received = asyncio.get_event_loop().create_future()

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
        if packet.topic == 'telephony_event':
            if not telephony_event_received.done():
                telephony_event_received.set_result(data)

    await room.connect(url, token)
    for p in room.remote_participants.values():
        if not agent_ready.done():
            agent_ready.set_result(p)

    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track('mic', source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    await asyncio.wait_for(agent_ready, timeout=10.0)
    await asyncio.sleep(0.5)

    # Request outbound dialing over WebRTC data channel
    dial_pkt = json.dumps({
        'action': 'dial_phone',
        'destination': '+15556667777',
        'caller_id': '+18005550199'
    }).encode()
    await room.local_participant.publish_data(dial_pkt, reliable=True)

    result = await asyncio.wait_for(telephony_event_received, timeout=10.0)
    await room.disconnect()
    event_type = result.get('event', 'unknown')
    call_id = result.get('call', {}).get('call_id', 'unknown')
    print(f"{event_type}|{call_id}")

asyncio.run(test())
"""
    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Live WebRTC telephony test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No telephony event received: {res.stdout}")

    event_type, call_id = lines[-1].split("|", 1)
    return f"event='{event_type}', call_id='{call_id}' verified over WebRTC data channel"


if __name__ == "__main__":
    print("Task 2.1 — SIP Gateway & Telephony Integration Acceptance\n")
    results = [
        check("SIP trunk models & E.164 phone validation", test_phone_number_and_trunk_models),
        check("Inbound DID routing & room dispatch engine", test_inbound_did_routing),
        check("Outbound dialer programmatic API & lifecycle", test_outbound_dialer_api),
        check("Telephony REST API server endpoints", test_telephony_rest_endpoints),
        check("Live WebRTC telephony action dispatching", test_live_webrtc_telephony_data_channel),
    ]

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)
