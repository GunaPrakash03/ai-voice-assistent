#!/usr/bin/env python3
"""
Test harness for Twilio Bi-Directional Media Stream WebSocket integration.

Simulates:
1. Inbound Twilio Webhook POST -> verifies TwiML <Connect><Stream> response.
2. WebSocket connection to /api/telephony/media-stream.
3. Sends 'connected' and 'start' events with DID + caller ID.
4. Verifies server responds with initial greeting 'media' packets (8kHz mu-law).
"""

import base64
import json
import socket
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from agent.twilio_stream import compute_ws_accept, build_ws_frame, read_ws_frame


def twilio_signature(url: str, params: dict) -> str:
    """What Twilio puts in X-Twilio-Signature: HMAC-SHA1(auth token, url + sorted params)."""
    import hashlib, hmac
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not token:
        for line in open(os.path.join(os.path.dirname(__file__), "..", ".env")):
            if line.startswith("TWILIO_AUTH_TOKEN="):
                token = line.split("=", 1)[1].strip()
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def test_inbound_twiml(base_url: str) -> str:
    url = f"{base_url}/api/telephony/voice/inbound"
    print(f"--> 1. Testing Inbound Voice Webhook POST on {url}")
    params = {"Called": "+19517177889", "Caller": "+15551234567", "CallSid": "CA_test_001"}
    # The server verifies X-Twilio-Signature (unless TWILIO_VALIDATE_SIGNATURE=0); sign like Twilio does.
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(params).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Twilio-Signature": twilio_signature(url, params)},
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        body = resp.read().decode("utf-8")
        print("   <-- TwiML Response:\n", body.strip())
        assert "<Connect>" in body and "<Stream" in body, "TwiML missing <Connect><Stream>"
        assert "/api/telephony/media-stream" in body, "TwiML missing media-stream URL"
        print("   [OK] TwiML properly returned <Connect><Stream>!\n")
        return body


def test_websocket_stream(host: str, port: int, ws_path: str = "/api/telephony/media-stream", token: str = "", expect_reject: bool = False) -> None:
    print(f"--> 2. Testing WebSocket Handshake to ws://{host}:{port}{ws_path}" + (" (bad token, expecting rejection)" if expect_reject else ""))
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect((host, port))

    sec_key = base64.b64encode(b"0123456789abcdef").decode()
    upgrade_req = (
        f"GET {ws_path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {sec_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(upgrade_req.encode("latin1"))

    # Read handshake response
    resp_header = b""
    while b"\r\n\r\n" not in resp_header:
        chunk = s.recv(1024)
        if not chunk:
            break
        resp_header += chunk

    print("   <-- Handshake Status:\n", resp_header.decode("latin1").split("\r\n\r\n")[0])
    assert b"101 Switching Protocols" in resp_header, "WebSocket upgrade failed"
    print("   [OK] WebSocket upgraded successfully (101 Switching Protocols)!\n")

    rfile = s.makefile("rb")

    # Send 'connected'
    print("--> 3. Sending 'connected' event...")
    s.sendall(build_ws_frame(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})))

    # Send 'start'
    print("--> 4. Sending 'start' event with DID: +19517177889 ...")
    start_payload = {
        "event": "start",
        "sequenceNumber": "1",
        "start": {
            "streamSid": "MZ_test_mock_stream_123",
            "accountSid": "AC_mock_acc_123",
            "callSid": "CA_mock_call_123",
            "tracks": ["inbound"],
            # Twilio strips query strings from the <Stream> url, so the server's token travels as a
            # custom parameter and is checked on this "start" event.
            "customParameters": {
                "called": "+19517177889",
                "caller": "+15551234567",
                "token": "bad-token" if expect_reject else token,
            },
            "mediaFormat": {
                "encoding": "audio/x-mulaw",
                "sampleRate": 8000,
                "channels": 1,
            },
        },
    }
    s.sendall(build_ws_frame(json.dumps(start_payload)))

    import select
    s.settimeout(None)
    media_count = 0
    start_wait = time.time()
    print("--> 5. Waiting for Agent's initial greeting audio packets ('media' events)...")
    while time.time() - start_wait < (4.0 if expect_reject else 15.0):
        r, _, _ = select.select([s], [], [], 1.0)
        if not r:
            continue
        opcode, payload = read_ws_frame(rfile)
        if opcode == 0x8:
            break
        if opcode == 0x1:
            evt = json.loads(payload.decode("utf-8", "replace"))
            evt_type = evt.get("event")
            if evt_type == "media":
                media_count += 1
                if media_count == 1:
                    chunk_len = len(base64.b64decode(evt.get("media", {}).get("payload", "")))
                    print(f"   <-- Received First Audio Packet! Size: {chunk_len} bytes mu-law (expected: 160)")
                if media_count >= 10:
                    break

    if expect_reject:
        assert media_count == 0, "Server streamed audio despite a bad token"
        print("   [OK] Stream with a bad token got no audio (rejected on 'start').\n")
        s.close()
        return
    print(f"   [OK] Received {media_count} audio packets from Agent!")
    assert media_count > 0, "No audio packets received from Agent greeting"

    # Send 'stop'
    print("--> 6. Sending 'stop' event (Call ended)...")
    s.sendall(build_ws_frame(json.dumps({
        "event": "stop",
        "sequenceNumber": "99",
        "stop": {"callSid": "CA_mock_call_123"},
    })))
    time.sleep(0.5)
    s.close()
    print("\n[SUCCESS] Twilio Media Stream Direct Connection Test PASSED!\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
    host = "127.0.0.1"
    twiml = test_inbound_twiml(f"http://{host}:{port}")
    # The TwiML carries the per-deployment token as a <Parameter>; the stream must present it on "start".
    m = re.search(r'<Parameter name="token" value="([^"]*)"', twiml)
    token = m.group(1) if m else ""
    if token:
        test_websocket_stream(host, port, token="", expect_reject=True)
    test_websocket_stream(host, port, token=token)
