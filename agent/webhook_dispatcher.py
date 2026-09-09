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
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

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

    def subscribes_to(self, event: str) -> bool:
        return self.active and ("*" in self.events or event in self.events)

    def to_dict(self, redact_secret: bool = True) -> Dict[str, Any]:
        data = asdict(self)
        if redact_secret:
            data["secret"] = f"{self.secret[:6]}...{self.secret[-4:]}" if len(self.secret) > 12 else "***"
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

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["attempt_count"] = self.attempt_count
        return data


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
    """Verifies a received webhook. Returns (ok, reason).

    Receivers get the same checks the dispatcher makes: the timestamp must be
    inside the replay window and the signature must match in constant time.
    """
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
    if not hmac.compare_digest(expected, signature or ""):
        return False, "signature_mismatch"
    return True, "ok"


def backoff_delay(attempt: int, jitter: bool = True) -> float:
    """Exponential backoff for `attempt` (1-based), capped and optionally jittered."""
    delay = min(RETRY_BASE_DELAY * (2 ** max(attempt - 1, 0)), RETRY_MAX_DELAY)
    if jitter:
        delay += delay * random.uniform(0, RETRY_JITTER)
    return round(delay, 3)


class WebhookDispatcher:
    """Registry, signer, retrying sender and audit log for outbound webhooks."""

    def __init__(self):
        self._endpoints: Dict[str, WebhookEndpoint] = {}
        self._deliveries: List[WebhookDelivery] = []
        self._seen_deliveries: Dict[str, float] = {}
        self._dead_letters: List[Dict[str, Any]] = []
        self._load_state()

    # ── Persistence ──────────────────────────────────────────────────────────
    def _load_state(self):
        if not os.path.isfile(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("endpoints", []):
                ep = WebhookEndpoint(**item)
                self._endpoints[ep.endpoint_id] = ep
            for item in data.get("deliveries", []):
                self._deliveries.append(WebhookDelivery(**item))
            self._dead_letters = data.get("dead_letters", [])
        except Exception as e:
            log.warning("Failed to load webhook state: %s", e)

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "endpoints": [asdict(e) for e in self._endpoints.values()],
                    "deliveries": [asdict(d) for d in self._deliveries[-AUDIT_LOG_LIMIT:]],
                    "dead_letters": self._dead_letters[-100:],
                    "updated_at": time.time(),
                }, f, indent=2)
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
        )
        self._endpoints[ep.endpoint_id] = ep
        self._save_state()
        log.info("Registered webhook endpoint %s -> %s (%s)", ep.endpoint_id, ep.url, ",".join(ep.events))
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
    ) -> Tuple[str, Dict[str, str], str]:
        """Returns (body, headers, signature) for one signed delivery attempt."""
        ts = int(time.time()) if timestamp is None else int(timestamp)
        envelope = {
            "id": delivery_id,
            "event": event,
            "created_at": ts,
            "data": payload,
        }
        body = canonical_body(envelope)
        signature = sign_payload(endpoint.secret, body, ts)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "voice-agent-webhooks/1.0",
            SIGNATURE_HEADER: signature,
            TIMESTAMP_HEADER: str(ts),
            DELIVERY_HEADER: delivery_id,
            EVENT_HEADER: event,
            ATTEMPT_HEADER: str(attempt),
        }
        headers.update(endpoint.headers or {})
        return body, headers, signature

    def _post(self, url: str, body: str, headers: Dict[str, str], timeout: float) -> int:
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        body, headers, signature = self.build_request(endpoint, event, payload, delivery_id, 1, timestamp)

        record = WebhookDelivery(
            delivery_id=delivery_id,
            endpoint_id=endpoint.endpoint_id,
            url=endpoint.url,
            event=event,
            call_id=call_id,
            payload_digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            signature=signature,
            timestamp=timestamp,
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
        targets = [e for e in self._endpoints.values() if e.subscribes_to(event)]
        if not targets:
            return []
        results = await asyncio.gather(
            *[self.deliver(ep, event, payload, call_id, sleep) for ep in targets],
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, WebhookDelivery)]

    def dispatch_soon(self, event: str, payload: Dict[str, Any], call_id: Optional[str] = None):
        """Fire-and-forget dispatch from sync code inside a running event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        return loop.create_task(self.dispatch(event, payload, call_id))

    async def replay_delivery(self, delivery_id: str) -> Optional[WebhookDelivery]:
        """Re-sends a dead-lettered or failed delivery as a fresh, newly signed request."""
        original = next((d for d in self._deliveries if d.delivery_id == delivery_id), None)
        if not original:
            return None
        endpoint = self._endpoints.get(original.endpoint_id)
        if not endpoint:
            return None
        payload = {"replay_of": delivery_id, "call_id": original.call_id}
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
                payload["metrics"] = job.metrics
                payload["archive_url"] = job.archive_url
            return self.dispatch_soon(event_name, payload, call_id=evt.get("call_id"))

        worker.subscribe(on_pipeline_event)
        return on_pipeline_event


# Module-level singleton used by the worker, REST API and acceptance tests.
webhook_dispatcher = WebhookDispatcher()
