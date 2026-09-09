#!/usr/bin/env python3
"""scripts/verify_webhooks.py — Task 3.4 Acceptance Tests.

7 checks:
  Check 1: Endpoint registry, secrets & event subscription filtering
  Check 2: HMAC-SHA256 signing and receiver-side verification
  Check 3: Replay prevention (timestamp window + delivery-id de-duplication)
  Check 4: Live signed delivery to an HTTP receiver (headers + envelope)
  Check 5: Exponential backoff retries and dead-letter parking
  Check 6: Delivery audit log, stats & pipeline event integration
  Check 7: REST API endpoints for webhook management
"""
import asyncio
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.webhook_dispatcher import (  # noqa: E402
    DeliveryStatus,
    WebhookEvent,
    backoff_delay,
    canonical_body,
    generate_secret,
    sign_payload,
    verify_signature,
    webhook_dispatcher,
    REPLAY_WINDOW_SECONDS,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    DELIVERY_HEADER,
    EVENT_HEADER,
    ATTEMPT_HEADER,
)
from agent.pipeline_worker import PostCallPipelineWorker  # noqa: E402

PASS = "\033[32m✔\033[0m"
FAIL = "\033[31m✘\033[0m"
failures = []
total_checks = 0

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
BASE_URL = f"http://localhost:{PORT}"


def check(name: str, condition: bool, detail: str = ""):
    global total_checks
    total_checks += 1
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}{' -- ' + detail if detail else ''}")
        failures.append(name)


# ─── Local receiver ───────────────────────────────────────────────────────────
RECEIVED = []          # every request the receiver accepted
FLAKY_HITS = {"n": 0}  # /flaky fails twice before succeeding


class ReceiverHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        RECEIVED.append({
            "path": self.path,
            "body": body,
            "headers": {k: v for k, v in self.headers.items()},
        })

        if self.path == "/down":
            self.send_error(503, "Service Unavailable")
            return
        if self.path == "/flaky":
            FLAKY_HITS["n"] += 1
            if FLAKY_HITS["n"] < 3:
                self.send_error(500, "Transient")
                return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


receiver = ThreadingHTTPServer(("127.0.0.1", 0), ReceiverHandler)
RECEIVER_PORT = receiver.server_address[1]
RECEIVER_URL = f"http://127.0.0.1:{RECEIVER_PORT}"
threading.Thread(target=receiver.serve_forever, daemon=True).start()

webhook_dispatcher.reset()

# ─── Check 1: Endpoint Registry ───────────────────────────────────────────────
print("\n- Check 1: Endpoint Registry & Event Subscriptions -")
ep_all = webhook_dispatcher.register_endpoint(
    url=f"{RECEIVER_URL}/hook",
    description="All events",
)
ep_completed = webhook_dispatcher.register_endpoint(
    url=f"{RECEIVER_URL}/hook",
    events=[WebhookEvent.CALL_COMPLETED.value],
    description="Completed only",
)

check("endpoint_id minted",                 ep_all.endpoint_id.startswith("whep_"), ep_all.endpoint_id)
check("secret auto-generated",              ep_all.secret.startswith("whsec_") and len(ep_all.secret) > 20)
check("secrets are unique per endpoint",    ep_all.secret != ep_completed.secret)
check("defaults to all events",             set(ep_all.events) == {e.value for e in WebhookEvent})
check("both endpoints registered",          len(webhook_dispatcher.list_endpoints()) == 2)
check("list_endpoints redacts secret",
      "..." in webhook_dispatcher.list_endpoints()[0]["secret"],
      webhook_dispatcher.list_endpoints()[0]["secret"])
check("subscribes_to matching event",       ep_completed.subscribes_to(WebhookEvent.CALL_COMPLETED.value))
check("filters unsubscribed event",         not ep_completed.subscribes_to(WebhookEvent.CALL_ANALYZED.value))
check("inactive endpoint receives nothing",
      not webhook_dispatcher.update_endpoint(ep_completed.endpoint_id, active=False)
      .subscribes_to(WebhookEvent.CALL_COMPLETED.value))
webhook_dispatcher.update_endpoint(ep_completed.endpoint_id, active=True)

rotated = webhook_dispatcher.rotate_secret(ep_completed.endpoint_id)
check("rotate_secret issues a new secret",  rotated.secret != ep_all.secret and rotated.secret.startswith("whsec_"))

bad_url = bad_event = False
try:
    webhook_dispatcher.register_endpoint(url="ftp://example.com/hook")
except ValueError:
    bad_url = True
try:
    webhook_dispatcher.register_endpoint(url=f"{RECEIVER_URL}/hook", events=["call.exploded"])
except ValueError:
    bad_event = True
check("rejects non-http url",               bad_url)
check("rejects unknown event name",         bad_event)

# ─── Check 2: HMAC-SHA256 Signing ─────────────────────────────────────────────
print("\n- Check 2: HMAC-SHA256 Signing & Verification -")
secret = generate_secret()
payload = {"event": "call.completed", "call_id": "call_8f3a2c", "duration_ms": 192000}
body = canonical_body(payload)
ts = int(time.time())
signature = sign_payload(secret, body, ts)

check("signature has sha256= prefix",       signature.startswith("sha256="))
check("signature is 64 hex chars",          len(signature.split("=", 1)[1]) == 64)
check("signing is deterministic",           sign_payload(secret, body, ts) == signature)
check("canonical body is key-sorted",       body.startswith('{"call_id"'), body[:20])

ok, reason = verify_signature(secret, body, ts, signature)
check("valid signature verifies",           ok and reason == "ok", reason)

ok_tampered, reason_tampered = verify_signature(
    secret, body.replace("192000", "999999"), ts, signature)
check("tampered body rejected",             not ok_tampered and reason_tampered == "signature_mismatch", reason_tampered)

ok_secret, reason_secret = verify_signature(generate_secret(), body, ts, signature)
check("wrong secret rejected",              not ok_secret and reason_secret == "signature_mismatch", reason_secret)

ok_ts, reason_ts = verify_signature(secret, body, ts + 1, signature)
check("timestamp is part of signature",     not ok_ts, reason_ts)
check("different timestamp -> different signature", sign_payload(secret, body, ts + 1) != signature)

# ─── Check 3: Replay Prevention ───────────────────────────────────────────────
print("\n- Check 3: Replay Prevention (window + de-duplication) -")
stale_ts = ts - (REPLAY_WINDOW_SECONDS + 60)
stale_sig = sign_payload(secret, body, stale_ts)
ok_stale, reason_stale = verify_signature(secret, body, stale_ts, stale_sig)
check("stale timestamp rejected",           not ok_stale and reason_stale == "timestamp_too_old", reason_stale)

future_ts = ts + (REPLAY_WINDOW_SECONDS + 60)
future_sig = sign_payload(secret, body, future_ts)
ok_future, reason_future = verify_signature(secret, body, future_ts, future_sig)
check("future timestamp rejected",          not ok_future and reason_future == "timestamp_in_future", reason_future)

edge_ts = ts - (REPLAY_WINDOW_SECONDS - 30)
ok_edge, _ = verify_signature(secret, body, edge_ts, sign_payload(secret, body, edge_ts))
check("timestamp inside window accepted",   ok_edge)

ok_bad_ts, reason_bad_ts = verify_signature(secret, body, "not-a-number", signature)
check("non-numeric timestamp rejected",     not ok_bad_ts and reason_bad_ts == "invalid_timestamp", reason_bad_ts)

check("unseen delivery id is not a replay", not webhook_dispatcher.is_replay("whd_first"))
webhook_dispatcher.remember_delivery("whd_first")
check("repeated delivery id is a replay",   webhook_dispatcher.is_replay("whd_first"))
webhook_dispatcher.remember_delivery("whd_expired", now=time.time() - (REPLAY_WINDOW_SECONDS + 60))
check("expired delivery id pruned",         not webhook_dispatcher.is_replay("whd_expired"))

# ─── Check 4: Live Signed Delivery ────────────────────────────────────────────
print("\n- Check 4: Live Signed Delivery to HTTP Receiver -")
RECEIVED.clear()
record = asyncio.run(webhook_dispatcher.deliver(
    ep_all,
    WebhookEvent.CALL_COMPLETED.value,
    {"call_id": "call_live_001", "duration_ms": 192000},
    call_id="call_live_001",
))
check("delivery status=delivered",          record.status == DeliveryStatus.DELIVERED.value, record.status)
check("delivered on first attempt",         record.attempt_count == 1, str(record.attempt_count))
check("receiver got exactly one request",   len(RECEIVED) == 1, str(len(RECEIVED)))

if RECEIVED:
    got = RECEIVED[0]
    hdrs = got["headers"]
    check(f"{SIGNATURE_HEADER} header sent",    SIGNATURE_HEADER in hdrs)
    check(f"{TIMESTAMP_HEADER} header sent",    TIMESTAMP_HEADER in hdrs)
    check(f"{DELIVERY_HEADER} header sent",     hdrs.get(DELIVERY_HEADER) == record.delivery_id)
    check(f"{EVENT_HEADER} header sent",        hdrs.get(EVENT_HEADER) == WebhookEvent.CALL_COMPLETED.value)
    check(f"{ATTEMPT_HEADER} header sent",      hdrs.get(ATTEMPT_HEADER) == "1")
    check("content-type is application/json",   hdrs.get("Content-Type") == "application/json")

    verified, verify_reason = verify_signature(
        ep_all.secret, got["body"], hdrs.get(TIMESTAMP_HEADER), hdrs.get(SIGNATURE_HEADER))
    check("receiver verifies the signature",    verified, verify_reason)

    envelope = json.loads(got["body"])
    check("envelope carries delivery id",       envelope.get("id") == record.delivery_id)
    check("envelope carries event name",        envelope.get("event") == WebhookEvent.CALL_COMPLETED.value)
    check("envelope wraps payload in data",     envelope.get("data", {}).get("call_id") == "call_live_001")
    check("payload digest recorded",            len(record.payload_digest) == 64)

# Fan-out: both endpoints subscribe to call.completed, only one to call.analyzed.
RECEIVED.clear()
fanout = asyncio.run(webhook_dispatcher.dispatch(
    WebhookEvent.CALL_COMPLETED.value, {"call_id": "call_fanout"}, call_id="call_fanout"))
check("dispatch fans out to 2 endpoints",   len(fanout) == 2, str(len(fanout)))

RECEIVED.clear()
analyzed = asyncio.run(webhook_dispatcher.dispatch(
    WebhookEvent.CALL_ANALYZED.value, {"call_id": "call_fanout"}, call_id="call_fanout"))
check("unsubscribed endpoint skipped",      len(analyzed) == 1, str(len(analyzed)))

# ─── Check 5: Retries & Dead Letters ──────────────────────────────────────────
print("\n- Check 5: Exponential Backoff Retries & Dead Letters -")
check("backoff grows exponentially",
      backoff_delay(1, jitter=False) == 1.0 and backoff_delay(2, jitter=False) == 2.0
      and backoff_delay(3, jitter=False) == 4.0,
      f"{backoff_delay(1, False)}, {backoff_delay(2, False)}, {backoff_delay(3, False)}")
check("backoff is capped",                  backoff_delay(20, jitter=False) <= 30.0)
check("jitter stays within 25%",
      all(backoff_delay(2) <= 2.0 * 1.25 for _ in range(20)))

flaky_ep = webhook_dispatcher.register_endpoint(
    url=f"{RECEIVER_URL}/flaky", description="Fails twice then succeeds", max_attempts=4)
FLAKY_HITS["n"] = 0
RECEIVED.clear()
started = time.time()
flaky_record = asyncio.run(webhook_dispatcher.deliver(
    flaky_ep, WebhookEvent.CALL_COMPLETED.value, {"call_id": "call_flaky"}, call_id="call_flaky"))
elapsed = time.time() - started

check("recovers after transient failures",  flaky_record.status == DeliveryStatus.DELIVERED.value,
      flaky_record.status)
check("took exactly 3 attempts",            flaky_record.attempt_count == 3, str(flaky_record.attempt_count))
check("waited for backoff between attempts", elapsed >= 3.0, f"elapsed={elapsed:.2f}s")
check("failed attempts recorded with error", flaky_record.attempts[0].get("error") is not None)
check("failed attempt records HTTP code",   flaky_record.attempts[0].get("status_code") == 500,
      str(flaky_record.attempts[0].get("status_code")))
check("retry delay stored on attempt",      flaky_record.attempts[0].get("next_retry_in_s") is not None)
check("attempt numbers increment",
      [a["attempt"] for a in flaky_record.attempts] == [1, 2, 3])
check("attempt header tracks retry count",
      RECEIVED[-1]["headers"].get(ATTEMPT_HEADER) == "3" if RECEIVED else False)

down_ep = webhook_dispatcher.register_endpoint(
    url=f"{RECEIVER_URL}/down", description="Always down", max_attempts=3)
dead_record = asyncio.run(webhook_dispatcher.deliver(
    down_ep, WebhookEvent.CALL_FAILED.value, {"call_id": "call_dead"},
    call_id="call_dead", sleep=False))
check("exhausted delivery is dead-lettered", dead_record.status == DeliveryStatus.DEAD_LETTERED.value,
      dead_record.status)
check("stopped at max_attempts",            dead_record.attempt_count == 3, str(dead_record.attempt_count))
check("last attempt has no retry scheduled", dead_record.attempts[-1].get("next_retry_in_s") is None)
check("dead letter queued",
      any(d["delivery_id"] == dead_record.delivery_id for d in webhook_dispatcher.list_dead_letters()))

replayed = asyncio.run(webhook_dispatcher.replay_delivery(dead_record.delivery_id))
check("dead letter can be replayed",        replayed is not None and replayed.delivery_id != dead_record.delivery_id)
check("replay is signed afresh",            replayed.signature != dead_record.signature)

# ─── Check 6: Audit Log & Pipeline Integration ────────────────────────────────
print("\n- Check 6: Delivery Audit Log & Pipeline Integration -")
audit = webhook_dispatcher.list_deliveries(limit=100)
check("audit log records every delivery",   len(audit) >= 6, str(len(audit)))
check("audit rows are newest first",        audit[0]["created_at"] >= audit[-1]["created_at"])
check("audit filter by event",
      all(d["event"] == WebhookEvent.CALL_COMPLETED.value
          for d in webhook_dispatcher.list_deliveries(event=WebhookEvent.CALL_COMPLETED.value)))
check("audit filter by status",
      all(d["status"] == DeliveryStatus.DEAD_LETTERED.value
          for d in webhook_dispatcher.list_deliveries(status=DeliveryStatus.DEAD_LETTERED.value)))
check("audit filter by call_id",
      len(webhook_dispatcher.list_deliveries(call_id="call_live_001")) == 1)
check("get_delivery by id",                 webhook_dispatcher.get_delivery(record.delivery_id) is not None)
check("audit log persisted to disk",
      os.path.isfile(os.path.join(ROOT, "recordings", "webhook_state.json")))

stats = webhook_dispatcher.get_stats()
check("stats count deliveries",             stats["deliveries"] == len(audit), str(stats["deliveries"]))
check("stats count dead letters",           stats["dead_lettered"] >= 1)
check("stats compute success rate",         0.0 < stats["success_rate"] <= 1.0, str(stats["success_rate"]))
check("stats track attempts",               stats["avg_attempts"] >= 1.0)

# Pipeline: a completed job must emit call.completed to subscribed endpoints.
worker = PostCallPipelineWorker(concurrency=1)
webhook_dispatcher.attach_to_pipeline(worker)
RECEIVED.clear()
before = len(webhook_dispatcher.list_deliveries(limit=500))

PIPELINE_TURNS = [
    {"role": "caller", "text": "My account number ACC-8821 has a $149.99 overcharge. I want a refund."},
    {"role": "agent",  "text": "I have raised the refund request for you."},
]


async def run_pipeline():
    job = worker.enqueue_call(
        call_id=f"verify-webhooks-{int(time.time())}",
        room_name="room-webhooks",
        transcript_turns=PIPELINE_TURNS,
        priority=1,
    )
    done = await worker.execute_job(job.job_id)
    # Let the fire-and-forget webhook tasks finish before the loop closes.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return done


pipeline_job = asyncio.run(run_pipeline())
after_rows = webhook_dispatcher.list_deliveries(limit=500)
emitted = after_rows[:len(after_rows) - before]
emitted_events = {d["event"] for d in emitted}

check("pipeline job completed",             pipeline_job.status == "completed", pipeline_job.status)
check("pipeline emitted webhooks",          len(emitted) > 0, str(len(emitted)))
check("call.completed emitted",             WebhookEvent.CALL_COMPLETED.value in emitted_events,
      str(emitted_events))
check("call.transcribed emitted",           WebhookEvent.CALL_TRANSCRIBED.value in emitted_events,
      str(emitted_events))
check("call.analyzed emitted",              WebhookEvent.CALL_ANALYZED.value in emitted_events,
      str(emitted_events))
check("emitted webhooks carry call_id",     all(d["call_id"] == pipeline_job.call_id for d in emitted))
check("internal stage names not leaked",    "stage_completed" not in emitted_events)

# ─── Check 7: REST API Endpoints ──────────────────────────────────────────────
print("\n- Check 7: REST API Endpoints -")


def api_post(path, body):
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


try:
    with urllib.request.urlopen(f"{BASE_URL}/api/webhooks/stats", timeout=5) as resp:
        stats_resp = json.loads(resp.read())
    check("GET /api/webhooks/stats status=ok",   stats_resp.get("status") == "ok")
    check("stats endpoint lists event types",
          WebhookEvent.CALL_ANALYZED.value in stats_resp.get("events", []))

    reg = api_post("/api/webhooks/register", {
        "url": f"{RECEIVER_URL}/hook",
        "events": [WebhookEvent.CALL_COMPLETED.value],
        "description": "REST registered",
    })
    check("POST /api/webhooks/register status=ok", reg.get("status") == "ok")
    api_endpoint_id = reg.get("endpoint", {}).get("endpoint_id", "")
    api_secret = reg.get("endpoint", {}).get("secret", "")
    check("register returns endpoint_id",        api_endpoint_id.startswith("whep_"), api_endpoint_id)
    check("register returns plaintext secret",   api_secret.startswith("whsec_"))

    with urllib.request.urlopen(f"{BASE_URL}/api/webhooks/endpoints", timeout=5) as resp:
        eps_resp = json.loads(resp.read())
    check("GET /api/webhooks/endpoints status=ok", eps_resp.get("status") == "ok")
    check("registered endpoint appears in list",
          any(e["endpoint_id"] == api_endpoint_id for e in eps_resp.get("endpoints", [])))
    check("listed endpoint secret is redacted",
          all("..." in e["secret"] or e["secret"] == "***" for e in eps_resp.get("endpoints", [])))

    RECEIVED.clear()
    api_call_id = f"api-test-{int(time.time())}"
    test_resp = api_post("/api/webhooks/test", {
        "endpoint_id": api_endpoint_id,
        "event": WebhookEvent.CALL_COMPLETED.value,
        "payload": {"call_id": api_call_id},
        "call_id": api_call_id,
    })
    check("POST /api/webhooks/test status=ok",   test_resp.get("status") == "ok")
    check("test delivery succeeded",
          test_resp.get("delivery", {}).get("status") == DeliveryStatus.DELIVERED.value,
          str(test_resp.get("delivery", {}).get("status")))
    check("test delivery reached receiver",      len(RECEIVED) == 1, str(len(RECEIVED)))
    if RECEIVED:
        verified_api, reason_api = verify_signature(
            api_secret,
            RECEIVED[0]["body"],
            RECEIVED[0]["headers"].get(TIMESTAMP_HEADER),
            RECEIVED[0]["headers"].get(SIGNATURE_HEADER),
        )
        check("API-sent webhook verifies with returned secret", verified_api, reason_api)

    with urllib.request.urlopen(
            f"{BASE_URL}/api/webhooks/deliveries?call_id={api_call_id}", timeout=5) as resp:
        deliv_resp = json.loads(resp.read())
    check("GET /api/webhooks/deliveries status=ok", deliv_resp.get("status") == "ok")
    check("deliveries filtered by call_id",
          len(deliv_resp.get("deliveries", [])) == 1, str(len(deliv_resp.get("deliveries", []))))

    rot = api_post("/api/webhooks/rotate", {"endpoint_id": api_endpoint_id})
    check("POST /api/webhooks/rotate status=ok", rot.get("status") == "ok")
    check("rotated secret differs",              rot.get("endpoint", {}).get("secret") != api_secret)

    delete_resp = api_post("/api/webhooks/delete", {"endpoint_id": api_endpoint_id})
    check("POST /api/webhooks/delete status=ok", delete_resp.get("removed") is True)

    missing_status = None
    try:
        api_post("/api/webhooks/test", {"endpoint_id": "whep_missing"})
    except urllib.error.HTTPError as e:
        missing_status = e.code
    check("unknown endpoint returns 404",        missing_status == 404, str(missing_status))

except urllib.error.URLError as e:
    print(f"  {FAIL} REST API check skipped -- server error: {e}")
    failures.append("REST API not reachable")

receiver.shutdown()

# Leave no test endpoints pointing at the now-dead receiver in the saved state.
webhook_dispatcher.reset()
webhook_dispatcher.persist()

# ─── Summary ──────────────────────────────────────────────────────────────────
passed = total_checks - len(failures)
print(f"\n{'='*55}")
print(f"  Task 3.4 Webhooks: {passed}/{total_checks} checks passed")
if failures:
    print(f"  FAILED: {', '.join(failures)}")
print(f"{'='*55}\n")
sys.exit(0 if not failures else 1)
