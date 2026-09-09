"""
Local test harness for task 1.1.

Serves the browser test page and mints join tokens for it. Stdlib only —
no pip install. This is a development harness, not the production token
path: in production Drupal mints the token (see BACKEND-FRONTEND-STACK).
"""

import asyncio
import json
import os
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from agent.token import join_token  # noqa: E402

# Default port is 8091; override with:
#   python3 scripts/serve.py <port>
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
WEB = os.path.join(ROOT, "web")


def env(name):
    for line in open(os.path.join(ROOT, ".env")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} missing from .env")


KEY, SECRET = env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET")
WS_URL = env("LIVEKIT_URL")


from agent.telephony_manager import telephony_manager, asdict, normalize_phone_number
from agent.transfer_manager import transfer_manager, TransferMode
from agent.dtmf_manager import dtmf_manager
from agent.amd_manager import AMDManager, AMDState, AMDAction, VoicemailDropConfig
from agent.recording_manager import recording_manager, RecordingConfig, ComplianceMode
amd_manager = AMDManager()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/token":
            q = parse_qs(parsed.query)
            room = (q.get("room") or ["test-room"])[0]
            identity = (q.get("identity") or q.get("user") or ["caller"])[0]

            self._send_json({
                "url": WS_URL,
                "room": room,
                "identity": identity,
                "token": join_token(KEY, SECRET, room, identity),
            })
            return
        elif parsed.path == "/api/telephony/trunks":
            self._send_json({
                "inbound": telephony_manager.list_inbound_trunks(),
                "outbound": telephony_manager.list_outbound_trunks(),
                "rules": telephony_manager.list_dispatch_rules(),
            })
            return
        elif parsed.path == "/api/telephony/calls":
            self._send_json({
                "calls": telephony_manager.list_calls(),
            })
            return
        elif parsed.path == "/api/telephony/transfers":
            self._send_json({
                "transfers": transfer_manager.list_transfers(),
            })
            return
        elif parsed.path == "/api/telephony/hold":
            q = parse_qs(parsed.query)
            call_id = (q.get("call_id") or [""])[0]
            self._send_json({
                "hold": transfer_manager.get_hold_state(call_id) if call_id else None,
            })
            return
        elif parsed.path == "/api/telephony/ivr":
            q = parse_qs(parsed.query)
            call_id = (q.get("call_id") or [""])[0]
            if call_id:
                self._send_json({"status": "ok", "state": dtmf_manager.get_call_state(call_id)})
            else:
                self._send_json({"status": "ok", "menus": dtmf_manager.list_menus()})
            return
        elif parsed.path == "/api/telephony/amd":
            q = parse_qs(parsed.query)
            call_id = (q.get("call_id") or [""])[0]
            if call_id:
                session = amd_manager.get_session(call_id)
                self._send_json({"status": "ok", "amd": session.to_result().dict() if session else None})
            else:
                self._send_json({"status": "ok", "default_config": amd_manager.default_config.dict()})
            return
        elif parsed.path == "/api/telephony/recordings":
            self._send_json({"status": "ok", "recordings": recording_manager.list_recordings()})
            return
        elif parsed.path == "/api/telephony/recording":
            q = parse_qs(parsed.query)
            call_id = (q.get("call_id") or [""])[0]
            if call_id:
                s = recording_manager.get_session(call_id)
                self._send_json({"status": "ok", "recording": s.to_metadata().dict() if s else None})
            else:
                self._send_json({"status": "ok", "recordings": recording_manager.list_recordings()})
            return

        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            payload = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if parsed.path == "/api/telephony/dial":
            destination = payload.get("destination", "").strip()
            caller_id = payload.get("caller_id", "").strip() or None
            room_name = payload.get("room", "").strip() or None
            if not destination:
                self._send_json({"error": "Missing 'destination' phone number"}, 400)
                return
            try:
                loop = asyncio.new_event_loop()
                record = loop.run_until_complete(
                    telephony_manager.dial_phone_number(
                        destination_number=destination,
                        caller_id=caller_id,
                        room_name=room_name,
                    )
                )
                loop.close()
                self._send_json({"status": "ok", "call": asdict(record)})
            except Exception as err:
                self._send_json({"status": "error", "error": str(err)}, 500)
            return

        elif parsed.path == "/api/telephony/inbound/simulate":
            from_num = payload.get("from", "+15551234567")
            to_num = payload.get("to", "+18005550199")
            routed = telephony_manager.route_inbound_call(dialed_number=to_num, caller_number=from_num)
            if not routed:
                self._send_json({"status": "error", "error": "No matching route found"}, 404)
            else:
                self._send_json({"status": "ok", "routed": routed})
            return

        elif parsed.path == "/api/telephony/transfer":
            call_id = payload.get("call_id", "").strip() or "active-call"
            target_number = payload.get("target_number", "").strip() or payload.get("destination", "").strip()
            mode = str(payload.get("mode", payload.get("transfer_type", "blind"))).strip().lower()
            dept = payload.get("department")
            reason = payload.get("reason", "Caller request")
            caller_name = payload.get("caller_name", "Customer")
            inquiry = payload.get("inquiry", reason)

            if not target_number:
                self._send_json({"error": "Missing 'target_number' phone number"}, 400)
                return

            try:
                loop = asyncio.new_event_loop()
                if mode == "warm":
                    record = loop.run_until_complete(
                        transfer_manager.initiate_warm_transfer(
                            call_id=call_id,
                            target_number=target_number,
                            caller_name=caller_name,
                            caller_inquiry=inquiry,
                            department=dept,
                            reason=reason,
                        )
                    )
                else:
                    record = loop.run_until_complete(
                        transfer_manager.initiate_blind_transfer(
                            call_id=call_id,
                            target_number=target_number,
                            department=dept,
                            reason=reason,
                        )
                    )
                loop.close()
                self._send_json({"status": "ok", "transfer": asdict(record)})
            except Exception as err:
                self._send_json({"status": "error", "error": str(err)}, 500)
            return

        elif parsed.path == "/api/telephony/hold":
            call_id = payload.get("call_id", "").strip() or "active-call"
            hold = bool(payload.get("hold", True))
            reason = payload.get("reason", "manual_hold")
            if hold:
                state = transfer_manager.put_on_hold(call_id, reason=reason)
            else:
                state = transfer_manager.remove_from_hold(call_id)
            self._send_json({"status": "ok", "hold": asdict(state)})
            return

        elif parsed.path == "/api/telephony/dtmf":
            call_id = payload.get("call_id", "").strip() or "active-call"
            digit = str(payload.get("digit", "")).strip().upper()
            duration_ms = int(payload.get("duration_ms", 160))
            if not digit:
                self._send_json({"error": "Missing 'digit' parameter"}, 400)
                return
            result = dtmf_manager.process_dtmf_digit(
                call_id=call_id,
                digit=digit,
                duration_ms=duration_ms,
            )
            self._send_json({"status": "ok", "result": result})
            return

        elif parsed.path == "/api/telephony/ivr/reset":
            call_id = payload.get("call_id", "").strip() or "active-call"
            dtmf_manager.reset_call(call_id)
            self._send_json({"status": "ok", "state": dtmf_manager.get_call_state(call_id)})
            return

        elif parsed.path == "/api/telephony/amd/configure":
            call_id = payload.get("call_id", "").strip() or "active-call"
            cfg_kwargs = {}
            if "enabled" in payload:
                cfg_kwargs["enabled"] = bool(payload["enabled"])
            if "message" in payload:
                cfg_kwargs["message"] = str(payload["message"])
            if "action_on_machine" in payload:
                cfg_kwargs["action_on_machine"] = AMDAction(payload["action_on_machine"])
            if "beep_detection_enabled" in payload:
                cfg_kwargs["beep_detection_enabled"] = bool(payload["beep_detection_enabled"])
            cfg = VoicemailDropConfig(**cfg_kwargs)
            session = amd_manager.get_or_create_session(call_id, cfg)
            session.config = cfg
            self._send_json({"status": "ok", "config": cfg.dict()})
            return

        elif parsed.path == "/api/telephony/amd/simulate":
            call_id = payload.get("call_id", "").strip() or "active-call"
            ev_type = payload.get("event_type", "machine_greeting")
            session = amd_manager.get_or_create_session(call_id)
            if ev_type == "human_greeting":
                session.state = AMDState.HUMAN
                session.confidence = 0.92
                session.reason = "Simulated short human greeting ('Hello?')"
                session.total_speech_duration = 1.1
            elif ev_type == "machine_greeting":
                session.state = AMDState.MACHINE_GREETING
                session.confidence = 0.95
                session.reason = "Simulated voicemail greeting ('Please leave a message after the tone...')"
                session.total_speech_duration = 4.8
            elif ev_type == "voicemail_beep":
                session.state = AMDState.VOICEMAIL_BEEP
                session.beep_detected = True
                session.confidence = 0.99
                session.reason = "Simulated 1000 Hz recording beep detected"
            elif ev_type == "transcript":
                text = payload.get("text", "Please leave a message after the tone")
                amd_res = amd_manager.process_transcript(call_id, text)
                self._send_json({"status": "ok", "amd": amd_res.dict()})
                return
            self._send_json({"status": "ok", "amd": session.to_result().dict()})
            return

        elif parsed.path == "/api/telephony/voicemail-drop":
            call_id = payload.get("call_id", "").strip() or "active-call"
            message = payload.get("message")
            res = amd_manager.trigger_voicemail_drop(call_id, custom_message=message)
            self._send_json({"status": "ok", "drop": res})
            return

        elif parsed.path == "/api/telephony/recording/start":
            call_id = payload.get("call_id", "").strip() or "active-call"
            cm_str = payload.get("compliance_mode", "two_party")
            try:
                cm = ComplianceMode(cm_str)
            except Exception:
                cm = ComplianceMode.TWO_PARTY
            cfg = RecordingConfig(
                compliance_mode=cm,
                beep_on_start=bool(payload.get("beep_on_start", True)),
                redact_on_pause=bool(payload.get("redact_on_pause", True)),
            )
            meta = recording_manager.start_recording(call_id, config=cfg)
            self._send_json({"status": "ok", "recording": meta.dict()})
            return

        elif parsed.path == "/api/telephony/recording/pause":
            call_id = payload.get("call_id", "").strip() or "active-call"
            reason = payload.get("reason", "pci_compliance")
            meta = recording_manager.pause_recording(call_id, reason=reason)
            self._send_json({"status": "ok", "recording": meta.dict()})
            return

        elif parsed.path == "/api/telephony/recording/resume":
            call_id = payload.get("call_id", "").strip() or "active-call"
            meta = recording_manager.resume_recording(call_id)
            self._send_json({"status": "ok", "recording": meta.dict()})
            return

        elif parsed.path == "/api/telephony/recording/stop":
            call_id = payload.get("call_id", "").strip() or "active-call"
            meta = recording_manager.stop_recording(call_id)
            self._send_json({"status": "ok", "recording": meta.dict()})
            return

        self.send_error(404, "Endpoint not found")

    def log_message(self, fmt, *args):
        first_arg = str(args[0]) if args else ""
        if "/token" in first_arg:
            sys.stderr.write("  token issued\n")


print(f"Test page:  http://localhost:{PORT}")
print(f"Signalling: {WS_URL}")
print("Ctrl+C to stop\n")
try:
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
except OSError as e:
    raise SystemExit(f"Port {PORT} is in use ({e}). Pass another: "
                     f"python3 scripts/serve.py 9090")
except KeyboardInterrupt:
    print("\nstopped")
