"""Task 3.4 — Webhook Delivery System (HMAC-SHA256).

Delivers post-call events to customer HTTP endpoints with the guarantees an
external integrator needs to trust them:
1. Endpoint registry with per-endpoint secrets and event subscriptions.
2. HMAC-SHA256 request signing over "<timestamp>.<body>" (X-Signature-256).
3. Replay prevention: signed timestamp window + delivery-id de-duplication.
4. Exponential backoff retries with jitter, then dead-letter parking.
5. Delivery audit log persisted to disk (every attempt, code, latency, error).
6. Pipeline integration emitting call.completed / call.transcribed / call.analyzed.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

log = logging.getLogger("voice-agent.webhooks")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT_DIR, "recordings", "webhook_state.json")

# Signed timestamps older than this are refused as replays.
REPLAY_WINDOW_SECONDS = 300
# Retry pacing: 1s, 2s, 4s, 8s ... capped, each with up to 25% jitter.
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 30.0
RETRY_JITTER = 0.25
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_ATTEMPTS = 4
AUDIT_LOG_LIMIT = 500
SEEN_DELIVERY_LIMIT = 2000

SIGNATURE_HEADER = "X-Signature-256"
TIMESTAMP_HEADER = "X-Webhook-Timestamp"
DELIVERY_HEADER = "X-Webhook-Delivery"
EVENT_HEADER = "X-Webhook-Event"
ATTEMPT_HEADER = "X-Webhook-Attempt"


class WebhookEvent(str, Enum):
    # Retell AI Event Standards
    CALL_STARTED     = "call_started"
    CALL_ENDED       = "call_ended"
    CALL_ANALYZED_RETELL = "call_analyzed"
    # Platform Events
    CALL_COMPLETED   = "call.completed"
    CALL_TRANSCRIBED = "call.transcribed"
    CALL_ANALYZED    = "call.analyzed"
    CALL_EXTRACTED   = "call.extracted"
    CALL_FAILED      = "call.failed"


class DeliveryStatus(str, Enum):
    PENDING       = "pending"
    DELIVERED     = "delivered"
    RETRYING      = "retrying"
    FAILED        = "failed"
    DEAD_LETTERED = "dead_lettered"


@dataclass
class WebhookEndpoint:
    endpoint_id: str
    url: str
    secret: str
    events: List[str] = field(default_factory=lambda: [e.value for e in WebhookEvent])
    description: str = ""
    active: bool = True
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    timeout_s: float = DEFAULT_TIMEOUT
    headers: Dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    schema_format: str = "standard"  # "standard" or "retell"

    def subscribes_to(self, event: str) -> bool:
        if not self.active:
            return False
        if "*" in self.events:
            return True
        if event in self.events:
            return True
        # Bidirectional aliases between Retell AI and internal events:
        aliases = {
            "call_started": ["call.started"],
            "call.started": ["call_started"],
            "call_ended": ["call.completed"],
            "call.completed": ["call_ended"],
            "call_analyzed": ["call.analyzed"],
            "call.analyzed": ["call_analyzed"],
        }
        for alias in aliases.get(event, []):
            if alias in self.events:
                return True
        return False

    def to_dict(self, redact_secret: bool = True) -> Dict[str, Any]:
        data = asdict(self)
        if redact_secret:
            prefix = "whsec_" if self.secret.startswith("whsec_") else ""
            data["secret"] = f"{prefix}***" + self.secret[-4:] if len(self.secret) > 8 else "***"
        return data


@dataclass
class DeliveryAttempt:
    attempt: int
    started_at: float
    duration_ms: float
    status_code: Optional[int] = None
    error: Optional[str] = None
    next_retry_in_s: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WebhookDelivery:
    delivery_id: str
    endpoint_id: str
    url: str
    event: str
    call_id: Optional[str] = None
    payload_digest: str = ""
    status: str = DeliveryStatus.PENDING.value
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    signature: str = ""
    timestamp: int = 0
    last_error: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["attempt_count"] = self.attempt_count
        return data


def format_retell_payload(
    event: str,
    data: Dict[str, Any],
    delivery_id: Optional[str] = None,
    call_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Formats event data into the exact Retell AI webhook schema.
    
    Produces { "event": "call_started"|"call_ended"|"call_analyzed", "call": { ... } }
    with full transcript, recording URL, call_analysis, and custom_analysis_data.
    """
    retell_event = event
    if event in ("call.completed", "call_ended"):
        retell_event = "call_ended"
    elif event in ("call.analyzed", "call_analyzed"):
        retell_event = "call_analyzed"
    elif event in ("call.started", "call_started"):
        retell_event = "call_started"
    elif event == "call.failed":
        retell_event = "call_ended"

    call_data = data.get("call") if isinstance(data.get("call"), dict) else {}
    cid = call_id or call_data.get("call_id") or data.get("call_id") or f"call_{secrets.token_hex(8)}"

    started_at_raw = call_data.get("started_at") or data.get("started_at")
    try:
        if started_at_raw is None:
            started_at = time.time()
        elif isinstance(started_at_raw, (int, float)):
            started_at = float(started_at_raw)
        else:
            started_at = float(str(started_at_raw).strip())
    except (ValueError, TypeError):
        started_at = time.time()

    start_ts_ms = int(started_at * 1000) if started_at < 1e11 else int(started_at)
    dur_raw = call_data.get("duration_seconds") or data.get("duration_seconds") or 0
    try:
        dur_s = float(dur_raw)
    except (ValueError, TypeError):
        dur_s = 0.0
    duration_ms = int(dur_s * 1000)
    end_ts_ms = start_ts_ms + duration_ms if retell_event != "call_started" else None

    raw_turns = data.get("transcript") or []
    transcript_lines = []
    transcript_objects = []
    if isinstance(raw_turns, list):
        for t in raw_turns:
            if isinstance(t, dict):
                speaker = (t.get("speaker") or t.get("role") or "user").lower()
                role = "agent" if speaker in ("agent", "bot", "assistant") else "user"
                content = t.get("text") or t.get("content") or ""
                transcript_lines.append(f"{role.capitalize()}: {content}")
                transcript_objects.append({"role": role, "content": content, "words": []})
        formatted_transcript = "\n".join(transcript_lines)
    elif isinstance(raw_turns, str):
        formatted_transcript = raw_turns
    else:
        formatted_transcript = ""

    if retell_event == "call_started":
        call_status = "ongoing"
        disconnection_reason = None
    elif event == "call.failed":
        call_status = "error"
        disconnection_reason = data.get("disconnection_reason") or "error"
    else:
        call_status = "ended"
        disconnection_reason = data.get("disconnection_reason") or "user_hangup"

    call_obj: Dict[str, Any] = {
        "call_id": cid,
        "call_type": "phone_call" if (call_data.get("from_number") or call_data.get("to_number")) else "web_call",
        "agent_id": call_data.get("agent_id") or "agent_default",
        "call_status": call_status,
        "start_timestamp": start_ts_ms,
        "end_timestamp": end_ts_ms,
        "duration_ms": duration_ms,
        "transcript": formatted_transcript,
        "transcript_object": transcript_objects,
        "recording_url": data.get("archive_url") or call_data.get("archive_url") or "",
        "disconnection_reason": disconnection_reason,
        "from_number": call_data.get("from_number") or "",
        "to_number": call_data.get("to_number") or "",
        "direction": call_data.get("direction") or "inbound",
        "metadata": data.get("metadata") or {},
    }

    if retell_event in ("call_analyzed", "call.completed", "call_ended"):
        summary = data.get("summary")
        summary_text = summary.get("executive_summary", "") if isinstance(summary, dict) else str(summary or "")

        sentiment = data.get("sentiment")
        if isinstance(sentiment, dict):
            polarity = str(sentiment.get("overall_polarity", "Neutral")).capitalize()
        else:
            polarity = str(sentiment or "Neutral").capitalize()

        extracted = data.get("extracted") or {}
        call_obj["call_analysis"] = {
            "call_summary": summary_text,
            "user_sentiment": polarity,
            "call_successful": data.get("call_successful", True),
            "in_voicemail": data.get("in_voicemail", False),
            "custom_analysis_data": extracted,
        }

    envelope: Dict[str, Any] = {
        "event": retell_event,
        "call": call_obj,
    }
    if delivery_id:
        envelope["id"] = delivery_id
    envelope["data"] = data
    return envelope


def generate_secret() -> str:
    """Mints an endpoint signing secret (whsec_ prefix mirrors common conventions)."""
    return "whsec_" + secrets.token_hex(24)


def canonical_body(payload: Dict[str, Any]) -> str:
    """Serializes a payload deterministically so both sides sign identical bytes."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def sign_payload(secret: str, body: str, timestamp: int) -> str:
    """Computes the X-Signature-256 value over '<timestamp>.<body>'."""
    signed = f"{timestamp}.{body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    secret: str,
    body: str,
    timestamp: Any,
    signature: str,
    tolerance_s: int = REPLAY_WINDOW_SECONDS,
    now: Optional[float] = None,
) -> Tuple[bool, str]:
    """Verifies a received webhook. Returns (ok, reason). Supports Retell & standard HMAC."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False, "invalid_timestamp"

    current = time.time() if now is None else now
    age = current - ts
    if age > tolerance_s:
        return False, "timestamp_too_old"
    if age < -tolerance_s:
        return False, "timestamp_in_future"

    expected = sign_payload(secret, body, ts)
    expected_raw = expected.split("sha256=")[-1]

    sig_str = (signature or "").strip()
    if sig_str.startswith("v=1,"):
        sig_str = sig_str.split("v=1,", 1)[1].strip()

    if sig_str.startswith("sha256="):
        sig_raw = sig_str.split("sha256=", 1)[1].strip()
    else:
        sig_raw = sig_str

    if hmac.compare_digest(expected_raw, sig_raw):
        return True, "ok"
    if hmac.compare_digest(expected, sig_str):
        return True, "ok"

    return False, "signature_mismatch"


def backoff_delay(attempt: int, jitter: bool = True) -> float:
    """Exponential backoff for `attempt` (1-based), capped and optionally jittered."""
    delay = min(RETRY_BASE_DELAY * (2 ** max(attempt - 1, 0)), RETRY_MAX_DELAY)
    if jitter:
        delay += delay * random.uniform(0, RETRY_JITTER)
    return round(delay, 3)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"Redirect to {newurl} not permitted for webhook POST", headers, fp
        )


class WebhookDispatcher:
    """Registry, signer, retrying sender and audit log for outbound webhooks."""

    def __init__(self):
        self._endpoints: Dict[str, WebhookEndpoint] = {}
        self._deliveries: List[WebhookDelivery] = []
        self._seen_deliveries: Dict[str, float] = {}
        self._dead_letters: List[Dict[str, Any]] = []
        self._pending_tasks: Set[asyncio.Task] = set()
        self._lock = threading.Lock()
        self._last_state_mtime: float = 0.0
        self._load_state()

    # ── Persistence ──────────────────────────────────────────────────────────
    def _refresh_endpoints_from_disk(self):
        """Re-read endpoints from disk if updated by peer process (serve.py or worker.py)."""
        if not os.path.isfile(STATE_FILE):
            return
        try:
            mtime = os.path.getmtime(STATE_FILE)
            if self._last_state_mtime >= mtime:
                return
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("endpoints", []):
                try:
                    ep = WebhookEndpoint(**item)
                    self._endpoints[ep.endpoint_id] = ep
                except Exception:
                    pass
            self._last_state_mtime = mtime
        except Exception:
            pass

    def _load_state(self):
        if not os.path.isfile(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("endpoints", []):
                try:
                    ep = WebhookEndpoint(**item)
                    self._endpoints[ep.endpoint_id] = ep
                except Exception as ex:
                    log.warning("Skipping malformed webhook endpoint: %s", ex)
            for item in data.get("deliveries", []):
                try:
                    self._deliveries.append(WebhookDelivery(**item))
                except Exception as ex:
                    log.warning("Skipping malformed webhook delivery: %s", ex)
            self._dead_letters = data.get("dead_letters", [])
            self._last_state_mtime = os.path.getmtime(STATE_FILE)
        except Exception as e:
            log.warning("Failed to load webhook state: %s", e)

    def _save_state(self):
        with self._lock:
            try:
                disk_endpoints = {}
                disk_deliveries = []
                disk_dlq = []
                if os.path.isfile(STATE_FILE):
                    try:
                        with open(STATE_FILE, "r", encoding="utf-8") as f:
                            disk_data = json.load(f)
                        for item in disk_data.get("endpoints", []):
                            disk_endpoints[item.get("endpoint_id")] = item
                        disk_deliveries = disk_data.get("deliveries", [])
                        disk_dlq = disk_data.get("dead_letters", [])
                    except Exception:
                        pass

                for ep_id, ep in self._endpoints.items():
                    disk_endpoints[ep_id] = asdict(ep)

                merged_deliveries = {d.get("delivery_id"): d for d in disk_deliveries if d.get("delivery_id")}
                for d in self._deliveries:
                    merged_deliveries[d.delivery_id] = asdict(d)

                merged_dlq = {d.get("delivery_id"): d for d in disk_dlq if d.get("delivery_id")}
                for dl in self._dead_letters:
                    if dl.get("delivery_id"):
                        merged_dlq[dl.get("delivery_id")] = dl

                dir_name = os.path.dirname(STATE_FILE)
                os.makedirs(dir_name, exist_ok=True)
                with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                    json.dump({
                        "endpoints": list(disk_endpoints.values()),
                        "deliveries": list(merged_deliveries.values())[-AUDIT_LOG_LIMIT:],
                        "dead_letters": list(merged_dlq.values())[-100:],
                        "updated_at": time.time(),
                    }, tf, indent=2)
                    temp_path = tf.name
                os.replace(temp_path, STATE_FILE)
                self._last_state_mtime = os.path.getmtime(STATE_FILE)
            except Exception as e:
                log.warning("Failed to save webhook state: %s", e)

    # ── Endpoint registry ────────────────────────────────────────────────────
    def register_endpoint(
        self,
        url: str,
        events: Optional[List[str]] = None,
        secret: Optional[str] = None,
        endpoint_id: Optional[str] = None,
        description: str = "",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        timeout_s: float = DEFAULT_TIMEOUT,
        headers: Optional[Dict[str, str]] = None,
        schema_format: str = "standard",
    ) -> WebhookEndpoint:
        if not url.startswith(("http://", "https://")):
            raise ValueError("Webhook url must be http(s)")

        valid = {e.value for e in WebhookEvent}
        subscribed = events or [e.value for e in WebhookEvent]
        unknown = [e for e in subscribed if e != "*" and e not in valid]
        if unknown:
            raise ValueError(f"Unknown webhook events: {', '.join(unknown)}")

        ep = WebhookEndpoint(
            endpoint_id=endpoint_id or f"whep_{secrets.token_hex(6)}",
            url=url,
            secret=secret or generate_secret(),
            events=subscribed,
            description=description,
            max_attempts=max_attempts,
            timeout_s=timeout_s,
            headers=headers or {},
            schema_format=schema_format,
        )
        self._endpoints[ep.endpoint_id] = ep
        self._save_state()
        log.info("Registered webhook endpoint %s -> %s (%s, format=%s)", ep.endpoint_id, ep.url, ",".join(ep.events), schema_format)
        return ep

    def get_endpoint(self, endpoint_id: str) -> Optional[WebhookEndpoint]:
        return self._endpoints.get(endpoint_id)

    def list_endpoints(self, redact_secret: bool = True) -> List[Dict[str, Any]]:
        return [e.to_dict(redact_secret=redact_secret) for e in self._endpoints.values()]

    def update_endpoint(self, endpoint_id: str, **changes) -> Optional[WebhookEndpoint]:
        ep = self._endpoints.get(endpoint_id)
        if not ep:
            return None
        for key, value in changes.items():
            if hasattr(ep, key) and value is not None:
                setattr(ep, key, value)
        self._save_state()
        return ep

    def rotate_secret(self, endpoint_id: str) -> Optional[WebhookEndpoint]:
        ep = self._endpoints.get(endpoint_id)
        if not ep:
            return None
        ep.secret = generate_secret()
        self._save_state()
        return ep

    def delete_endpoint(self, endpoint_id: str) -> bool:
        removed = self._endpoints.pop(endpoint_id, None) is not None
        if removed:
            self._save_state()
        return removed

    # ── Replay prevention ────────────────────────────────────────────────────
    def is_replay(self, delivery_id: str, now: Optional[float] = None) -> bool:
        """True when this delivery id was already accepted inside the replay window."""
        current = time.time() if now is None else now
        self._prune_seen(current)
        return delivery_id in self._seen_deliveries

    def remember_delivery(self, delivery_id: str, now: Optional[float] = None):
        current = time.time() if now is None else now
        self._seen_deliveries[delivery_id] = current
        if len(self._seen_deliveries) > SEEN_DELIVERY_LIMIT:
            self._prune_seen(current, force=True)

    def _prune_seen(self, now: float, force: bool = False):
        stale = [k for k, v in self._seen_deliveries.items() if now - v > REPLAY_WINDOW_SECONDS]
        for k in stale:
            self._seen_deliveries.pop(k, None)
        if force and len(self._seen_deliveries) > SEEN_DELIVERY_LIMIT:
            for k in sorted(self._seen_deliveries, key=self._seen_deliveries.get)[:len(self._seen_deliveries) // 2]:
                self._seen_deliveries.pop(k, None)

    # ── Sending ──────────────────────────────────────────────────────────────
    def build_request(
        self,
        endpoint: WebhookEndpoint,
        event: str,
        payload: Dict[str, Any],
        delivery_id: str,
        attempt: int = 1,
        timestamp: Optional[int] = None,
        call_id: Optional[str] = None,
    ) -> Tuple[str, Dict[str, str], str]:
        """Returns (body, headers, signature) for one signed delivery attempt."""
        ts = int(time.time()) if timestamp is None else int(timestamp)
        effective_call_id = call_id or payload.get("call_id") or (payload.get("call", {}).get("call_id") if isinstance(payload.get("call"), dict) else None)
        envelope = format_retell_payload(event, payload, delivery_id=delivery_id, call_id=effective_call_id)
        envelope["created_at"] = ts

        # For endpoints configured with format="retell", map dot-notation events to Retell standard names:
        if getattr(endpoint, "schema_format", "standard") == "retell":
            if event in ("call.completed", "call_ended"):
                envelope["event"] = "call_ended"
            elif event in ("call.analyzed", "call_analyzed"):
                envelope["event"] = "call_analyzed"
            elif event in ("call.started", "call_started"):
                envelope["event"] = "call_started"
            else:
                envelope["event"] = event
        else:
            envelope["event"] = event

        body = canonical_body(envelope)
        signature = sign_payload(endpoint.secret, body, ts)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "voice-agent-webhooks/1.0",
            SIGNATURE_HEADER: signature,
            "X-Retell-Signature": signature,
            TIMESTAMP_HEADER: str(ts),
            DELIVERY_HEADER: delivery_id,
            EVENT_HEADER: event,
            "X-Retell-Event": envelope.get("event", event),
            ATTEMPT_HEADER: str(attempt),
        }
        headers.update(endpoint.headers or {})
        return body, headers, signature

    _opener = None

    @classmethod
    def _get_opener(cls):
        if cls._opener is None:
            cls._opener = urllib.request.build_opener(_NoRedirectHandler())
        return cls._opener

    def _post(self, url: str, body: str, headers: Dict[str, str], timeout: float) -> int:
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        opener = self._get_opener()
        with opener.open(req, timeout=timeout) as resp:
            return resp.status

    async def deliver(
        self,
        endpoint: WebhookEndpoint,
        event: str,
        payload: Dict[str, Any],
        call_id: Optional[str] = None,
        sleep: bool = True,
    ) -> WebhookDelivery:
        """Sends one event to one endpoint, retrying with exponential backoff."""
        delivery_id = f"whd_{secrets.token_hex(8)}"
        timestamp = int(time.time())
        body, headers, signature = self.build_request(endpoint, event, payload, delivery_id, 1, timestamp, call_id=call_id)

        record = WebhookDelivery(
            delivery_id=delivery_id,
            endpoint_id=endpoint.endpoint_id,
            url=endpoint.url,
            event=event,
            call_id=call_id,
            payload_digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            signature=signature,
            timestamp=timestamp,
            payload=payload,
        )
        self._deliveries.append(record)

        for attempt in range(1, max(endpoint.max_attempts, 1) + 1):
            headers[ATTEMPT_HEADER] = str(attempt)
            started = time.time()
            try:
                status_code = await asyncio.to_thread(
                    self._post, endpoint.url, body, headers, endpoint.timeout_s
                )
                duration_ms = round((time.time() - started) * 1000, 2)
                record.attempts.append(DeliveryAttempt(attempt, started, duration_ms, status_code).to_dict())
                record.status = DeliveryStatus.DELIVERED.value
                record.completed_at = time.time()
                record.last_error = None
                log.info("Webhook %s %s -> %s (%s, attempt %d)",
                         delivery_id, event, endpoint.url, status_code, attempt)
                break
            except Exception as err:
                duration_ms = round((time.time() - started) * 1000, 2)
                status_code = getattr(err, "code", None)
                is_last = attempt >= max(endpoint.max_attempts, 1)
                delay = None if is_last else backoff_delay(attempt)
                record.attempts.append(
                    DeliveryAttempt(attempt, started, duration_ms, status_code, str(err), delay).to_dict()
                )
                record.last_error = str(err)
                log.warning("Webhook %s attempt %d failed (%s); retry in %ss",
                             delivery_id, attempt, err, delay)
                if is_last:
                    record.status = DeliveryStatus.DEAD_LETTERED.value
                    record.completed_at = time.time()
                    self._dead_letters.append({
                        "delivery_id": delivery_id,
                        "endpoint_id": endpoint.endpoint_id,
                        "event": event,
                        "call_id": call_id,
                        "attempts": len(record.attempts),
                        "last_error": str(err),
                        "parked_at": time.time(),
                    })
                    break
                record.status = DeliveryStatus.RETRYING.value
                if sleep and delay:
                    await asyncio.sleep(delay)

        self._trim_audit_log()
        self._save_state()
        return record

    async def dispatch(
        self,
        event: str,
        payload: Dict[str, Any],
        call_id: Optional[str] = None,
        sleep: bool = True,
    ) -> List[WebhookDelivery]:
        """Fans one event out to every subscribed, active endpoint."""
        self._refresh_endpoints_from_disk()
        targets = [e for e in self._endpoints.values() if e.subscribes_to(event)]
        if not targets:
            return []
        results = await asyncio.gather(
            *[self.deliver(ep, event, payload, call_id, sleep) for ep in targets],
            return_exceptions=True,
        )
        deliveries = []
        for r in results:
            if isinstance(r, WebhookDelivery):
                deliveries.append(r)
            elif isinstance(r, Exception):
                log.error("Webhook delivery unexpected error for event '%s': %s", event, r, exc_info=r)
        return deliveries

    def dispatch_soon(self, event: str, payload: Dict[str, Any], call_id: Optional[str] = None):
        """Fire-and-forget dispatch from sync code inside a running event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(self.dispatch(event, payload, call_id))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    async def wait_pending_dispatches(self, timeout: float = 5.0) -> None:
        """Awaits any pending dispatch tasks before loop teardown."""
        if not self._pending_tasks:
            return
        pending = [t for t in self._pending_tasks if not t.done()]
        if pending:
            try:
                await asyncio.wait(pending, timeout=timeout)
            except Exception:
                pass

    async def replay_delivery(self, delivery_id: str) -> Optional[WebhookDelivery]:
        """Re-sends a dead-lettered or failed delivery as a fresh, newly signed request."""
        self._refresh_endpoints_from_disk()
        original = next((d for d in self._deliveries if d.delivery_id == delivery_id), None)
        if not original:
            return None
        endpoint = self._endpoints.get(original.endpoint_id)
        if not endpoint:
            return None
        # Preserve original payload so real call metrics/transcripts are sent instead of blanks
        payload = dict(original.payload) if original.payload else {"replay_of": delivery_id, "call_id": original.call_id}
        payload["is_replay"] = True

        # Remove from dead letters on replay to prevent unbounded accumulation
        self._dead_letters = [dl for dl in self._dead_letters if dl.get("delivery_id") != delivery_id]
        self._save_state()

        return await self.deliver(endpoint, original.event, payload, original.call_id)

    # ── Audit log ────────────────────────────────────────────────────────────
    def _trim_audit_log(self):
        if len(self._deliveries) > AUDIT_LOG_LIMIT:
            self._deliveries = self._deliveries[-AUDIT_LOG_LIMIT:]

    def list_deliveries(
        self,
        endpoint_id: Optional[str] = None,
        event: Optional[str] = None,
        status: Optional[str] = None,
        call_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        rows = list(reversed(self._deliveries))
        if endpoint_id:
            rows = [d for d in rows if d.endpoint_id == endpoint_id]
        if event:
            rows = [d for d in rows if d.event == event]
        if status:
            rows = [d for d in rows if d.status == status]
        if call_id:
            rows = [d for d in rows if d.call_id == call_id]
        return [d.to_dict() for d in rows[:limit]]

    def get_delivery(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        for d in self._deliveries:
            if d.delivery_id == delivery_id:
                return d.to_dict()
        return None

    def list_dead_letters(self, limit: int = 50) -> List[Dict[str, Any]]:
        return list(reversed(self._dead_letters))[:limit]

    def get_stats(self) -> Dict[str, Any]:
        delivered = sum(1 for d in self._deliveries if d.status == DeliveryStatus.DELIVERED.value)
        dead = sum(1 for d in self._deliveries if d.status == DeliveryStatus.DEAD_LETTERED.value)
        total_attempts = sum(d.attempt_count for d in self._deliveries)
        latencies = [
            a["duration_ms"] for d in self._deliveries for a in d.attempts
            if a.get("status_code") and 200 <= a["status_code"] < 300
        ]
        return {
            "endpoints": len(self._endpoints),
            "active_endpoints": sum(1 for e in self._endpoints.values() if e.active),
            "deliveries": len(self._deliveries),
            "delivered": delivered,
            "dead_lettered": dead,
            "success_rate": round(delivered / len(self._deliveries), 4) if self._deliveries else 0.0,
            "total_attempts": total_attempts,
            "avg_attempts": round(total_attempts / len(self._deliveries), 2) if self._deliveries else 0.0,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
            "dead_letter_queue": len(self._dead_letters),
        }

    def reset(self):
        """Clears endpoints, audit log and dead letters (used by acceptance tests)."""
        self._endpoints.clear()
        self._deliveries.clear()
        self._dead_letters.clear()
        self._seen_deliveries.clear()

    def persist(self):
        """Writes the current registry and audit log to disk."""
        self._save_state()

    # ── Pipeline integration ─────────────────────────────────────────────────
    def attach_to_pipeline(self, worker) -> Callable[[Dict[str, Any]], Any]:
        """Subscribes to a PostCallPipelineWorker and emits webhooks for its events.

        Stage completions map onto the public event names external systems
        subscribe to, so integrators never see internal stage vocabulary.
        """
        stage_events = {
            "transcript_normalization": WebhookEvent.CALL_TRANSCRIBED.value,
            "sentiment_analysis": WebhookEvent.CALL_ANALYZED.value,
            "schema_extraction": WebhookEvent.CALL_EXTRACTED.value,
        }

        def on_pipeline_event(evt: Dict[str, Any]):
            event_name = None
            if evt.get("event") == "job_completed":
                event_name = WebhookEvent.CALL_COMPLETED.value
            elif evt.get("event") == "job_failed":
                event_name = WebhookEvent.CALL_FAILED.value
            elif evt.get("event") == "stage_completed":
                event_name = stage_events.get(evt.get("stage"))
            if not event_name:
                return None

            payload = {k: v for k, v in evt.items() if k != "type"}
            job = worker.get_job(job_id=evt.get("job_id"))
            if job is not None:
                # Per-agent opt-in: an agent with "Webhook & data" switched off posts nothing.
                try:
                    from agent.agent_builder import agent_builder
                    md0 = job.metadata or {}
                    cfg = agent_builder.get_agent(md0.get("agent_id") or "")
                    if cfg is None and md0.get("agent_name"):
                        cfg = next((agent_builder.get_agent(a["agent_id"]) for a in agent_builder.list_agents()
                                    if a["name"] == md0.get("agent_name")), None)
                    if cfg is not None and not cfg.webhook_enabled:
                        return None
                except Exception:
                    pass
                payload["metrics"] = job.metrics
                payload["archive_url"] = job.archive_url
                md = job.metadata or {}
                payload["call"] = {
                    "call_id": job.call_id, "room": job.room_name,
                    "agent_id": md.get("agent_id"), "agent_name": md.get("agent_name"),
                    "direction": md.get("direction"), "from_number": md.get("from_number"), "to_number": md.get("to_number"),
                    "started_at": md.get("started_at") or job.created_at, "duration_seconds": md.get("duration_seconds"),
                }
                payload["summary"] = md.get("summary")
                payload["sentiment"] = md.get("sentiment")
                # Flat key→value data the AI pulled out of the transcript (agent's own fields first).
                agent_x = md.get("agent_extraction") or {}
                payload["extracted"] = agent_x.get("values") or {}
                payload["extraction"] = {"agent_fields": agent_x, "schemas": md.get("crm_payloads") or {}}
                if event_name in (
                    WebhookEvent.CALL_COMPLETED.value,
                    WebhookEvent.CALL_EXTRACTED.value,
                    WebhookEvent.CALL_TRANSCRIBED.value,
                    WebhookEvent.CALL_ANALYZED.value,
                ):
                    payload["transcript"] = [{"speaker": t.get("speaker"), "role": t.get("role"), "text": t.get("text")}
                                             for t in (job.transcript_turns or [])]
            return self.dispatch_soon(event_name, payload, call_id=evt.get("call_id"))

        worker.subscribe(on_pipeline_event)
        return on_pipeline_event


# Module-level singleton used by the worker, REST API and acceptance tests.
webhook_dispatcher = WebhookDispatcher()
