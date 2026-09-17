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
import logging
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
from urllib.parse import urlparse, parse_qs


ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from agent.token import join_token  # noqa: E402
from agent.provider_manager import provider_manager as _pm  # noqa: E402  (loads .env into os.environ)
from agent import storage as _storage  # noqa: E402
_storage.bootstrap("dashboard server")   # restores JSON/recordings from PostgreSQL before managers load them
log = logging.getLogger("serve")

# Default port is 8091; override with:
#   python3 scripts/serve.py <port>
# Railway (and other PaaS) inject PORT; an explicit argument still wins.
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.getenv("PORT", "8091"))
WEB = os.path.join(ROOT, "web")


def env(name):
    """LiveKit credentials: the process environment first (Railway variables), then .env for local dev."""
    val = (os.getenv(name) or "").strip()
    if val:
        return val
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} missing from the environment and .env")


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
from agent.auth_manager import auth_manager, ApiScope, UserRole
amd_manager = AMDManager()



def run_pipeline_job(job_id: str):
    """Executes a post-call job and lets the webhook deliveries it schedules finish before the loop closes.

    Webhook dispatch is fire-and-forget on the running loop; closing the loop straight after
    execute_job() silently dropped every call.completed delivery.
    """
    loop = asyncio.new_event_loop()
    try:
        job = loop.run_until_complete(pipeline_worker.execute_job(job_id))
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=45))
        return job
    finally:
        loop.close()

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def _send_json(self, data: dict, status: int = 200, extra_headers: Optional[Dict[str, str]] = None):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    # ── Browser sessions (login page) ───────────────────────────────────────
    SESSION_COOKIE = "va_session"
    PUBLIC_PATHS = ("/login", "/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/auth/session",
                    "/api/v1/auth/setup", "/api/v1/auth/otp/resend", "/api/v1/auth/otp/verify", "/api/v1/auth/dev-session",
                    "/api/v1/health", "/favicon.ico")
    STATIC_SUFFIXES = (".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".mp3", ".wav", ".map", ".d.ts")
    # Pages have clean URLs: /api-keys serves web/api-keys.html. Anything else (/api/..., /token, files
    # with an extension) is not a page.
    PAGE_RE = re.compile(r"^/[a-z0-9-]+$")

    # The Call Desk is one page with several views; each sidebar item has its own path so the links
    # are real links. /sip-trunks/<sub> selects a telephony sub-tab. (call-desk.html, the older copy,
    # gets the same views under /call-desk/…)
    DESK_VIEWS = ("calls", "call-detail", "sip-trunks", "phone-numbers", "agents")

    def _desk_view_file(self, path: str) -> Optional[str]:
        for prefix, page in (("", "index.html"), ("/call-desk", "call-desk.html")):
            rest = path[len(prefix):] if path.startswith(prefix) else None
            if rest is None or not rest.startswith("/"):
                continue
            view = rest[1:].split("/", 1)[0]
            if view in self.DESK_VIEWS and (rest == "/" + view or rest.startswith("/sip-trunks/")):
                return os.path.join(WEB, page)
        return None

    def _page_file(self, path: str) -> Optional[str]:
        """web/<name>.html behind a clean page URL, or None when the URL is not a page."""
        desk = self._desk_view_file(path)
        if desk:
            return desk
        if not self.PAGE_RE.match(path):
            return None
        f = os.path.join(WEB, path[1:] + ".html")
        return f if os.path.isfile(f) else None

    def _session_token(self) -> str:
        raw = self.headers.get("Cookie", "") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == self.SESSION_COOKIE:
                return v.strip()
        return ""

    def _session_user(self):
        return auth_manager.session_user(self._session_token())

    def _is_loopback(self) -> bool:
        return (self.client_address[0] if self.client_address else "") in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _require_session(self, parsed) -> bool:
        """Login gate. Returns True when the request may proceed (a response was sent otherwise).

        Pages redirect to /login; JSON routes get 401. Requests from this machine (verify
        suites, curl) may use the API without a session unless AUTH_TRUST_LOOPBACK=0.
        """
        path = parsed.path
        if path in self.PUBLIC_PATHS or path.endswith(self.STATIC_SUFFIXES) or path.startswith("/audio/"):
            return True
        if self._session_user():
            return True
        is_page = path == "/" or path.endswith("/") or self._page_file(path) is not None
        if is_page:
            nxt = self.path if self.path != "/" else ""
            self.send_response(302)
            self.send_header("Location", "/login" + (("?next=" + urllib.parse.quote(nxt, safe="")) if nxt else ""))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return False
        if self._is_loopback() and os.getenv("AUTH_TRUST_LOOPBACK", "1") != "0":
            return True
        self._send_json({"status": "error", "error": "Sign in required", "login": "/login"}, 401)
        return False

    # Two roles: Product Admin (stored role "admin") and User. A User's dashboard is Overview,
    # Calls, Call detail and their own Profile — everything else (agents, telephony, webhooks,
    # provider keys, member management, guides) is Product-Admin-only. Reads that those allowed
    # pages themselves depend on at boot (e.g. Overview's live-call banner, the nav's agent/DID
    # counts) stay unrestricted on GET; only mutating calls and full-page navigation are gated,
    # so a hidden sidebar link can't be worked around with a direct API call or typed URL.
    ADMIN_GET_PREFIXES = ("/api/providers", "/api/v1/users", "/api/v1/api-keys", "/api/v1/workspaces")
    ADMIN_POST_PREFIXES = ("/api/providers", "/api/v1/users", "/api/v1/api-keys", "/api/v1/workspaces",
                           "/api/v1/auth/users", "/api/agents", "/api/telephony", "/api/webhooks")
    ADMIN_PAGES = ("/api-keys", "/admin-guide", "/cost-comparison", "/agent-builder", "/webhooks",
                   "/competitor-analysis", "/user-guide", "/agents", "/sip-trunks", "/phone-numbers",
                   "/call-desk/agents", "/call-desk/sip-trunks", "/call-desk/phone-numbers")

    def _viewer(self) -> Dict[str, Any]:
        """Who is looking. Agents and calls are shared workspace-wide, so owner is always None
        (unrestricted); is_admin only gates the admin-only routes and pages above."""
        u = self._session_user()
        if u is None:
            return {"user": None, "is_admin": True, "owner": None, "agents": None}
        return {"user": u, "is_admin": u.role == "admin", "owner": None, "agents": None}

    def _enforce_role(self, parsed, method: str) -> bool:
        """Admin-only routes/pages. Returns False after sending a response when access is denied."""
        v = self._viewer()
        if v["is_admin"]:
            return True
        path = parsed.path
        if path.startswith(self.ADMIN_PAGES):
            self.send_response(302)
            self.send_header("Location", "/?denied=admin")
            self.end_headers()
            return False
        prefixes = self.ADMIN_POST_PREFIXES if method == "POST" else self.ADMIN_GET_PREFIXES
        if path.startswith(prefixes):
            self._send_json({"status": "error", "error": "Admin access required"}, 403)
            return False
        return True

    def _agent_allowed(self, agent_id: str) -> bool:
        v = self._viewer()
        if agent_builder.can_access(agent_id, v["owner"]):
            return True
        self._send_json({"status": "error", "error": "Agent not found"}, 404)   # do not reveal other users' agents
        return False

    def _set_session_cookie(self, token: str, max_age: int) -> Dict[str, str]:
        attrs = f"{self.SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={int(max_age)}"
        if self.headers.get("X-Forwarded-Proto", "") == "https":
            attrs += "; Secure"
        return {"Set-Cookie": attrs}

    def _extract_headers(self) -> Dict[str, str]:
        return {k: self.headers.get(k, "") for k in self.headers.keys()}

    def _auth_err_code(self, err: Optional[str]) -> int:
        err_str = str(err).lower()
        if "forbidden" in err_str or "scope" in err_str:
            return 403
        if "rate limit" in err_str:
            return 429
        return 401



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
        try:
            self.end_headers()
            self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # LiveKit Cloud SIP domain; a call's TwiML bridges the caller here so LiveKit's SIP service
    # answers and dispatches the agent. Overridable via env if the project is renamed.
    LIVEKIT_SIP_DOMAIN = os.getenv("LIVEKIT_SIP_DOMAIN", "voice-agent-kuxp4ocs.sip.livekit.cloud")

    def _maybe_twiml_webhook(self, parsed) -> bool:
        """Twilio Programmable Voice webhook for inbound PSTN calls. Twilio POSTs here (no session);
        we return TwiML that dials the LiveKit Cloud SIP URI, so the call reaches the agent without
        Elastic SIP Trunk origination. This mirrors how Retell connects a Twilio number and works on
        trial accounts. Returns True when it handled the request."""
        if parsed.path != "/api/telephony/voice/inbound":
            return False
        # Which DID was dialled — Twilio sends "To"/"Called"; default to our number.
        params = {}
        try:
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                params = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
            params.update({k: v[0] for k, v in parse_qs(parsed.query).items()})
        except Exception:
            params = {}
        dialed = (params.get("To") or params.get("Called") or "+19517177889").strip()
        dialed = re.sub(r"[^\d+]", "", dialed) or "+19517177889"
        sip_uri = f"sip:{dialed}@{self.LIVEKIT_SIP_DOMAIN}"
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response><Dial answerOnBridge="true" timeout="30">'
            f'<Sip>{sip_uri}</Sip>'
            '</Dial></Response>'
        )
        body = twiml.encode("utf-8")
        log.info("Twilio voice webhook: dialled=%s -> %s", dialed, sip_uri)
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        if self._maybe_twiml_webhook(parsed):
            return
        if parsed.path.endswith(".html"):
            # Old bookmarks and links: /api-keys.html → /api-keys (permanent, so browsers update).
            clean = "/" if parsed.path == "/index.html" else parsed.path[:-len(".html")]
            self.send_response(301)
            self.send_header("Location", clean + (("?" + parsed.query) if parsed.query else ""))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if not self._require_session(parsed) or not self._enforce_role(parsed, "GET"):
            return
        if parsed.path == "/api/v1/auth/session":
            self._send_json({"status": "ok", **auth_manager.session_info(self._session_token())})
            return
        if parsed.path == "/token":
            q = parse_qs(parsed.query)
            room = (q.get("room") or ["test-room"])[0]
            identity = (q.get("identity") or q.get("user") or ["caller"])[0]

            host_header = self.headers.get("Host", "localhost").split(":")[0]
            if "ngrok" in host_header:
                ws_endpoint = "ws://10.149.107.174:7880"
            elif host_header not in ("localhost", "127.0.0.1") and WS_URL.startswith("ws://127.0.0.1"):
                ws_endpoint = f"ws://{host_header}:7880"
            else:
                ws_endpoint = WS_URL

            self._send_json({
                "url": ws_endpoint,
                "room": room,
                "identity": identity,
                "token": join_token(KEY, SECRET, room, identity),
            })
            return
        elif parsed.path == "/api/telephony/trunks":
            self._send_json({
                "trunks": telephony_manager.list_trunks(),
                "inbound": telephony_manager.list_inbound_trunks(),
                "outbound": telephony_manager.list_outbound_trunks(),
                "rules": telephony_manager.list_dispatch_rules(),
            })
            return
        elif parsed.path == "/api/telephony/numbers":
            self._send_json({
                "status": "ok",
                "numbers": telephony_manager.list_owned_numbers(),
                "total": len(telephony_manager.list_owned_numbers()),
            })
            return
        elif parsed.path == "/api/telephony/numbers/available":
            q = parse_qs(parsed.query)
            country = (q.get("country") or [""])[0] or None
            search = (q.get("search") or q.get("q") or [""])[0] or None
            carrier = (q.get("carrier") or [""])[0] or None
            self._send_json({
                "status": "ok",
                "carrier": carrier or "all",
                "carriers": telephony_manager.list_carriers(),
                "available": telephony_manager.list_available_numbers(country=country, search=search, carrier=carrier),
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
            viewer = self._viewer()
            active = agent_builder.get_active_agent()
            if active and not agent_builder.can_access(active.agent_id, viewer["owner"]):
                active = None
            gemini = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
            openai_key = bool(os.getenv("OPENAI_API_KEY"))
            self._send_json({
                "status": "ok",
                "agents": agent_builder.list_agents(viewer["owner"]),
                "active_agent": active.agent_id if active else None,
                "viewer": {"role": viewer["user"].role if viewer["user"] else "admin", "is_admin": viewer["is_admin"]},
                # What actually writes the words: the builder shows a notice when it is the rule script.
                "llm": {
                    "sandbox_backend": "gemini" if gemini else "script",
                    "live_backend": "gemini" if gemini else ("openai" if openai_key else "mock"),
                },
            })
            return
        elif parsed.path == "/api/agents/get":
            q = parse_qs(parsed.query)
            if not self._agent_allowed((q.get("agent_id") or [""])[0]):
                return
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
            vq = parse_qs(parsed.query)
            refresh = (vq.get("refresh") or ["0"])[0] in ("1", "true", "yes")
            from agent.voice_synthesizer import voice_engine_readiness
            voices = agent_builder.list_voices(refresh=refresh)
            for v in voices:
                r = voice_engine_readiness(v.get("voice_id", ""), v.get("provider", ""), v.get("gender", "female"))
                v["api_ready"] = bool(r["ready"])
                v["engine"] = r["engine"]
                v["api_note"] = r["note"]
            self._send_json({
                "status": "ok",
                "voices": voices,
                "models": agent_builder.list_models(),
            })
            return
        elif parsed.path == "/api/agents/voice-audio":
            q = parse_qs(parsed.query)
            voice_id = (q.get("voice_id") or [""])[0]
            custom_text = (q.get("text") or [""])[0].strip()
            if custom_text.lower() in ("undefined", "null", "none"):
                custom_text = ""
            custom_style = (q.get("style") or [""])[0]
            voice = agent_builder.get_voice(voice_id)
            if (q.get("stream") or ["0"])[0] in ("1", "true") and voice:
                # Progressive audio: bytes go out as the engine produces them, so the <audio>
                # element starts playing after the first few KB instead of after the whole clip.
                from agent.voice_synthesizer import stream_voice_audio, voice_engine_readiness
                ready = voice_engine_readiness(voice.voice_id, voice.provider, voice.gender)
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("X-Voice-Engine", str(ready.get("engine") or ""))
                self.send_header("X-Voice-Requested-Provider", voice.provider)
                if ready.get("note"):
                    self.send_header("X-Voice-Fallback-Reason", str(ready["note"]).encode("ascii", "replace").decode("ascii"))
                self.send_header("Access-Control-Expose-Headers", "X-Voice-Engine, X-Voice-Requested-Provider, X-Voice-Fallback-Reason")
                self.end_headers()
                try:
                    for chunk in stream_voice_audio(voice_id=voice.voice_id, name=voice.name, gender=voice.gender,
                                                    style=custom_style or voice.style, provider=voice.provider, text=custom_text):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            try:
                from agent.voice_synthesizer import get_voice_audio
                if voice:
                    audio_bytes = get_voice_audio(
                        voice_id=voice.voice_id,
                        name=voice.name,
                        gender=voice.gender,
                        style=custom_style or voice.style,
                        provider=voice.provider,
                        text=custom_text,
                    )
                else:
                    audio_bytes = get_voice_audio(
                        voice_id=voice_id or "default",
                        style=custom_style,
                        text=custom_text,
                    )
            except Exception as e:
                self._send_json({"status": "error", "error": str(e)}, 500)
                return

            c_type = "audio/mpeg" if (audio_bytes.startswith(b"\xff\xfb") or audio_bytes.startswith(b"\xff\xf3") or audio_bytes.startswith(b"ID3")) else "audio/wav"
            self.send_response(200)
            self.send_header("Content-Type", c_type)
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            # Tell the page which engine really spoke, so a silent fallback is never mistaken for the picked voice.
            from agent.voice_synthesizer import get_voice_engine_status
            eng = get_voice_engine_status(voice.voice_id if voice else (voice_id or "default"))
            self.send_header("X-Voice-Engine", eng.get("engine") or "unknown")
            self.send_header("X-Voice-Requested-Provider", (voice.provider if voice else "") or "")
            if eng.get("fallback_reason"):
                self.send_header("X-Voice-Fallback-Reason", eng["fallback_reason"].encode("ascii", "replace").decode("ascii"))
            self.send_header("Access-Control-Expose-Headers", "X-Voice-Engine, X-Voice-Requested-Provider, X-Voice-Fallback-Reason")
            self.end_headers()
            self.wfile.write(audio_bytes)
            return
        elif parsed.path == "/api/v1/auth/profile":
            su = self._session_user()
            self._send_json({"status": "ok", "profile": auth_manager.get_profile(su.user_id if su else None)})
            return
        elif parsed.path == "/api/storage/status":
            from agent import storage
            self._send_json({"status": "ok", "storage": storage.status()})
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
            if not self._agent_allowed(agent_id):
                return
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
        elif parsed.path == "/api/providers/keys":
            from agent.provider_manager import provider_manager
            self._send_json({"status": "ok", "providers": provider_manager.list_providers_status()})
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
                    visible_agents=self._viewer()["agents"],
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
            if not call_history.can_view((q.get("call_id") or [""])[0], self._viewer()["agents"]):
                self._send_json({"status": "error", "error": "Call not found"}, 404)
                return
            detail = call_history.get_call((q.get("call_id") or [""])[0])
            if not detail:
                self._send_json({"status": "error", "error": "Call not found"}, 404)
                return
            self._send_json({"status": "ok", **detail})
            return
        elif parsed.path == "/api/calls/waveform":
            q = parse_qs(parsed.query)
            if not call_history.can_view((q.get("call_id") or [""])[0], self._viewer()["agents"]):
                self._send_json({"status": "error", "error": "Call not found"}, 404)
                return
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
            if not call_history.can_view((q.get("call_id") or [""])[0], self._viewer()["agents"]):
                self._send_json({"status": "error", "error": "Call not found"}, 404)
                return
            self._send_audio((q.get("call_id") or [""])[0])
            return
        elif parsed.path == "/api/calls/stats":
            self._send_json({"status": "ok", "stats": call_history.stats(visible_agents=self._viewer()["agents"])})
            return
        elif parsed.path == "/api/calls/export":
            q = parse_qs(parsed.query)
            csv_body = call_history.export_csv(
                visible_agents=self._viewer()["agents"],
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

        # --- Task 4.3: REST API & Multi-Tenant v1 GET Endpoints ---
        elif parsed.path == "/api/v1/health":
            self._send_json({"status": "ok", "service": "voice-agent-service", "version": "1.0.0", "timestamp": time.time()})
            return
        elif parsed.path == "/api/v1/auth/me":
            ok, ctx, err = auth_manager.authenticate_request(self._extract_headers())
            if not ok:
                self._send_json({"status": "error", "error": err}, 401)
                return
            self._send_json({"status": "ok", "auth": ctx})
            return
        elif parsed.path == "/api/v1/workspaces":
            ok, ctx, err = auth_manager.authenticate_request(self._extract_headers(), required_scope=ApiScope.WORKSPACES_ADMIN.value)
            if not ok:
                self._send_json({"status": "error", "error": err}, self._auth_err_code(err))
                return
            self._send_json({"status": "ok", "workspaces": auth_manager.list_workspaces()})
            return
        elif parsed.path == "/api/v1/api-keys":
            ok, ctx, err = auth_manager.authenticate_request(self._extract_headers(), required_scope=ApiScope.WORKSPACES_ADMIN.value)
            if not ok:
                self._send_json({"status": "error", "error": err}, self._auth_err_code(err))
                return
            q = parse_qs(parsed.query)
            ws_id = (q.get("workspace_id") or [""])[0] or ctx["workspace_id"]
            self._send_json({"status": "ok", "api_keys": auth_manager.list_api_keys(workspace_id=ws_id)})
            return
        elif parsed.path == "/api/v1/users":
            ok, ctx, err = auth_manager.authenticate_request(self._extract_headers(), required_scope=ApiScope.WORKSPACES_ADMIN.value)
            if not ok:
                self._send_json({"status": "error", "error": err}, self._auth_err_code(err))
                return
            q = parse_qs(parsed.query)
            ws_id = (q.get("workspace_id") or [""])[0] or ctx["workspace_id"]
            self._send_json({"status": "ok", "users": auth_manager.list_users(workspace_id=ws_id)})
            return
        elif parsed.path == "/api/v1/calls":
            ok, ctx, err = auth_manager.authenticate_request(self._extract_headers(), required_scope=ApiScope.CALLS_READ.value)
            if not ok:
                self._send_json({"status": "error", "error": err}, self._auth_err_code(err))
                return
            q = parse_qs(parsed.query)
            self._send_json({"status": "ok", "workspace_id": ctx["workspace_id"], **call_history.list_calls(page=int((q.get("page") or ["1"])[0] or 1), page_size=int((q.get("page_size") or ["25"])[0] or 25))})
            return
        page = self._page_file(parsed.path)
        if page:
            self.path = "/" + os.path.basename(page) + (("?" + parsed.query) if parsed.query else "")
        return super().do_GET()


    def do_POST(self):
        parsed = urlparse(self.path)
        # Twilio's voice webhook (public, form-encoded body read inside the handler) must be caught
        # before we read the body as JSON below.
        if self._maybe_twiml_webhook(parsed):
            return
        content_len = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            payload = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if not self._require_session(parsed) or not self._enforce_role(parsed, "POST"):
            return
        if parsed.path == "/api/v1/auth/login":
            # Step 1: password. If the account has a phone and SMS is configured, a code is sent and
            # the session is only created after /api/v1/auth/otp/verify. Otherwise sign in directly.
            from agent import sms
            remember = bool(payload.get("remember"))
            ua = self.headers.get("User-Agent", "")
            try:
                user = auth_manager.check_password(str(payload.get("email") or ""), str(payload.get("password") or ""))
            except ValueError as e:
                time.sleep(0.4)  # slow down guessing
                self._send_json({"status": "error", "error": str(e)}, 401)
                return
            phone = auth_manager.normalize_phone(user.phone)
            if phone and sms.configured().get("ready"):
                try:
                    step = auth_manager.begin_two_step(user, remember, ua)
                except ValueError as e:
                    self._send_json({"status": "error", "error": str(e)}, 400)
                    return
                result = sms.send_sms(step["phone"], f"Your Call Desk sign-in code is {step['code']}. It expires in 5 minutes.")
                if not result.get("ok"):
                    self._send_json({"status": "error", "error": "Password accepted but the SMS code could not be sent: " + str(result.get("error", ""))}, 502)
                    return
                body = {"status": "ok", "step": "otp", "ticket": step["ticket"], "phone_masked": step["phone_masked"],
                        "expires_in": auth_manager.OTP_TTL, "resend_after": auth_manager.OTP_RESEND_AFTER}
                if result.get("dry_run"):
                    body["dry_run_code"] = step["code"]   # SMS_DRY_RUN=1 only (local testing)
                self._send_json(body)
                return
            token, doc = auth_manager.create_session(user, remember=remember, user_agent=ua, method="password")
            info = auth_manager.session_info(token)
            info["hint"] = "Add a phone number on your profile to turn on the SMS code step." if not phone else ""
            self._send_json({"status": "ok", "step": "done", **info}, 200, self._set_session_cookie(token, doc["expires_at"] - time.time()))
            return
        if parsed.path == "/api/v1/auth/dev-session":
            # Automated browser tests on this machine need a session cookie without a phone in the loop.
            # Same trust boundary as the loopback API bypass; refused from any other address.
            if not (self._is_loopback() and os.getenv("AUTH_TRUST_LOOPBACK", "1") != "0"):
                self._send_json({"status": "error", "error": "Only available from localhost"}, 403)
                return
            user = auth_manager._current_user()
            if not user:
                self._send_json({"status": "error", "error": "No admin user"}, 400)
                return
            token, doc = auth_manager.create_session(user, remember=False, user_agent="dev-session", method="dev")
            self._send_json({"status": "ok", "token": token, "cookie": self.SESSION_COOKIE, **auth_manager.session_info(token)},
                            200, self._set_session_cookie(token, doc["expires_at"] - time.time()))
            return
        if parsed.path == "/api/v1/auth/otp/resend":
            from agent import sms
            try:
                step = auth_manager.resend_code(str(payload.get("ticket") or ""))
            except ValueError as e:
                self._send_json({"status": "error", "error": str(e)}, 429 if "Wait" in str(e) else 400)
                return
            result = sms.send_sms(step["phone"], f"Your Call Desk sign-in code is {step['code']}. It expires in 5 minutes.")
            if not result.get("ok"):
                self._send_json({"status": "error", "error": str(result.get("error", "SMS could not be sent"))}, 502)
                return
            body = {"status": "ok", "phone_masked": step["phone_masked"], "expires_in": auth_manager.OTP_TTL}
            if result.get("dry_run"):
                body["dry_run_code"] = step["code"]
            self._send_json(body)
            return
        if parsed.path == "/api/v1/auth/otp/verify":
            try:
                token, doc = auth_manager.complete_two_step(str(payload.get("ticket") or ""), str(payload.get("code") or ""))
            except ValueError as e:
                time.sleep(0.4)
                self._send_json({"status": "error", "error": str(e)}, 401)
                return
            self._send_json({"status": "ok", "step": "done", **auth_manager.session_info(token)}, 200, self._set_session_cookie(token, doc["expires_at"] - time.time()))
            return
        if parsed.path == "/api/v1/auth/setup":
            try:
                user = auth_manager.setup_admin(str(payload.get("email") or ""), str(payload.get("password") or ""), str(payload.get("name") or ""))
                token, doc = auth_manager.create_session(user, remember=True, user_agent=self.headers.get("User-Agent", ""), method="setup")
            except (ValueError, KeyError, PermissionError) as e:
                self._send_json({"status": "error", "error": str(e)}, 400)
                return
            self._send_json({"status": "ok", "step": "done", **auth_manager.session_info(token)}, 200, self._set_session_cookie(token, doc["expires_at"] - time.time()))
            return
        if parsed.path == "/api/v1/auth/users":
            v = self._viewer()
            action = str(payload.get("action") or "create")
            try:
                if action == "remove":
                    ok = auth_manager.deactivate_user(str(payload.get("user_id") or ""), v["user"].user_id if v["user"] else None)
                    self._send_json({"status": "ok" if ok else "error", "removed": ok}, 200 if ok else 404)
                    return
                ws_id = v["user"].workspace_id if v["user"] else (auth_manager.get_profile().get("workspace_id") or "ws-default")
                user = auth_manager.add_member(ws_id, str(payload.get("email") or ""), str(payload.get("password") or ""),
                                               role=str(payload.get("role") or "user"), name=str(payload.get("name") or ""),
                                               phone=str(payload.get("phone") or ""))
            except (ValueError, KeyError) as e:
                self._send_json({"status": "error", "error": str(e)}, 400)
                return
            self._send_json({"status": "ok", "user": {"user_id": user.user_id, "email": user.email, "name": user.name, "role": user.role, "phone": user.phone}})
            return
        if parsed.path == "/api/v1/auth/logout":
            auth_manager.logout(self._session_token())
            self._send_json({"status": "ok", "authenticated": False}, 200, self._set_session_cookie("", 0))
            return

        if parsed.path == "/api/telephony/dial":
            destination = payload.get("destination", "").strip()
            caller_id = payload.get("caller_id", "").strip() or None
            room_name = payload.get("room", "").strip() or None
            agent_choice = (
                payload.get("agent", "") or
                payload.get("agent_id", "") or
                payload.get("agent_name", "") or
                payload.get("outbound_agent", "")
            ).strip() or None
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
                        agent=agent_choice,
                    )
                )
                loop.close()
                self._send_json({"status": "ok", "call": asdict(record)})
            except Exception as err:
                self._send_json({"status": "error", "error": str(err)}, 500)
            return

        elif parsed.path in ("/api/telephony/inbound/simulate", "/api/telephony/simulate-inbound"):
            from_num = payload.get("from") or payload.get("caller_number") or "+15551234567"
            to_num = payload.get("to") or payload.get("dialed_number") or "+18005550199"
            routed = telephony_manager.route_inbound_call(dialed_number=to_num, caller_number=from_num)
            if not routed:
                self._send_json({"status": "error", "error": "No matching route found"}, 404)
            else:
                self._send_json({"status": "ok", "routed": routed})
            return

        elif parsed.path == "/api/telephony/numbers/purchase":
            number = payload.get("phone_number", "").strip()
            friendly = payload.get("friendly_name", "").strip()
            trunk_id = payload.get("trunk_id", "trunk-inbound-primary").strip()
            agent_name = payload.get("agent_name", "Intake Agent").strip()
            carrier = (payload.get("carrier") or "").strip() or None
            if not number:
                self._send_json({"error": "Missing 'phone_number' parameter"}, 400)
                return
            try:
                rec = telephony_manager.purchase_number(
                    phone_number=number,
                    friendly_name=friendly,
                    assigned_trunk_id=trunk_id or "trunk-inbound-primary",
                    assigned_agent=agent_name or "Intake Agent",
                    carrier=carrier,
                )
                self._send_json({"status": "ok", "number": rec})
            except ValueError as e:
                code = 409 if "already provisioned" in str(e) else 400
                self._send_json({"status": "error", "error": str(e)}, code)
            except Exception as e:
                self._send_json({"status": "error", "error": str(e)}, 500)
            return

        elif parsed.path == "/api/telephony/numbers/release":
            number = payload.get("phone_number", "").strip()
            if not number:
                self._send_json({"error": "Missing 'phone_number' parameter"}, 400)
                return
            ok = telephony_manager.release_number(number)
            if not ok:
                self._send_json({"status": "error", "released": False, "error": f"{number} is not an active owned number"}, 404)
                return
            self._send_json({"status": "ok", "released": True})
            return

        elif parsed.path == "/api/telephony/numbers/route":
            number = payload.get("phone_number", "").strip()
            agent_name = payload.get("agent_name", "Intake Agent").strip()
            trunk_id = payload.get("trunk_id", "").strip() or None
            if not number:
                self._send_json({"error": "Missing 'phone_number' parameter"}, 400)
                return
            try:
                res = telephony_manager.update_number_routing(number, agent_name, trunk_id)
            except ValueError as e:
                self._send_json({"status": "error", "error": str(e)}, 400)
                return
            if not res:
                self._send_json({"status": "error", "error": "Number not found or not active"}, 404)
                return
            self._send_json({"status": "ok", "number": res})
            return

        elif parsed.path == "/api/telephony/numbers/sync":
            try:
                numbers = telephony_manager.sync_carrier_numbers()
                self._send_json({"status": "ok", "numbers": numbers, "total": len(numbers)})
            except Exception as e:
                self._send_json({"status": "error", "error": str(e)}, 500)
            return

        elif parsed.path == "/api/telephony/trunks":
            from agent.telephony_manager import SIPTrunk, validate_unified_trunk_payload
            t_id = str(payload.get("trunk_id", "")).strip() or f"trunk-{int(time.time())}"
            name = str(payload.get("name") or "Custom SIP Trunk").strip()
            address = str(payload.get("address") or "").strip()
            numbers = payload.get("numbers", [])
            allowed = payload.get("allowed_addresses") or ["0.0.0.0/0"]
            transport = str(payload.get("transport") or "udp").lower()
            direction = str(payload.get("direction") or "both").lower()
            metadata = payload.get("metadata") or {}
            errors = validate_unified_trunk_payload(
                trunk_id=t_id,
                name=name,
                address=address,
                numbers=numbers,
                allowed_addresses=allowed,
                transport=transport,
                direction=direction,
            )
            if errors:
                self._send_json({"status": "error", "error": "; ".join(errors), "errors": errors}, 400)
                return
            if telephony_manager.has_trunk(t_id) and not payload.get("replace"):
                self._send_json({"status": "error", "error": f"Trunk id '{t_id}' already exists. Choose another id or pass replace=true."}, 409)
                return
            trunk = SIPTrunk(
                trunk_id=t_id,
                name=name,
                address=address,
                numbers=numbers,
                allowed_addresses=allowed,
                auth_username=payload.get("auth_username"),
                auth_password=payload.get("auth_password"),
                transport=transport,
                direction=direction,
                metadata=metadata,
            )
            telephony_manager.register_trunk(trunk)
            default_agent = None
            try:
                active = agent_builder.get_active_agent()
                default_agent = active.name if active else None
            except Exception:
                default_agent = None
            provisioned = []
            if trunk.is_inbound:
                for num in (trunk.numbers or []):
                    rec = telephony_manager.ensure_owned_number(num, t_id, agent_name=default_agent, carrier="twilio")
                    if rec:
                        provisioned.append(rec["phone_number"])
            self._send_json({"status": "ok", "trunk": telephony_manager._public_trunk(trunk),
                             "provisioned_numbers": provisioned})
            return

        elif parsed.path == "/api/telephony/trunks/inbound":
            from agent.telephony_manager import SIPInboundTrunk, validate_inbound_trunk_payload
            t_id = str(payload.get("trunk_id", "")).strip() or f"trunk-in-{int(time.time())}"
            name = str(payload.get("name") or "Custom Inbound Trunk").strip()
            numbers = payload.get("numbers", [])
            allowed = payload.get("allowed_addresses") or ["0.0.0.0/0"]
            errors = validate_inbound_trunk_payload(t_id, name, numbers, allowed)
            if errors:
                self._send_json({"status": "error", "error": "; ".join(errors), "errors": errors}, 400)
                return
            if telephony_manager.has_trunk(t_id) and not payload.get("replace"):
                self._send_json({"status": "error", "error": f"Trunk id '{t_id}' already exists. Choose another id or pass replace=true."}, 409)
                return
            trunk = SIPInboundTrunk(
                trunk_id=t_id,
                name=name,
                numbers=numbers,
                allowed_addresses=allowed,
                auth_username=payload.get("auth_username"),
                auth_password=payload.get("auth_password"),
            )
            telephony_manager.register_inbound_trunk(trunk)
            # Any DID typed into the trunk form is also provisioned as an owned number, so it shows in
            # Active Phone Numbers and can be assigned to an agent (the trunk alone doesn't do that).
            default_agent = None
            try:
                active = agent_builder.get_active_agent()
                default_agent = active.name if active else None
            except Exception:
                default_agent = None
            provisioned = []
            for num in (trunk.numbers or []):
                rec = telephony_manager.ensure_owned_number(num, t_id, agent_name=default_agent, carrier="twilio")
                if rec:
                    provisioned.append(rec["phone_number"])
            self._send_json({"status": "ok", "trunk": telephony_manager._public_trunk(trunk),
                             "provisioned_numbers": provisioned})
            return

        elif parsed.path == "/api/telephony/numbers/update":
            number = payload.get("phone_number", "").strip()
            friendly = payload.get("friendly_name")
            agent_name = payload.get("agent_name")
            trunk_id = payload.get("trunk_id")
            carrier = payload.get("carrier")
            if not number:
                self._send_json({"error": "Missing 'phone_number' parameter"}, 400)
                return
            res = telephony_manager.update_phone_number(
                phone_number=number,
                friendly_name=friendly,
                agent_name=agent_name,
                trunk_id=trunk_id,
                carrier=carrier,
            )
            if not res:
                self._send_json({"status": "error", "error": f"Phone number '{number}' not found"}, 404)
                return
            self._send_json({"status": "ok", "number": res})
            return

        elif parsed.path in ("/api/telephony/numbers/delete", "/api/telephony/numbers/remove"):
            number = payload.get("phone_number", "").strip()
            purge = bool(payload.get("purge", True))
            if not number:
                self._send_json({"error": "Missing 'phone_number' parameter"}, 400)
                return
            ok = telephony_manager.delete_phone_number(number, purge=purge)
            if not ok:
                self._send_json({"status": "error", "error": f"Phone number '{number}' not found"}, 404)
                return
            self._send_json({"status": "ok", "deleted": True})
            return

        elif parsed.path == "/api/telephony/trunks/inbound/delete":
            trunk_id = payload.get("trunk_id", "").strip()
            if not trunk_id:
                self._send_json({"error": "Missing 'trunk_id' parameter"}, 400)
                return
            ok = telephony_manager.delete_inbound_trunk(trunk_id)
            if not ok:
                self._send_json({"status": "error", "error": f"Inbound trunk '{trunk_id}' not found"}, 404)
                return
            self._send_json({"status": "ok", "deleted": True})
            return

        elif parsed.path == "/api/telephony/trunks/outbound/delete":
            trunk_id = payload.get("trunk_id", "").strip()
            if not trunk_id:
                self._send_json({"error": "Missing 'trunk_id' parameter"}, 400)
                return
            ok = telephony_manager.delete_outbound_trunk(trunk_id)
            if not ok:
                self._send_json({"status": "error", "error": f"Outbound trunk '{trunk_id}' not found"}, 404)
                return
            self._send_json({"status": "ok", "deleted": True})
            return

        elif parsed.path in ("/api/telephony/trunks/delete", "/api/telephony/trunks/remove"):
            trunk_id = payload.get("trunk_id", "").strip()
            if not trunk_id:
                self._send_json({"error": "Missing 'trunk_id' parameter"}, 400)
                return
            ok = telephony_manager.delete_trunk(trunk_id)
            if not ok:
                self._send_json({"status": "error", "error": f"Trunk '{trunk_id}' not found"}, 404)
                return
            self._send_json({"status": "ok", "deleted": True})
            return

        elif parsed.path == "/api/telephony/trunks/outbound":
            from agent.telephony_manager import SIPOutboundTrunk, validate_outbound_trunk_payload
            t_id = str(payload.get("trunk_id", "")).strip() or f"trunk-out-{int(time.time())}"
            name = str(payload.get("name") or "Custom Outbound Trunk").strip()
            address = str(payload.get("address") or "").strip()
            numbers = payload.get("numbers", [])
            transport = str(payload.get("transport") or "udp").lower()
            errors = validate_outbound_trunk_payload(t_id, name, address, numbers, transport)
            if errors:
                self._send_json({"status": "error", "error": "; ".join(errors), "errors": errors}, 400)
                return
            if telephony_manager.has_trunk(t_id) and not payload.get("replace"):
                self._send_json({"status": "error", "error": f"Trunk id '{t_id}' already exists. Choose another id or pass replace=true."}, 409)
                return
            trunk = SIPOutboundTrunk(
                trunk_id=t_id,
                name=name,
                address=address,
                numbers=numbers,
                auth_username=payload.get("auth_username"),
                auth_password=payload.get("auth_password"),
                transport=transport,
            )
            telephony_manager.register_outbound_trunk(trunk)
            self._send_json({"status": "ok", "trunk": telephony_manager._public_trunk(trunk)})
            return

        elif parsed.path in ("/api/telephony/calls/end", "/api/telephony/calls/hangup"):
            call_id = payload.get("call_id", "").strip()
            if not call_id:
                self._send_json({"error": "Missing 'call_id' parameter"}, 400)
                return
            rec = telephony_manager.end_call(call_id)
            self._send_json({"status": "ok", "call": asdict(rec) if rec else None})
            return

        elif parsed.path in ("/api/telephony/calls/clear", "/api/telephony/calls/end-all"):
            ended = telephony_manager.end_all_calls()
            self._send_json({"status": "ok", "ended_count": len(ended)})
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
                job = run_pipeline_job(job.job_id)
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
                job = run_pipeline_job(job.job_id)
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

        elif parsed.path == "/api/agents":
            active = agent_builder.get_active_agent()
            # "new": true forces a create. Without it, a missing agent_id used to fall back to the
            # live agent, so a second agent edited in the builder overwrote the first one.
            create_new = bool(payload.get("new"))
            viewer = self._viewer()
            agent_id = None if create_new else (payload.get("agent_id") or (active.agent_id if active else None))
            # A member with nothing loaded must not fall back to the workspace's live agent: create theirs.
            if agent_id and not payload.get("agent_id") and not agent_builder.can_access(agent_id, viewer["owner"]):
                agent_id = None
            if agent_id and not agent_builder.can_access(agent_id, viewer["owner"]):
                self._send_json({"status": "error", "error": "Agent not found"}, 404)
                return
            if agent_id:
                try:
                    changes = {k: v for k, v in payload.items() if k not in ("agent_id", "new", "activate", "note")}
                    cfg = agent_builder.update_agent(agent_id, changes, payload.get("note", "Updated via agent builder"))
                    # Saving edits no longer changes which agent takes live calls; use /api/agents/activate
                    # (or activate: true here) for that.
                    if payload.get("activate") or not agent_builder.get_active_agent():
                        agent_builder.set_active(agent_id)
                    cfg = agent_builder.get_agent(agent_id) or cfg
                    self._send_json({"status": "ok", "agent": cfg.to_dict(), "lint": agent_builder.lint_prompt(cfg.system_prompt)})
                    return
                except Exception as e:
                    self._send_json({"status": "error", "error": str(e)}, 400)
                    return
            else:
                try:
                    cfg = agent_builder.create_agent(
                        name=payload.get("name", "Voice Agent"),
                        first_message=payload.get("first_message", "Hello, how can I help?"),
                        system_prompt=payload.get("system_prompt", "You are a helpful assistant."),
                        owner_id=(viewer["user"].user_id if viewer["user"] else ""),
                        **{k: v for k, v in payload.items() if k not in ("name", "first_message", "system_prompt", "agent_id", "new", "activate", "note", "owner_id")},
                    )
                    if payload.get("activate") or not agent_builder.get_active_agent():
                        agent_builder.set_active(cfg.agent_id)
                    cfg = agent_builder.get_agent(cfg.agent_id) or cfg
                    self._send_json({"status": "ok", "agent": cfg.to_dict()})
                    return
                except Exception as e:
                    self._send_json({"status": "error", "error": str(e)}, 400)
                    return

        elif parsed.path == "/api/agents/tools/test":
            tool_name = payload.get("tool_name") or payload.get("name") or ""
            args = payload.get("arguments") or payload.get("args") or {}
            from agent.tool_manager import ToolRegistry, AsyncToolDispatcher
            reg = ToolRegistry()
            dispatcher = AsyncToolDispatcher(reg)
            if not reg.get_tool(tool_name):
                self._send_json({"status": "error", "error": f"Tool '{tool_name}' not found"}, 404)
                return
            try:
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(dispatcher.execute_tool(tool_name, args))
                loop.close()
                self._send_json({"status": "ok", "execution": result})
            except Exception as ex:
                self._send_json({"status": "error", "error": str(ex)}, 500)
            return

        elif parsed.path == "/api/agents/tools/register":
            name = payload.get("name", "").strip().lower().replace(" ", "_")
            desc = payload.get("description", "").strip()
            endpoint = payload.get("endpoint_url", "").strip()
            method = payload.get("method", "POST").upper()
            timeout = float(payload.get("timeout", 3.0))
            params = payload.get("parameters", {"type": "object", "properties": {}})
            fillers = payload.get("filler_phrases", [f"Contacting {name} service..."])

            if not name or not desc:
                self._send_json({"status": "error", "error": "Tool name and description are required"}, 400)
                return

            from agent.tool_manager import ToolRegistry, ToolDefinition
            reg = ToolRegistry()

            async def custom_handler(**kwargs):
                if endpoint:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        if method == "POST":
                            async with session.post(endpoint, json=kwargs, timeout=timeout) as resp:
                                return {"status_code": resp.status, "body": await resp.text()}
                        else:
                            async with session.get(endpoint, params=kwargs, timeout=timeout) as resp:
                                return {"status_code": resp.status, "body": await resp.text()}
                else:
                    return {"status": "executed", "tool": name, "echo": kwargs}

            tool_def = ToolDefinition(
                name=name,
                description=desc,
                parameters=params if isinstance(params, dict) else {"type": "object", "properties": {}},
                handler=custom_handler,
                filler_phrases=fillers if isinstance(fillers, list) else [str(fillers)],
                timeout=timeout,
            )
            reg.register(tool_def, persist=True)
            self._send_json({"status": "ok", "tool": {
                "name": tool_def.name,
                "description": tool_def.description,
                "parameters": tool_def.parameters,
                "timeout": tool_def.timeout,
                "filler_phrases": tool_def.filler_phrases,
            }})
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
            if not self._agent_allowed(str(payload.get("agent_id", ""))):
                return
            agent_id = payload.get("agent_id", "")
            changes = payload.get("changes") or payload.get("patch") or {k: v for k, v in payload.items() if k not in ("agent_id", "note")}
            try:
                cfg = agent_builder.update_agent(
                    agent_id, changes, payload.get("note", ""))
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
            if not self._agent_allowed(str(payload.get("agent_id", ""))):
                return
            try:
                cfg = agent_builder.clone_agent(payload.get("agent_id", ""), payload.get("name"))
            except KeyError:
                self._send_json({"status": "error", "error": "Agent not found"}, 404)
                return
            self._send_json({"status": "ok", "agent": cfg.to_dict()})
            return

        elif parsed.path == "/api/agents/delete":
            if not self._agent_allowed(str(payload.get("agent_id", ""))):
                return
            removed = agent_builder.delete_agent(payload.get("agent_id", ""))
            self._send_json({"status": "ok" if removed else "error", "removed": removed},
                            200 if removed else 404)
            return

        elif parsed.path == "/api/agents/activate":
            if not self._agent_allowed(str(payload.get("agent_id", ""))):
                return
            try:
                cfg = agent_builder.set_active(payload.get("agent_id", ""))
            except KeyError:
                self._send_json({"status": "error", "error": "Agent not found"}, 404)
                return
            self._send_json({"status": "ok", "agent": cfg.to_dict()})
            return

        elif parsed.path == "/api/agents/rollback":
            if not self._agent_allowed(str(payload.get("agent_id", ""))):
                return
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
            agent_id = payload.get("agent_id", "")
            if agent_id and agent_id in agent_builder._agents and not self._agent_allowed(agent_id):
                return
            if not agent_id or agent_id not in agent_builder._agents:
                active = agent_builder.get_active_agent()
                agent_id = active.agent_id if active else (list(agent_builder._agents.keys())[0] if agent_builder._agents else "test-agent")
            
            # Apply inline edits if sent from builder — but only when something actually changed.
            # Writing a revision per test message had grown config/agents.json to 1.4 MB.
            editable_keys = ("system_prompt", "first_message", "voice_id", "llm_model", "tools", "speak_first")
            current = agent_builder._agents.get(agent_id)
            changes = {k: v for k, v in payload.items()
                       if k in editable_keys and v is not None and current is not None and getattr(current, k, None) != v}
            if changes and current is not None:
                try:
                    agent_builder.update_agent(agent_id, changes, note="Test run inline update")
                except Exception:
                    pass

            try:
                if "utterance" in payload:
                    # Live sandbox: one new caller line against the transcript the page already shows.
                    turn = agent_builder.reply_once(agent_id, payload.get("history") or [], str(payload.get("utterance") or ""))
                    # Pre-warm the audio for each speech chunk right now, in the background, so the
                    # page's fetch a few ms later finds it (or waits only for the remainder).
                    voice = agent_builder.get_voice(str(payload.get("voice_id") or (current.voice_id if current else "")))
                    style = str(payload.get("style") or (voice.style if voice else "") or "")
                    if voice and turn.get("speech_chunks"):
                        import threading
                        from agent.voice_synthesizer import stream_voice_audio

                        def _prewarm(chunk_text):
                            for _ in stream_voice_audio(voice_id=voice.voice_id, name=voice.name, gender=voice.gender,
                                                        style=style or voice.style, provider=voice.provider, text=chunk_text):
                                pass
                        for chunk in turn["speech_chunks"]:
                            threading.Thread(target=_prewarm, args=(chunk,), daemon=True).start()
                    self._send_json({"status": "ok", "turn": turn, "backend": turn.get("backend")})
                    return
                result = agent_builder.test_run(
                    agent_id, payload.get("utterances", []))
            except KeyError:
                self._send_json({"status": "error", "error": "Agent not found"}, 404)
                return
            self._send_json({"status": "ok", "result": result})
            return

        elif parsed.path == "/api/agents/test-call/save":
            # The Agent Builder sandbox ends a test call: keep it like a real call so the dashboard
            # shows the transcript, sentiment/summary and a playable dual-channel recording.
            import base64
            agent_id = payload.get("agent_id", "")
            if agent_id and agent_builder.get_agent(agent_id) and not self._agent_allowed(agent_id):
                return
            cfg = agent_builder.get_agent(agent_id) or agent_builder.get_active_agent()
            turns_in = payload.get("turns") or []
            if not turns_in:
                self._send_json({"status": "error", "error": "No transcript turns"}, 400)
                return
            started = float(payload.get("started_at") or time.time())
            ended = float(payload.get("ended_at") or time.time())
            call_id = f"sandbox-{int(started * 1000)}"
            transcript_turns = []
            for i, t in enumerate(turns_in, start=1):
                is_caller = str(t.get("speaker", "")).lower() == "caller"
                text = str(t.get("text", "")).strip()
                transcript_turns.append({
                    "turn_index": i,
                    "role": "user" if is_caller else "assistant",
                    "speaker": "Customer" if is_caller else (cfg.name if cfg else "AI Agent"),
                    "text": text,
                    "timestamp": float(t.get("at") or 0) / 1000.0 if t.get("at") else started,
                    "end_timestamp": (float(t.get("end_at")) / 1000.0) if t.get("end_at") else None,
                    "word_count": len(text.split()),
                    "tool": t.get("tool"),
                    "backend": t.get("backend"),
                })
            audio_path = None
            wav_b64 = payload.get("wav_base64") or ""
            if wav_b64:
                try:
                    raw = base64.b64decode(wav_b64)
                    if raw[:4] == b"RIFF":
                        os.makedirs(call_history.recordings_dir, exist_ok=True)
                        audio_path = os.path.join(call_history.recordings_dir, f"rec-{call_id}-{int(ended)}.wav")
                        with open(audio_path, "wb") as f:
                            f.write(raw)
                        from agent import storage
                        storage.save_recording(audio_path, call_id)
                except Exception as e:
                    log.warning("Sandbox recording not saved: %s", e)
            job = pipeline_worker.enqueue_call(
                call_id=call_id,
                room_name=f"sandbox-{agent_id or 'agent'}",
                transcript_turns=transcript_turns,
                audio_path=audio_path,
                metadata={
                    "source": "agent-builder-sandbox",
                    "agent_name": cfg.name if cfg else "AI Agent",
                    "agent_id": cfg.agent_id if cfg else agent_id,
                    "direction": "sandbox",
                    "from_number": "Agent Builder test call",
                    "to_number": cfg.name if cfg else "",
                    "voice_id": payload.get("voice_id", ""),
                    "llm_backend": payload.get("backend", ""),
                    "mic_log": (payload.get("mic_log") or [])[:600],   # browser recogniser events, for diagnosing "it did not hear me"
                    "started_at": started,
                    "ended_at": ended,
                    "duration_seconds": max(0.0, ended - started),
                },
                priority=3,
            )
            try:
                job = run_pipeline_job(job.job_id)
            except Exception as e:
                log.warning("Sandbox post-call pipeline failed: %s", e)
            self._send_json({"status": "ok", "call_id": call_id, "job_id": job.job_id, "has_audio": bool(audio_path)})
            return

        elif parsed.path == "/api/v1/auth/profile":
            try:
                su = self._session_user()
                self._send_json({"status": "ok", "profile": auth_manager.update_profile(payload, su.user_id if su else None)})
            except (KeyError, ValueError) as e:
                self._send_json({"status": "error", "error": str(e)}, 400)
            return

        elif parsed.path == "/api/agents/suggest-fields":
            # AI reads the agent's prompt and proposes the data points to extract from every call.
            from agent.schema_extractor import suggest_fields_from_prompt
            agent_id = str(payload.get("agent_id") or "")
            if agent_id and not self._agent_allowed(agent_id):
                return
            cfg = agent_builder.get_agent(agent_id) if agent_id else None
            prompt_text = str(payload.get("system_prompt") or (cfg.system_prompt if cfg else ""))
            first = str(payload.get("first_message") or (cfg.first_message if cfg else ""))
            if not prompt_text.strip():
                self._send_json({"status": "error", "error": "No prompt to analyse"}, 400)
                return
            try:
                fields = suggest_fields_from_prompt(prompt_text, first)
            except Exception as e:
                self._send_json({"status": "error", "error": str(e)}, 502)
                return
            self._send_json({"status": "ok", "fields": fields, "count": len(fields)})
            return

        elif parsed.path == "/api/agents/extract-preview":
            # Run the agent's field extraction on a past call (latest by default) to see what a webhook would carry.
            from agent.schema_extractor import schema_extractor, fields_to_schema
            agent_id = str(payload.get("agent_id") or "")
            if not agent_id or not self._agent_allowed(agent_id):
                if not agent_id:
                    self._send_json({"status": "error", "error": "agent_id required"}, 400)
                return
            cfg = agent_builder.get_agent(agent_id)
            fields = payload.get("fields") or (cfg.extraction_fields if cfg else [])
            if not fields:
                self._send_json({"status": "error", "error": "This agent has no data fields yet. Use Suggest from prompt first."}, 400)
                return
            turns = payload.get("transcript_turns")
            call_id = str(payload.get("call_id") or "")
            if not turns:
                visible = self._viewer()["agents"]
                if call_id:
                    if not call_history.can_view(call_id, visible):
                        self._send_json({"status": "error", "error": "Call not found"}, 404)
                        return
                else:
                    listing = call_history.list_calls(page_size=1, agent=cfg.name if cfg else None, visible_agents=visible)
                    call_id = listing["items"][0]["call_id"] if listing["items"] else ""
                job = pipeline_worker.get_job(call_id=call_id) if call_id else None
                turns = getattr(job, "transcript_turns", None) if job else None
                if not turns:
                    self._send_json({"status": "error", "error": "No transcript found for this agent yet. Run a test call first."}, 404)
                    return
            res = schema_extractor.extract_with_ai(f"agent:{agent_id}", call_id or "preview", turns, {}, schema=fields_to_schema(fields))
            self._send_json({"status": "ok", "call_id": call_id, "engine": "gemini" if any(f.source == "ai" for f in res.fields) else "regex",
                             "values": res.to_crm_payload(), "fields": [f.to_dict() for f in res.fields if not f.field_name.startswith("_")],
                             "coverage": res.extraction_coverage, "confidence": res.overall_confidence, "missing_required": res.missing_required,
                             "note": next((f.raw_match for f in res.fields if f.field_name == "_ai_note"), "")})
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

        elif parsed.path == "/api/providers/keys":
            from agent.provider_manager import provider_manager
            keys_to_update = payload.get("keys") or {k: v for k, v in payload.items() if k not in ("action", "note")}
            try:
                res = provider_manager.save_keys(keys_to_update)
                self._send_json({
                    "status": "ok",
                    "updated": res.get("updated", []),
                    "providers": provider_manager.list_providers_status(),
                })
            except Exception as ex:
                self._send_json({"status": "error", "error": str(ex)}, 500)
            return

        elif parsed.path == "/api/providers/test":
            from agent.provider_manager import provider_manager
            provider_id = payload.get("provider_id") or payload.get("provider") or ""
            api_key = payload.get("api_key") or payload.get("key") or None
            res = provider_manager.test_provider_connection(provider_id, api_key)
            self._send_json(res, 200 if res.get("status") == "ok" else 400)
            return

        # --- Task 4.3: REST API & Multi-Tenant v1 POST Endpoints ---
        elif parsed.path == "/api/v1/auth/token":
            # Issues signed JWT Bearer token
            subject = payload.get("username", payload.get("email", payload.get("subject", "user")))
            ws_id = payload.get("workspace_id", "ws-default")
            role = payload.get("role", "user")
            ttl = int(payload.get("ttl_seconds", 3600))
            token = auth_manager.issue_token(workspace_id=ws_id, subject=subject, role=role, ttl_seconds=ttl)
            self._send_json({"status": "ok", "token": token, "token_type": "Bearer", "expires_in": ttl, "workspace_id": ws_id})
            return

        elif parsed.path == "/api/v1/workspaces":
            ok, ctx, err = auth_manager.authenticate_request(self._extract_headers(), required_scope=ApiScope.WORKSPACES_ADMIN.value)
            if not ok:
                self._send_json({"status": "error", "error": err}, self._auth_err_code(err))
                return
            name = payload.get("name", "").strip()
            if not name:
                self._send_json({"status": "error", "error": "Workspace 'name' is required"}, 400)
                return
            ws = auth_manager.create_workspace(name=name, slug=payload.get("slug"), rate_limit_rpm=int(payload.get("rate_limit_rpm", 120)))
            self._send_json({"status": "ok", "workspace": ws.to_dict()}, 201)
            return

        elif parsed.path == "/api/v1/api-keys":
            ok, ctx, err = auth_manager.authenticate_request(self._extract_headers(), required_scope=ApiScope.WORKSPACES_ADMIN.value)
            if not ok:
                self._send_json({"status": "error", "error": err}, self._auth_err_code(err))
                return
            name = payload.get("name", "Default Key").strip()
            ws_id = payload.get("workspace_id", ctx["workspace_id"])
            scopes = payload.get("scopes")
            is_test = bool(payload.get("is_test", False))
            try:
                key_obj, raw_secret = auth_manager.generate_api_key(workspace_id=ws_id, name=name, scopes=scopes, is_test=is_test)
                self._send_json({"status": "ok", "api_key": key_obj.to_dict(include_hash=False), "secret_token": raw_secret}, 201)
            except KeyError as e:
                self._send_json({"status": "error", "error": str(e)}, 404)
            return

        elif parsed.path == "/api/v1/api-keys/revoke":
            ok, ctx, err = auth_manager.authenticate_request(self._extract_headers(), required_scope=ApiScope.WORKSPACES_ADMIN.value)
            if not ok:
                self._send_json({"status": "error", "error": err}, self._auth_err_code(err))
                return
            key_id = payload.get("key_id", "").strip()
            if not auth_manager.revoke_api_key(key_id):
                self._send_json({"status": "error", "error": f"API Key '{key_id}' not found"}, 404)
                return
            self._send_json({"status": "ok", "revoked_key_id": key_id})
            return

        elif parsed.path == "/api/v1/users":
            ok, ctx, err = auth_manager.authenticate_request(self._extract_headers(), required_scope=ApiScope.WORKSPACES_ADMIN.value)
            if not ok:
                self._send_json({"status": "error", "error": err}, self._auth_err_code(err))
                return
            email = payload.get("email", "").strip()
            role = payload.get("role", "user").strip()
            ws_id = payload.get("workspace_id", ctx["workspace_id"])
            try:
                user = auth_manager.create_user(workspace_id=ws_id, email=email, role=role)
                self._send_json({"status": "ok", "user": user.to_dict()}, 201)
            except (KeyError, ValueError) as e:
                self._send_json({"status": "error", "error": str(e)}, 400)
            return

        elif parsed.path == "/api/v1/calls/dispatch":
            # Programmatic Call Dispatching with RBAC & Rate Limiting
            ok, ctx, err = auth_manager.authenticate_request(self._extract_headers(), required_scope=ApiScope.CALLS_DISPATCH.value)
            if not ok:
                self._send_json({"status": "error", "error": err}, self._auth_err_code(err))
                return

            destination = payload.get("to", payload.get("destination", "")).strip()
            if not destination:
                self._send_json({"status": "error", "error": "Destination number 'to' is required"}, 400)
                return
            caller_id = payload.get("from", payload.get("caller_id", None))
            agent_id = payload.get("agent_id", "intake-agent")
            custom_metadata = payload.get("metadata", {})
            room_name = f"dispatch-{ctx['workspace_id']}-{int(time.time()*1000)}"

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
                dispatch_res = {
                    "status": "dispatched",
                    "call_id": record.call_id,
                    "workspace_id": ctx["workspace_id"],
                    "agent_id": agent_id,
                    "destination": destination,
                    "room": room_name,
                    "dispatched_by": ctx["identity"],
                    "timestamp": time.time(),
                }
                self._send_json({"status": "ok", "dispatch": dispatch_res}, 200)
            except Exception as err:
                self._send_json({"status": "error", "error": str(err)}, 500)
            return

        self.send_error(404, "Endpoint not found")




    def log_message(self, fmt, *args):
        first_arg = str(args[0]) if args else ""
        if "/token" in first_arg:
            sys.stderr.write("  token issued\n")


print(f"Test page:  http://localhost:{PORT}")
print(f"Signalling: {WS_URL}")
print("Ctrl+C to stop\n")

import signal
try:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
except Exception:
    pass

try:
    webhook_dispatcher.attach_to_pipeline(pipeline_worker)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    while True:
        try:
            server.serve_forever()
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as err:
            time.sleep(0.2)
except OSError as e:
    raise SystemExit(f"Port {PORT} is in use ({e}). Pass another: "
                     f"python3 scripts/serve.py 9090")
except KeyboardInterrupt:
    print("\nstopped")

