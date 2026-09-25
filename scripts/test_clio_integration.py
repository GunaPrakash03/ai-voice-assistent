#!/usr/bin/env python3
"""Test script for Voice Agent & Clio Integration.

Supports:
1. --mock (default): Simulates Clio Grow API server locally and verifies full payload serialization, headers, and response handling.
2. --live --token=<TOKEN>: Sends a real test intake lead to Clio Grow Inbox API.
"""

import argparse
import http.server
import json
import os
import sys
import threading
import time

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from agent.clio_connector import ClioConnector


class MockClioServer(http.server.BaseHTTPRequestHandler):
    received_payload = None
    received_headers = None

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8")
        MockClioServer.received_payload = json.loads(body)
        MockClioServer.received_headers = dict(self.headers)

        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {"status": "created", "inbox_lead_id": 99481, "message": "Lead received successfully"}
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, format, *args):
        pass  # suppress noisy stdout


def run_mock_test():
    print("=" * 65)
    print("RUNNING VOICE AGENT -> CLIO GROW MOCK INTEGRATION TEST")
    print("=" * 65)

    server = http.server.HTTPServer(("127.0.0.1", 18492), MockClioServer)
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    connector = ClioConnector(grow_inbox_token="test_mock_token_abc123", enabled=True)
    # Point directly to mock server
    import agent.clio_connector
    agent.clio_connector.CLIO_GROW_API_URL = "http://127.0.0.1:18492/api/v1/inbox_leads"

    extracted_test_fields = {
        "client_name": "Eleanor Vance",
        "contact_phone": "+1-555-019-2834",
        "contact_email": "eleanor.vance@example.com",
        "case_type": "personal_injury",
        "injury_date": "2026-09-15",
        "needs_urgent_consultation": True,
    }
    sample_transcript = (
        "Caller: Hi, my name is Eleanor Vance.\n"
        "Agent: Hello Eleanor, how can I help you today?\n"
        "Caller: I was in a car crash on September 15th and hurt my back."
    )

    print("\n[1/3] Formatting Clio Grow Inbox Payload...")
    payload = connector.format_grow_payload(
        extracted_fields=extracted_test_fields,
        transcript_text=sample_transcript,
        recording_url="https://storage.firm.com/recordings/call_test_01.wav",
    )
    print("Payload generated successfully:")
    print(json.dumps(payload, indent=2))

    print("\n[2/3] Dispatching to Clio API...")
    res = connector.send_lead_to_clio_grow(
        extracted_fields=extracted_test_fields,
        transcript_text=sample_transcript,
        recording_url="https://storage.firm.com/recordings/call_test_01.wav",
    )
    print("Dispatch result:", res)

    print("\n[3/3] Validating Received Payload on Server...")
    received = MockClioServer.received_payload
    headers = MockClioServer.received_headers

    assert received is not None, "Server did not receive payload"
    lead = received.get("inbox_lead", {})
    assert lead.get("first_name") == "Eleanor", f"Expected Eleanor, got {lead.get('first_name')}"
    assert lead.get("last_name") == "Vance", f"Expected Vance, got {lead.get('last_name')}"
    assert lead.get("phone") == "+1-555-019-2834", "Phone mismatch"
    assert "Bearer test_mock_token_abc123" in headers.get("Authorization", ""), "Auth header missing"

    print("\n" + "=" * 65)
    print("SUCCESS: 3/3 Mock Integration Verification Checks Passed!")
    print("=" * 65)


def run_live_test(token: str):
    print("=" * 65)
    print("RUNNING LIVE CLIO GROW API DISPATCH TEST")
    print("=" * 65)
    connector = ClioConnector(grow_inbox_token=token, enabled=True)

    extracted_test_fields = {
        "client_name": "Test Integration Caller",
        "contact_phone": "+1-555-019-0000",
        "contact_email": "test.voice.intake@example.com",
        "case_type": "general_intake",
        "injury_date": "2026-09-23",
        "needs_urgent_consultation": False,
    }
    sample_transcript = "Test automated voice intake dispatch to Clio Grow Inbox API."

    print("Posting lead to Clio Grow...")
    res = connector.send_lead_to_clio_grow(
        extracted_fields=extracted_test_fields,
        transcript_text=sample_transcript,
    )
    print("Live API Response:", res)
    if res.get("status") == "success":
        print("\nSUCCESS: Lead successfully created in your Clio Grow Inbox!")
    else:
        print("\nFAILURE: Could not create lead. Check token and error message above.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Clio Integration")
    parser.add_argument("--live", action="store_true", help="Run live test against Clio API")
    parser.add_argument("--token", type=str, help="Clio Grow Inbox API Token")
    args = parser.parse_args()

    if args.live:
        tok = args.token or os.getenv("CLIO_GROW_INBOX_TOKEN")
        if not tok:
            print("Error: --token or CLIO_GROW_INBOX_TOKEN environment variable required for live test.")
            sys.exit(1)
        run_live_test(tok)
    else:
        run_mock_test()
