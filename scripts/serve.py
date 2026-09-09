"""
Local test harness for task 1.1.

Serves the browser test page and mints join tokens for it. Stdlib only —
no pip install. This is a development harness, not the production token
path: in production Drupal mints the token (see BACKEND-FRONTEND-STACK).
"""

import asyncio
import json
import os
import re
import sys
import time
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
from agent.pipeline_worker import pipeline_worker
from agent.webhook_dispatcher import webhook_dispatcher, WebhookEvent
from agent.agent_builder import agent_builder
from agent.call_history import call_history
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

    def _send_audio(self, call_id: str):
        """Streams a call recording, honouring Range requests so the player can seek."""
        path = call_history.audio_path(call_id)
        if not path:
            self._send_json({"status": "error", "error": "No audio for this call"}, 404)
            return

        header = self.headers.get("Range", "")
        match = re.match(r"bytes=(\d*)-(\d*)", header) if header else None
        if match:
            start = int(match.group(1)) if match.group(1) else 0
            end = int(match.group(2)) if match.group(2) else None
            chunk, start, end, total = call_history.read_audio_range(call_id, start, end)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        else:
            chunk, start, end, total = call_history.read_audio_range(call_id)
            self.send_response(200)

        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

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
        elif parsed.path == "/api/pipeline/jobs":
            q = parse_qs(parsed.query)
            status_filter = (q.get("status") or [""])[0] or None
            limit = int((q.get("limit") or ["50"])[0])
            self._send_json({"status": "ok", "jobs": pipeline_worker.list_jobs(status=status_filter, limit=limit)})
            return
        elif parsed.path == "/api/pipeline/job":
            q = parse_qs(parsed.query)
            job_id = (q.get("job_id") or [""])[0] or None
            call_id = (q.get("call_id") or [""])[0] or None
            job = pipeline_worker.get_job(job_id=job_id, call_id=call_id)
            self._send_json({"status": "ok", "job": job.to_dict() if job else None})
            return
        elif parsed.path == "/api/pipeline/stats":
            self._send_json({"status": "ok", "stats": pipeline_worker.get_stats()})
            return
        elif parsed.path == "/api/pipeline/analytics":
            q = parse_qs(parsed.query)
            job_id = (q.get("job_id") or [""])[0] or None
            call_id = (q.get("call_id") or [""])[0] or None
            job = pipeline_worker.get_job(job_id=job_id, call_id=call_id)
            if not job:
                self._send_json({"status": "error", "error": "Job not found"}, 404)
                return
            sentiment = job.metadata.get("sentiment")
            summary = job.metadata.get("summary")
            self._send_json({
                "status": "ok",
                "call_id": job.call_id,
                "job_id": job.job_id,
                "sentiment": sentiment,
                "summary": summary,
            })
            return

        elif parsed.path == "/api/pipeline/extraction":
            q = parse_qs(parsed.query)
            job_id  = (q.get("job_id")  or [""])[0] or None
            call_id = (q.get("call_id") or [""])[0] or None
            job = pipeline_worker.get_job(job_id=job_id, call_id=call_id)
            if not job:
                self._send_json({"status": "error", "error": "Job not found"}, 404)
                return
            self._send_json({
                "status": "ok",
                "call_id": job.call_id,
                "job_id": job.job_id,
                "extractions": job.metadata.get("extractions"),
                "crm_payloads": job.metadata.get("crm_payloads"),
            })
            return
        elif parsed.path == "/api/extraction/schemas":
            from agent.schema_extractor import list_schemas, get_schema
            q = parse_qs(parsed.query)
            schema_id = (q.get("id") or [""])[0] or None
            if schema_id:
                schema = get_schema(schema_id)
                if not schema:
                    self._send_json({"status": "error", "error": f"Schema '{schema_id}' not found"}, 404)
                    return
                self._send_json({"status": "ok", "schema_id": schema_id, "schema": schema})
            else:
                self._send_json({"status": "ok", "schemas": list_schemas()})
            return

        elif parsed.path == "/api/webhooks/endpoints":
            self._send_json({"status": "ok", "endpoints": webhook_dispatcher.list_endpoints()})
            return
        elif parsed.path == "/api/webhooks/deliveries":
            q = parse_qs(parsed.query)
            self._send_json({
                "status": "ok",
                "deliveries": webhook_dispatcher.list_deliveries(
                    endpoint_id=(q.get("endpoint_id") or [""])[0] or None,
                    event=(q.get("event") or [""])[0] or None,
                    status=(q.get("delivery_status") or [""])[0] or None,
                    call_id=(q.get("call_id") or [""])[0] or None,
                    limit=int((q.get("limit") or ["50"])[0]),
                ),
                "dead_letters": webhook_dispatcher.list_dead_letters(limit=20),
            })
            return
        elif parsed.path == "/api/webhooks/stats":
            self._send_json({
                "status": "ok",
                "stats": webhook_dispatcher.get_stats(),
                "events": [e.value for e in WebhookEvent],
            })
            return

        elif parsed.path == "/api/agents":
            active = agent_builder.get_active_agent()
            self._send_json({
                "status": "ok",
                "agents": agent_builder.list_agents(),
                "active_agent": active.agent_id if active else None,
            })
            return
        elif parsed.path == "/api/agents/get":
            q = parse_qs(parsed.query)
            cfg = agent_builder.get_agent((q.get("agent_id") or [""])[0])
            if not cfg:
                self._send_json({"status": "error", "error": "Agent not found"}, 404)
                return
            self._send_json({
                "status": "ok",
                "agent": cfg.to_dict(),
                "lint": agent_builder.lint_prompt(cfg.system_prompt),
            })
            return
        elif parsed.path == "/api/agents/voices":
            self._send_json({
                "status": "ok",
                "voices": agent_builder.list_voices(),
                "models": agent_builder.list_models(),
            })
            return
        elif parsed.path == "/api/agents/tools":
            self._send_json({"status": "ok", "tools": agent_builder.available_tools()})
            return
        elif parsed.path == "/api/agents/presets":
            self._send_json({"status": "ok", "presets": agent_builder.list_presets()})
            return
        elif parsed.path == "/api/agents/revisions":
            q = parse_qs(parsed.query)
            agent_id = (q.get("agent_id") or [""])[0]
            if not agent_builder.get_agent(agent_id):
                self._send_json({"status": "error", "error": "Agent not found"}, 404)
                return
            body = {"status": "ok", "revisions": agent_builder.list_revisions(agent_id)}
            from_rev, to_rev = (q.get("from") or [""])[0], (q.get("to") or [""])[0]
            if from_rev and to_rev:
                try:
                    body["diff"] = agent_builder.diff_revisions(agent_id, int(from_rev), int(to_rev))
                except (KeyError, ValueError) as e:
                    body["diff_error"] = str(e)
            self._send_json(body)
            return
        elif parsed.path == "/api/agents/stats":
            self._send_json({"status": "ok", "stats": agent_builder.get_stats()})
            return

        elif parsed.path == "/api/calls":
            q = parse_qs(parsed.query)
            def _opt(name, cast=str):
                raw = (q.get(name) or [""])[0]
                if raw == "":
                    return None
                try:
                    return cast(raw)
                except ValueError:
                    return None
            self._send_json({
                "status": "ok",
                **call_history.list_calls(
                    page=int((q.get("page") or ["1"])[0] or 1),
                    page_size=int((q.get("page_size") or ["25"])[0] or 25),
                    sentiment=_opt("sentiment"),
                    agent=_opt("agent"),
                    outcome=_opt("outcome"),
                    direction=_opt("direction"),
                    min_duration=_opt("min_duration", float),
                    max_duration=_opt("max_duration", float),
                    transferred=(None if _opt("transferred") is None
                                 else _opt("transferred") == "true"),
                    has_audio=(None if _opt("has_audio") is None
                               else _opt("has_audio") == "true"),
                    search=_opt("q"),
                    sort=(q.get("sort") or ["started_at"])[0],
                    order=(q.get("order") or ["desc"])[0],
                ),
            })
            return
        elif parsed.path == "/api/calls/detail":
            q = parse_qs(parsed.query)
            detail = call_history.get_call((q.get("call_id") or [""])[0])
            if not detail:
                self._send_json({"status": "error", "error": "Call not found"}, 404)
                return
            self._send_json({"status": "ok", **detail})
            return
        elif parsed.path == "/api/calls/waveform":
            q = parse_qs(parsed.query)
            wave_data = call_history.waveform(
                (q.get("call_id") or [""])[0],
                buckets=int((q.get("buckets") or ["240"])[0] or 240))
            if not wave_data:
                self._send_json({"status": "error", "error": "No audio for this call"}, 404)
                return
            self._send_json({"status": "ok", "waveform": wave_data})
            return
        elif parsed.path == "/api/calls/audio":
            q = parse_qs(parsed.query)
            self._send_audio((q.get("call_id") or [""])[0])
            return
        elif parsed.path == "/api/calls/stats":
            self._send_json({"status": "ok", "stats": call_history.stats()})
            return
        elif parsed.path == "/api/calls/export":
            q = parse_qs(parsed.query)
            csv_body = call_history.export_csv(
                sentiment=(q.get("sentiment") or [""])[0] or None,
                agent=(q.get("agent") or [""])[0] or None,
                outcome=(q.get("outcome") or [""])[0] or None,
                search=(q.get("q") or [""])[0] or None,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="call-history.csv"')
            self.send_header("Content-Length", str(len(csv_body)))
            self.end_headers()
            self.wfile.write(csv_body)
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

        elif parsed.path == "/api/pipeline/enqueue":
            call_id = payload.get("call_id", "").strip() or f"call-{int(time.time())}"
            room_name = payload.get("room_name")
            transcript_turns = payload.get("transcript_turns", [])
            audio_path = payload.get("audio_path")
            metadata = payload.get("metadata", {})
            priority = int(payload.get("priority", 5))
            execute_now = bool(payload.get("execute_now", True))

            job = pipeline_worker.enqueue_call(
                call_id=call_id,
                room_name=room_name,
                transcript_turns=transcript_turns,
                audio_path=audio_path,
                metadata=metadata,
                priority=priority,
            )
            if execute_now:
                loop = asyncio.new_event_loop()
                job = loop.run_until_complete(pipeline_worker.execute_job(job.job_id))
                loop.close()
            self._send_json({"status": "ok", "job": job.to_dict()})
            return

        elif parsed.path == "/api/pipeline/retry":
            job_id = payload.get("job_id", "").strip()
            execute_now = bool(payload.get("execute_now", True))
            job = pipeline_worker.retry_job(job_id)
            if not job:
                self._send_json({"status": "error", "error": f"Job '{job_id}' not found"}, 404)
                return
            if execute_now:
                loop = asyncio.new_event_loop()
                job = loop.run_until_complete(pipeline_worker.execute_job(job.job_id))
                loop.close()
            self._send_json({"status": "ok", "job": job.to_dict()})
            return

        elif parsed.path == "/api/pipeline/analyze":
            call_id = payload.get("call_id", f"analyze-{int(time.time())}")
            transcript_turns = payload.get("transcript_turns", [])
            metadata = payload.get("metadata", {})
            from agent.sentiment_analyzer import sentiment_analyzer
            result = sentiment_analyzer.analyze_and_summarize(
                call_id=call_id,
                transcript_turns=transcript_turns,
                metadata=metadata,
            )
            self._send_json({"status": "ok", "analytics": result})
            return

        elif parsed.path == "/api/extraction/extract":
            schema_id = payload.get("schema_id", "legal_intake")
            call_id = payload.get("call_id", f"extract-{int(time.time())}")
            transcript_turns = payload.get("transcript_turns", [])
            metadata = payload.get("metadata", {})
            from agent.schema_extractor import schema_extractor
            try:
                result = schema_extractor.extract(
                    schema_id=schema_id,
                    call_id=call_id,
                    transcript_turns=transcript_turns,
                    metadata=metadata,
                )
                self._send_json({
                    "status": "ok",
                    "extraction": result.to_dict(),
                    "crm_payload": result.to_crm_payload(),
                })
            except KeyError as e:
                self._send_json({"status": "error", "error": str(e)}, 404)
            return

        elif parsed.path == "/api/extraction/register":
            schema_id = payload.get("schema_id", "").strip()
            schema = payload.get("schema", {})
            if not schema_id or not schema:
                self._send_json({"status": "error", "error": "schema_id and schema required"}, 400)
                return
            from agent.schema_extractor import register_schema, list_schemas
            try:
                register_schema(schema_id, schema)
                self._send_json({"status": "ok", "registered": schema_id, "all_schemas": list_schemas()})
            except ValueError as e:
                self._send_json({"status": "error", "error": str(e)}, 400)
            return

        elif parsed.path == "/api/webhooks/register":
            url = (payload.get("url") or "").strip()
            if not url:
                self._send_json({"status": "error", "error": "url required"}, 400)
                return
            try:
                ep = webhook_dispatcher.register_endpoint(
                    url=url,
                    events=payload.get("events"),
                    secret=payload.get("secret"),
                    description=payload.get("description", ""),
                    max_attempts=int(payload.get("max_attempts", 4)),
                    timeout_s=float(payload.get("timeout_s", 10)),
                    headers=payload.get("headers"),
                )
            except ValueError as e:
                self._send_json({"status": "error", "error": str(e)}, 400)
                return
            # The plaintext secret is returned once, at registration time only.
            self._send_json({
                "status": "ok",
                "endpoint": ep.to_dict(redact_secret=False),
                "endpoints": webhook_dispatcher.list_endpoints(),
            })
            return

        elif parsed.path == "/api/webhooks/test":
            endpoint_id = payload.get("endpoint_id", "")
            ep = webhook_dispatcher.get_endpoint(endpoint_id)
            if not ep:
                self._send_json({"status": "error", "error": "Endpoint not found"}, 404)
                return
            event = payload.get("event", WebhookEvent.CALL_COMPLETED.value)
            body = payload.get("payload", {"test": True, "sent_at": time.time()})
            loop = asyncio.new_event_loop()
            try:
                record = loop.run_until_complete(
                    webhook_dispatcher.deliver(ep, event, body, payload.get("call_id"), sleep=False)
                )
            finally:
                loop.close()
            self._send_json({"status": "ok", "delivery": record.to_dict()})
            return

        elif parsed.path == "/api/webhooks/rotate":
            ep = webhook_dispatcher.rotate_secret(payload.get("endpoint_id", ""))
            if not ep:
                self._send_json({"status": "error", "error": "Endpoint not found"}, 404)
                return
            self._send_json({"status": "ok", "endpoint": ep.to_dict(redact_secret=False)})
            return

        elif parsed.path == "/api/webhooks/delete":
            removed = webhook_dispatcher.delete_endpoint(payload.get("endpoint_id", ""))
            self._send_json({"status": "ok" if removed else "error",
                             "removed": removed}, 200 if removed else 404)
            return

        elif parsed.path == "/api/webhooks/replay":
            loop = asyncio.new_event_loop()
            try:
                record = loop.run_until_complete(
                    webhook_dispatcher.replay_delivery(payload.get("delivery_id", ""))
                )
            finally:
                loop.close()
            if not record:
                self._send_json({"status": "error", "error": "Delivery not found"}, 404)
                return
            self._send_json({"status": "ok", "delivery": record.to_dict()})
            return

        elif parsed.path == "/api/agents/create":
            try:
                cfg = agent_builder.create_agent(
                    name=payload.get("name", ""),
                    first_message=payload.get("first_message", ""),
                    system_prompt=payload.get("system_prompt", ""),
                    **{k: v for k, v in payload.items()
                       if k not in ("name", "first_message", "system_prompt")},
                )
            except (ValueError, TypeError) as e:
                self._send_json({"status": "error", "error": str(e)}, 400)
                return
            self._send_json({"status": "ok", "agent": cfg.to_dict()})
            return

        elif parsed.path == "/api/agents/update":
            agent_id = payload.get("agent_id", "")
            try:
                cfg = agent_builder.update_agent(
                    agent_id, payload.get("changes", {}), payload.get("note", ""))
            except KeyError:
                self._send_json({"status": "error", "error": "Agent not found"}, 404)
                return
            except ValueError as e:
                self._send_json({"status": "error", "error": str(e)}, 400)
                return
            self._send_json({
                "status": "ok",
                "agent": cfg.to_dict(),
                "lint": agent_builder.lint_prompt(cfg.system_prompt),
            })
            return

        elif parsed.path == "/api/agents/clone":
            try:
                cfg = agent_builder.clone_agent(payload.get("agent_id", ""), payload.get("name"))
            except KeyError:
                self._send_json({"status": "error", "error": "Agent not found"}, 404)
                return
            self._send_json({"status": "ok", "agent": cfg.to_dict()})
            return

        elif parsed.path == "/api/agents/delete":
            removed = agent_builder.delete_agent(payload.get("agent_id", ""))
            self._send_json({"status": "ok" if removed else "error", "removed": removed},
                            200 if removed else 404)
            return

        elif parsed.path == "/api/agents/activate":
            try:
                cfg = agent_builder.set_active(payload.get("agent_id", ""))
            except KeyError:
                self._send_json({"status": "error", "error": "Agent not found"}, 404)
                return
            self._send_json({"status": "ok", "agent": cfg.to_dict()})
            return

        elif parsed.path == "/api/agents/rollback":
            try:
                cfg = agent_builder.rollback(payload.get("agent_id", ""), int(payload.get("revision", 0)))
            except KeyError as e:
                self._send_json({"status": "error", "error": str(e)}, 404)
                return
            except ValueError as e:
                self._send_json({"status": "error", "error": str(e)}, 400)
                return
            self._send_json({"status": "ok", "agent": cfg.to_dict()})
            return

        elif parsed.path == "/api/agents/test":
            try:
                result = agent_builder.test_run(
                    payload.get("agent_id", ""), payload.get("utterances", []))
            except KeyError:
                self._send_json({"status": "error", "error": "Agent not found"}, 404)
                return
            self._send_json({"status": "ok", "result": result})
            return

        elif parsed.path == "/api/agents/lint":
            prompt = payload.get("system_prompt", "")
            ok, errors = agent_builder.validate({
                "name": payload.get("name", "lint"),
                "first_message": payload.get("first_message", "lint"),
                "system_prompt": prompt,
                "temperature": payload.get("temperature", 0.7),
            })
            self._send_json({
                "status": "ok",
                "valid": ok,
                "errors": errors,
                "lint": agent_builder.lint_prompt(prompt),
            })
            return

        elif parsed.path == "/api/agents/voice-preview":
            try:
                preview = agent_builder.preview_voice(
                    payload.get("voice_id", ""), payload.get("text", ""))
            except KeyError as e:
                self._send_json({"status": "error", "error": str(e)}, 404)
                return
            self._send_json({"status": "ok", "preview": preview})
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
    webhook_dispatcher.attach_to_pipeline(pipeline_worker)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
except OSError as e:
    raise SystemExit(f"Port {PORT} is in use ({e}). Pass another: "
                     f"python3 scripts/serve.py 9090")
except KeyboardInterrupt:
    print("\nstopped")
