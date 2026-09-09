#!/usr/bin/env python3
"""scripts/verify_extraction.py — Task 3.3 Acceptance Tests.

7 checks:
  Check 1: Schema registry & dynamic schema registration
  Check 2: Typed field extraction (string, number, phone, email, date, boolean, enum)
  Check 3: Confidence scoring (pattern > proximity > default > missing)
  Check 4: Multi-schema extraction and CRM payload generation
  Check 5: Custom schema registration and field validation
  Check 6: Pipeline worker integration (6-stage execution & manifest with extractions)
  Check 7: REST API endpoints for extraction
"""
import asyncio
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.schema_extractor import (
    schema_extractor, register_schema, list_schemas, get_schema,
    FieldType, FieldResult, ExtractionResult,
)
from agent.pipeline_worker import PostCallPipelineWorker, PipelineStage, StageStatus

PASS = "\033[32m\u2714\033[0m"
FAIL = "\033[31m\u2718\033[0m"
failures = []

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
BASE_URL = f"http://localhost:{PORT}"


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}{' -- ' + detail if detail else ''}")
        failures.append(name)


# ─── Check 1: Schema Registry ─────────────────────────────────────────────────
print("\n- Check 1: Schema Registry & Dynamic Registration -")
schemas = list_schemas()
check("legal_intake schema registered",   "legal_intake" in schemas)
check("billing schema registered",        "billing" in schemas)
check("scheduling schema registered",     "scheduling" in schemas)
check("technical_support schema registered", "technical_support" in schemas)

# Dynamic registration
custom_schema = {
    "title": "Test Custom",
    "required": ["order_id"],
    "properties": {
        "order_id": {
            "type": "string",
            "x-patterns": [r"\bORD-(\d{4,8})\b"],
            "x-keywords": ["order", "order number"],
        },
        "quantity": {"type": "integer", "x-keywords": ["quantity", "items", "units"]},
    },
}
register_schema("test_custom", custom_schema)
check("custom schema dynamically registered", "test_custom" in list_schemas())
check("get_schema returns correct schema",     get_schema("legal_intake") is not None)

# ─── Check 2: Typed Field Extraction ──────────────────────────────────────────
print("\n- Check 2: Typed Field Extraction -")
LEGAL_TURNS = [
    {"role": "caller", "text": "Hi, my name is Robert Chen. I had a car accident on March 15th."},
    {"role": "agent",  "text": "I am sorry to hear that. Can I get your contact details?"},
    {"role": "caller", "text": "Sure. My number is 555-867-5309. Email is r.chen@example.com."},
    {"role": "caller", "text": "It is a personal injury matter. I need this urgently please."},
]
legal_result = schema_extractor.extract("legal_intake", "chk2-001", LEGAL_TURNS)
fields_by_name = {f.field_name: f for f in legal_result.fields}

check("client_name extracted (string)",       fields_by_name.get("client_name", {}).value is not None,
      f"got {fields_by_name.get('client_name', {}).value!r}")
check("contact_phone extracted (phone)",      fields_by_name.get("contact_phone", {}).value is not None,
      f"got {fields_by_name.get('contact_phone', {}).value!r}")
check("contact_email extracted (email)",      fields_by_name.get("contact_email", {}).value is not None,
      f"got {fields_by_name.get('contact_email', {}).value!r}")
check("injury_date extracted (date)",         fields_by_name.get("injury_date", {}).value is not None,
      f"got {fields_by_name.get('injury_date', {}).value!r}")
check("case_type extracted (enum)",           fields_by_name.get("case_type", {}).value == "personal_injury",
      f"got {fields_by_name.get('case_type', {}).value!r}")
check("needs_urgent_consultation (boolean)",  fields_by_name.get("needs_urgent_consultation") is not None)
check("extraction_coverage >= 80%",           legal_result.extraction_coverage >= 0.8,
      f"got {legal_result.extraction_coverage:.1%}")

# ─── Check 3: Confidence Scoring ──────────────────────────────────────────────
print("\n- Check 3: Confidence Scoring (pattern > proximity > default > missing) -")
BILLING_TURNS = [
    {"role": "caller", "text": "My account number ACC-8821 has a $149.99 overcharge. I want a refund."},
    {"role": "caller", "text": "Please call me back at 555-234-5678."},
]
billing_result = schema_extractor.extract("billing", "chk3-001", BILLING_TURNS)
b_fields = {f.field_name: f for f in billing_result.fields}

check("pattern source has conf >= 0.90",      b_fields.get("account_id", {}).confidence >= 0.90,
      f"got conf={b_fields.get('account_id', {}).confidence}")
check("account_id extracted correctly",       b_fields.get("account_id", {}).value == "ACC-8821",
      f"got {b_fields.get('account_id', {}).value!r}")
check("amount extracted as float",            isinstance(b_fields.get("amount", {}).value, float),
      f"got {b_fields.get('amount', {}).value!r} type={type(b_fields.get('amount', {}).value)}")
check("amount value is 149.99",               b_fields.get("amount", {}).value == 149.99,
      f"got {b_fields.get('amount', {}).value}")
check("issue_type=refund (enum proximity)",   b_fields.get("issue_type", {}).value == "refund",
      f"got {b_fields.get('issue_type', {}).value!r}")
check("missing fields have conf=0.0",         b_fields.get("invoice_number", {}).confidence == 0.0)

# ─── Check 4: Multi-Schema & CRM Payload ──────────────────────────────────────
print("\n- Check 4: Multi-Schema Extraction & CRM Payload -")
SCHED_TURNS = [
    {"role": "caller", "text": "I need to book a consultation appointment for March 20th."},
    {"role": "caller", "text": "My name is Sarah Kim. Please call me at 555-999-1234 at 2pm."},
    {"role": "caller", "text": "Can you send me a reminder? My invoice balance also needs checking."},
]
multi_result = schema_extractor.extract_multi(
    schema_ids=["scheduling", "billing"],
    call_id="chk4-001",
    transcript_turns=SCHED_TURNS,
)
check("multi-schema returns dict keyed by schema_id", set(multi_result.keys()) == {"scheduling", "billing"})
check("scheduling result is ExtractionResult",         isinstance(multi_result["scheduling"], ExtractionResult))
check("scheduling date extracted",
      any(f.field_name == "appointment_date" and f.value for f in multi_result["scheduling"].fields))

sched_crm = multi_result["scheduling"].to_crm_payload()
check("CRM payload is flat dict",                      isinstance(sched_crm, dict))
check("CRM payload has at least 1 extracted field",    len(sched_crm) > 0)
check("to_dict() serializable",                        isinstance(multi_result["scheduling"].to_dict(), dict))
check("to_dict() fields list present",                 isinstance(multi_result["scheduling"].to_dict().get("fields"), list))

# ─── Check 5: Custom Schema ───────────────────────────────────────────────────
print("\n- Check 5: Custom Schema Registration & Extraction -")
ORDER_TURNS = [
    {"role": "caller", "text": "I need help with order ORD-48291. I ordered 3 items."},
]
custom_result = schema_extractor.extract("test_custom", "chk5-001", ORDER_TURNS)
c_fields = {f.field_name: f for f in custom_result.fields}
check("custom schema order_id extracted",     c_fields.get("order_id", {}).value == "48291",
      f"got {c_fields.get('order_id', {}).value!r}")
check("custom schema quantity extracted",     c_fields.get("quantity", {}).value is not None,
      f"got {c_fields.get('quantity', {}).value!r}")
check("missing required = [] when covered",   len(custom_result.missing_required) == 0)

# ─── Check 6: Pipeline Worker Integration (6-Stage) ───────────────────────────
print("\n- Check 6: Pipeline Worker Integration (6-Stage Execution) -")
worker = PostCallPipelineWorker(concurrency=1)
call_id = f"verify-extraction-{int(time.time())}"

job = worker.enqueue_call(
    call_id=call_id,
    room_name=f"room-{call_id}",
    transcript_turns=BILLING_TURNS,
    metadata={"caller_name": "Test Customer"},
    priority=1,
)
completed_job = asyncio.run(worker.execute_job(job.job_id))

check("job status=completed",              completed_job.status == "completed",
      f"got {completed_job.status}")

expected_stages = [s.value for s in PipelineStage]
check("all 6 pipeline stages present",     set(expected_stages) == set(completed_job.stages.keys()),
      f"got {set(completed_job.stages.keys())}")

all_done = all(completed_job.stages[s]["status"] == StageStatus.COMPLETED.value for s in expected_stages)
check("all 6 stages completed",            all_done,
      str({s: completed_job.stages[s]["status"] for s in expected_stages}))

schema_stage = completed_job.stages.get(PipelineStage.SCHEMA_EXTRACTION.value, {})
check("schema_extraction stage completed",
      schema_stage.get("status") == StageStatus.COMPLETED.value,
      f"got {schema_stage.get('status')}")
check("extractions in job.metadata",       "extractions" in completed_job.metadata)
check("crm_payloads in job.metadata",      "crm_payloads" in completed_job.metadata)
check("crm_payloads is dict",              isinstance(completed_job.metadata.get("crm_payloads"), dict))

# Verify manifest
import os as _os
ARCHIVE_DIR = _os.path.join(ROOT, "recordings", "archive")
manifest_path = _os.path.join(ARCHIVE_DIR, f"{call_id}_manifest.json")
manifest_ok = extractions_in_manifest = crm_in_manifest = False
if _os.path.isfile(manifest_path):
    with open(manifest_path) as f:
        manifest = json.load(f)
    manifest_ok = True
    extractions_in_manifest = "extractions" in manifest and manifest["extractions"] is not None
    crm_in_manifest = "crm_payloads" in manifest and manifest["crm_payloads"] is not None
check("manifest written to disk",          manifest_ok)
check("extractions in manifest",           extractions_in_manifest)
check("crm_payloads in manifest",          crm_in_manifest)

# ─── Check 7: REST API Endpoints ──────────────────────────────────────────────
print("\n- Check 7: REST API Endpoints -")
try:
    # GET /api/extraction/schemas
    with urllib.request.urlopen(f"{BASE_URL}/api/extraction/schemas", timeout=5) as resp:
        schemas_resp = json.loads(resp.read())
    check("GET /api/extraction/schemas status=ok",  schemas_resp.get("status") == "ok")
    check("schemas list includes legal_intake",     "legal_intake" in schemas_resp.get("schemas", []))

    # GET /api/extraction/schemas?id=billing
    with urllib.request.urlopen(f"{BASE_URL}/api/extraction/schemas?id=billing", timeout=5) as resp:
        schema_resp = json.loads(resp.read())
    check("GET /api/extraction/schemas?id=billing", schema_resp.get("status") == "ok")

    # POST /api/extraction/extract
    extract_payload = json.dumps({
        "schema_id": "legal_intake",
        "call_id": "api-extract-001",
        "transcript_turns": LEGAL_TURNS,
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/extraction/extract",
        data=extract_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        extract_resp = json.loads(resp.read())
    check("POST /api/extraction/extract status=ok",  extract_resp.get("status") == "ok")
    check("extraction result returned",              "extraction" in extract_resp)
    check("crm_payload returned",                   "crm_payload" in extract_resp)
    check("crm_payload is dict",                    isinstance(extract_resp.get("crm_payload"), dict))

    # POST /api/extraction/register
    register_payload = json.dumps({
        "schema_id": "api_test_schema",
        "schema": {
            "title": "API Test",
            "required": ["test_field"],
            "properties": {"test_field": {"type": "string", "x-keywords": ["test"]}},
        },
    }).encode()
    req2 = urllib.request.Request(
        f"{BASE_URL}/api/extraction/register",
        data=register_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req2, timeout=5) as resp2:
        register_resp = json.loads(resp2.read())
    check("POST /api/extraction/register status=ok", register_resp.get("status") == "ok")
    check("registered schema appears in list",       "api_test_schema" in register_resp.get("all_schemas", []))

    # GET /api/pipeline/extraction for completed job
    enqueue_payload = json.dumps({
        "call_id": f"api-extraction-test-{int(time.time())}",
        "transcript_turns": BILLING_TURNS,
        "execute_now": True,
    }).encode()
    enqueue_req = urllib.request.Request(
        f"{BASE_URL}/api/pipeline/enqueue",
        data=enqueue_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(enqueue_req, timeout=15) as resp:
        enqueue_resp = json.loads(resp.read())
    server_job_id = enqueue_resp.get("job", {}).get("job_id", "")
    with urllib.request.urlopen(
        f"{BASE_URL}/api/pipeline/extraction?job_id={server_job_id}", timeout=5
    ) as resp:
        extr_get_resp = json.loads(resp.read())
    check("GET /api/pipeline/extraction status=ok",  extr_get_resp.get("status") == "ok")
    check("extractions in pipeline/extraction GET",  extr_get_resp.get("extractions") is not None)
    check("crm_payloads in pipeline/extraction GET", extr_get_resp.get("crm_payloads") is not None)

except Exception as e:
    print(f"  {FAIL} REST API check skipped -- server error: {e}")
    failures.append("REST API not reachable")

# ─── Summary ──────────────────────────────────────────────────────────────────
total = 41
passed = total - len(failures)
print(f"\n{'='*55}")
print(f"  Task 3.3 Extraction: {passed}/{total} checks passed")
if failures:
    print(f"  FAILED: {', '.join(failures)}")
print(f"{'='*55}\n")
sys.exit(0 if not failures else 1)
