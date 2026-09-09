#!/usr/bin/env python3
"""
Task 2.2 — Call Transfer Engine (Blind & Warm Transfer) Acceptance Tests.

Verifies:
1. Transfer models, state machine, and target E.164 phone validation.
2. Blind (cold) transfer execution with SIP REFER simulation.
3. Warm (attended) transfer lifecycle: caller hold, consultation leg, handoff briefing, bridging & AI exit.
4. Transfer failure & decline recovery: timeout/busy handling, automatic caller un-hold & fallback dialogue.
5. Live WebRTC transfer tool invocation & data channel event telemetry.
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

from agent.transfer_manager import (
    CallHoldState,
    TransferManager,
    TransferMode,
    TransferRecord,
    TransferStatus,
    transfer_manager,
)
from agent.telephony_manager import normalize_phone_number, is_valid_phone_number


def check(name, fn):
    try:
        out = fn()
        tag = f" — {out}" if out else ""
        print(f"  \033[32mPASS\033[0m  {name}{tag}")
        return True
    except Exception as e:
        print(f"  \033[31mFAIL\033[0m  {name} — {e}")
        return False


def test_transfer_models_and_validation():
    """Verify TransferMode, TransferStatus, and target phone number validation."""
    assert TransferMode.BLIND == "blind"
    assert TransferMode.WARM == "warm"

    # Status machine stages
    statuses = [s.value for s in TransferStatus]
    for required in ["initiated", "hold", "consulting", "briefing", "bridged", "completed", "failed", "cancelled"]:
        assert required in statuses, f"Missing required transfer status: {required}"

    # Target validation
    assert is_valid_phone_number("+18885550142"), "Expected valid E.164"
    assert normalize_phone_number("888-555-0142") == "+18885550142"
    assert not is_valid_phone_number("invalid-phone")

    # Briefing generator
    brief = transfer_manager.generate_briefing(
        caller_name="Jane Doe",
        topic="Billing Inquiry",
        department="Finance",
        context_notes="Invoice #9948 has discrepancy",
    )
    assert "Jane Doe" in brief
    assert "Billing Inquiry" in brief
    assert "Finance" in brief
    assert "Invoice #9948" in brief

    return f"models validated ({len(statuses)} states) & handoff briefing generator verified"


def test_blind_transfer_execution():
    """Verify blind (cold) transfer initiates, runs SIP REFER, and completes immediately."""
    mgr = TransferManager()
    loop = asyncio.new_event_loop()
    try:
        rec = loop.run_until_complete(
            mgr.initiate_blind_transfer(
                call_id="test-blind-call-1",
                target_number="+18885550142",
                source_participant="caller-1",
                department="billing",
                reason="Transfer to billing specialist",
            )
        )
    finally:
        loop.close()

    assert rec.mode == TransferMode.BLIND
    assert rec.status == TransferStatus.COMPLETED, f"Expected COMPLETED, got {rec.status}"
    assert rec.target_number == "+18885550142"
    assert rec.completed_at is not None
    assert rec.duration_seconds >= 0.0
    assert rec.transfer_id.startswith("xfer-blind-")

    return f"blind transfer '{rec.transfer_id}' completed (mode=blind, target={rec.target_number}, dur={rec.duration_seconds}s)"


def test_warm_transfer_lifecycle():
    """Verify warm (attended) transfer executes hold -> consulting -> briefing -> bridged -> completed."""
    mgr = TransferManager()
    loop = asyncio.new_event_loop()
    try:
        rec = loop.run_until_complete(
            mgr.initiate_warm_transfer(
                call_id="test-warm-call-1",
                target_number="+18005550199",
                source_participant="caller-42",
                caller_name="Dr. Robert",
                caller_inquiry="Surgery equipment consultation",
                department="medical-sales",
                reason="High-value enterprise lead",
            )
        )
    finally:
        loop.close()

    assert rec.mode == TransferMode.WARM
    assert rec.status == TransferStatus.COMPLETED
    assert rec.consultation_room is not None and "consult-" in rec.consultation_room
    assert rec.consultation_participant == "agent-+18005550199"
    assert "Dr. Robert" in (rec.briefing_summary or "")
    assert not mgr.is_on_hold("test-warm-call-1"), "Caller must be taken off hold when bridged"

    return f"warm transfer '{rec.transfer_id}' lifecycle verified (briefing='{rec.briefing_summary[:40]}...', consult_room={rec.consultation_room})"


def test_transfer_failure_and_fallback():
    """Verify transfer failure/decline un-holds caller and allows conversational fallback."""
    mgr = TransferManager()

    # Step 1: Put caller on hold as transfer starts
    call_id = "test-fail-call-1"
    mgr.put_on_hold(call_id, reason="Transfer in progress")
    assert mgr.is_on_hold(call_id), "Call should be marked on hold"

    # Step 2: Create a transfer record
    rec = TransferRecord(
        transfer_id="xfer-fail-001",
        call_id=call_id,
        source_participant="caller",
        target_number="+18885550142",
        mode=TransferMode.WARM,
        status=TransferStatus.CONSULTING,
    )
    mgr._transfers[rec.transfer_id] = rec

    # Step 3: Specialist declines or line is busy
    failed_rec = mgr.fail_transfer("xfer-fail-001", error_reason="Destination line busy (SIP 486)")
    assert failed_rec is not None
    assert failed_rec.status == TransferStatus.FAILED
    assert failed_rec.failure_reason == "Destination line busy (SIP 486)"
    assert not mgr.is_on_hold(call_id), "Caller MUST be un-held on transfer failure to allow conversational fallback"

    # Step 4: Test user cancellation as well
    mgr.put_on_hold(call_id, reason="Transfer attempt 2")
    rec2 = TransferRecord(
        transfer_id="xfer-cancel-002",
        call_id=call_id,
        source_participant="caller",
        target_number="+18885550142",
        mode=TransferMode.WARM,
        status=TransferStatus.HOLD,
    )
    mgr._transfers[rec2.transfer_id] = rec2
    cancelled_rec = mgr.cancel_transfer("xfer-cancel-002", reason="caller requested to stay on line")
    assert cancelled_rec is not None
    assert cancelled_rec.status == TransferStatus.CANCELLED
    assert not mgr.is_on_hold(call_id), "Caller MUST be un-held on cancellation"

    return "failure and cancellation un-hold caller verified (SIP 486 busy -> fallback active)"


def test_transfer_rest_endpoints():
    """Verify HTTP REST endpoints for transfer and hold operations."""
    # 1. GET /api/telephony/transfers
    try:
        with urllib.request.urlopen("http://localhost:8091/api/telephony/transfers", timeout=5) as r:
            assert r.status == 200
            data = json.loads(r.read().decode())
            assert "transfers" in data
    except Exception as e:
        raise AssertionError(f"GET /api/telephony/transfers failed: {e}")

    # 2. POST /api/telephony/transfer (warm)
    try:
        req = urllib.request.Request(
            "http://localhost:8091/api/telephony/transfer",
            data=json.dumps({
                "call_id": "test-rest-call-1",
                "target_number": "+18885550142",
                "mode": "warm",
                "department": "billing",
                "reason": "REST transfer test",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            res = json.loads(r.read().decode())
            assert res.get("status") == "ok"
            assert res.get("transfer", {}).get("mode") == "warm"
    except Exception as e:
        raise AssertionError(f"POST /api/telephony/transfer failed: {e}")

    # 3. POST /api/telephony/hold
    try:
        req = urllib.request.Request(
            "http://localhost:8091/api/telephony/hold",
            data=json.dumps({
                "call_id": "test-rest-call-1",
                "hold": True,
                "reason": "consultation",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            res = json.loads(r.read().decode())
            assert res.get("status") == "ok"
            assert res.get("hold", {}).get("is_held") is True
    except Exception as e:
        raise AssertionError(f"POST /api/telephony/hold failed: {e}")

    return "GET /api/telephony/transfers & POST /api/telephony/transfer 200 OK"


def test_live_webrtc_transfer_dispatch():
    """Verify WebRTC data channel transfer_call action and live event dispatching with the agent worker."""
    script = """
import asyncio, json, os, time
from livekit import rtc
from agent.token import join_token

async def test():
    key = os.environ['LIVEKIT_API_KEY']
    secret = os.environ['LIVEKIT_API_SECRET']
    url = os.environ['LIVEKIT_URL']
    token = join_token(key, secret, 'verify-transfer-room', 'transfer-tester')
    room = rtc.Room()

    agent_ready = asyncio.get_event_loop().create_future()
    transfer_event_received = asyncio.get_event_loop().create_future()

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
        if packet.topic == 'transfer_event':
            if not transfer_event_received.done():
                transfer_event_received.set_result(data)

    await room.connect(url, token)
    for p in room.remote_participants.values():
        if not agent_ready.done():
            agent_ready.set_result(p)

    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track('mic', source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    await asyncio.wait_for(agent_ready, timeout=10.0)
    await asyncio.sleep(0.5)

    # Publish transfer_call action over WebRTC data channel
    xfer_pkt = json.dumps({
        'action': 'transfer_call',
        'target_number': '+18885550142',
        'mode': 'warm',
        'department': 'billing',
        'reason': 'Live WebRTC transfer verification'
    }).encode()
    await room.local_participant.publish_data(xfer_pkt, reliable=True)

    result = await asyncio.wait_for(transfer_event_received, timeout=10.0)
    await room.disconnect()
    event_type = result.get('event', 'unknown')
    target = result.get('target_number') or result.get('transfer', {}).get('target_number', 'unknown')
    print(f"{event_type}|{target}")

asyncio.run(test())
"""
    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Live WebRTC transfer test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No transfer event received: {res.stdout}")

    event_type, target = lines[-1].split("|", 1)
    return f"event='{event_type}', target='{target}' verified over WebRTC data channel"


def main():
    print("Task 2.2 — Call Transfer Engine (Blind & Warm Transfer) Acceptance\n")
    checks = [
        ("Transfer models, state machine & E.164 target validation", test_transfer_models_and_validation),
        ("Blind (cold) transfer execution via SIP REFER", test_blind_transfer_execution),
        ("Warm (attended) transfer lifecycle with hold & briefing", test_warm_transfer_lifecycle),
        ("Transfer failure, decline & cancellation fallback", test_transfer_failure_and_fallback),
        ("Telephony transfer REST API server endpoints", test_transfer_rest_endpoints),
        ("Live WebRTC transfer action & event dispatching", test_live_webrtc_transfer_dispatch),
    ]

    passed = 0
    for name, fn in checks:
        if check(name, fn):
            passed += 1

    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
