#!/usr/bin/env python3
"""scripts/verify_analytics.py — Task 3.2 Acceptance Tests.

6 checks:
  Check 1: Multi-dimensional sentiment scoring (-1.0 to 1.0, polarity)
  Check 2: Frustration & escalation detection with trigger keywords
  Check 3: Sentiment trajectory tracking (start->mid->end, trend)
  Check 4: Executive narrative summary, key intent & action items
  Check 5: Pipeline worker integration (5-stage execution & manifest)
  Check 6: Analytics REST API endpoints
"""
import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.sentiment_analyzer import sentiment_analyzer, SentimentPolarity, ResolutionStatus
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


# --- Check 1: Multi-Dimensional Sentiment Scoring ---
print("\n- Check 1: Multi-Dimensional Sentiment Scoring -")
POSITIVE_TURNS = [
    {"role": "caller", "text": "Thank you so much, that was excellent and very helpful!"},
    {"role": "agent",  "text": "You are welcome, happy to assist you today!"},
    {"role": "caller", "text": "Perfect, I really appreciate your prompt response."},
]
result = sentiment_analyzer.analyze_call(POSITIVE_TURNS)
check("overall_polarity populated",     result.overall_polarity in [e.value for e in SentimentPolarity])
check("overall_score in [-1.0, 1.0]",  -1.0 <= result.overall_score <= 1.0)
check("positive conversation scored >0",result.overall_score > 0.0,
      f"got {result.overall_score}")
check("turn_sentiments list populated", len(result.turn_sentiments) == len(POSITIVE_TURNS))
check("per-turn score field present",   all("score" in t for t in result.turn_sentiments))
check("positive_percentage > 0",        result.positive_percentage > 0.0)

# --- Check 2: Frustration & Escalation Detection ---
print("\n- Check 2: Frustration & Escalation Detection -")
FRUSTRATED_TURNS = [
    {"role": "caller", "text": "This is unacceptable, I need to speak to a manager immediately!"},
    {"role": "agent",  "text": "I understand your concern, let me help you."},
    {"role": "caller", "text": "This is a waste of time, I already told you this is broken!"},
]
frust_result = sentiment_analyzer.analyze_call(FRUSTRATED_TURNS)
check("frustration_detected=True",      frust_result.frustration_detected is True,
      f"got {frust_result.frustration_detected}")
check("frustration_score > 0",          frust_result.frustration_score > 0.0,
      f"got {frust_result.frustration_score}")
check("frustration_reasons list non-empty", len(frust_result.frustration_reasons) > 0)
check("negative overall_polarity",      frust_result.overall_polarity == SentimentPolarity.NEGATIVE.value,
      f"got {frust_result.overall_polarity}")
check("caller_score < 0",               frust_result.caller_score < 0.0,
      f"got {frust_result.caller_score}")

# --- Check 3: Sentiment Trajectory Tracking ---
print("\n- Check 3: Sentiment Trajectory Tracking -")
IMPROVING_TURNS = [
    {"role": "caller", "text": "This is terrible, I am very upset with this issue!"},
    {"role": "agent",  "text": "I am sorry to hear that, let me resolve this immediately."},
    {"role": "caller", "text": "Oh great, that fixed it! Thank you, wonderful help!"},
]
traj_result = sentiment_analyzer.analyze_call(IMPROVING_TURNS)
check("trajectory list length >= 2",    len(traj_result.sentiment_trajectory) >= 2,
      f"got {traj_result.sentiment_trajectory}")
check("trajectory_trend populated",     traj_result.trajectory_trend in ("improving", "declining", "stable"),
      f"got {traj_result.trajectory_trend}")
check("improving trajectory detected",  traj_result.trajectory_trend == "improving",
      f"got {traj_result.trajectory_trend} -- start={traj_result.turn_sentiments[0]['score']:.3f}, "
      f"end={traj_result.turn_sentiments[-1]['score']:.3f}")

# --- Check 4: Executive Narrative Summary ---
print("\n- Check 4: Executive Narrative Summary & Intent Extraction -")
BILLING_TURNS = [
    {"role": "caller", "text": "Hi, I have a question about my invoice and retainer payment."},
    {"role": "agent",  "text": "Sure, let me look up your billing details."},
    {"role": "caller", "text": "There seems to be a charge I don't recognize."},
    {"role": "agent",  "text": "I have confirmed your account and cleared the issue -- you're all set."},
]
summary = sentiment_analyzer.generate_summary(
    call_id="test-billing-001",
    transcript_turns=BILLING_TURNS,
    metadata={"caller_name": "Jane Doe"},
)
check("executive_summary non-empty",    len(summary.executive_summary) > 20)
check("key_intent populated",           len(summary.key_intent) > 5,
      f"got '{summary.key_intent}'")
check("billing topic detected",         "billing" in summary.topics,
      f"got topics={summary.topics}")
check("action_items list non-empty",    len(summary.action_items) > 0)
check("resolution_status populated",    summary.resolution_status in [e.value for e in ResolutionStatus],
      f"got '{summary.resolution_status}'")
check("caller_name in executive summary",
      "Jane Doe" in summary.executive_summary or "Customer" in summary.executive_summary)

# --- Check 5: Pipeline Worker Integration (5-Stage) ---
print("\n- Check 5: Pipeline Worker Integration (5-Stage Execution) -")
worker = PostCallPipelineWorker(concurrency=1)
call_id = f"verify-analytics-{int(time.time())}"

job = worker.enqueue_call(
    call_id=call_id,
    room_name=f"room-{call_id}",
    transcript_turns=BILLING_TURNS,
    metadata={"caller_name": "Test Customer"},
    priority=1,
)
completed_job = asyncio.run(worker.execute_job(job.job_id))

check("job status=completed",           completed_job.status == "completed",
      f"got {completed_job.status}")

expected_stages = [s.value for s in PipelineStage]
check("all 5 pipeline stages present",  set(expected_stages) == set(completed_job.stages.keys()),
      f"missing: {set(expected_stages) - set(completed_job.stages.keys())}")

all_done = all(
    completed_job.stages[s]["status"] == StageStatus.COMPLETED.value
    for s in expected_stages
)
check("all 5 stages completed",         all_done,
      str({s: completed_job.stages[s]["status"] for s in expected_stages}))

sentiment_stage = completed_job.stages.get(PipelineStage.SENTIMENT_ANALYSIS.value, {})
check("sentiment_analysis stage completed",
      sentiment_stage.get("status") == StageStatus.COMPLETED.value,
      f"got {sentiment_stage.get('status')}")
check("sentiment stored in job.metadata",  "sentiment" in completed_job.metadata)
check("summary stored in job.metadata",    "summary" in completed_job.metadata)

# Verify manifest file contains sentiment/summary
ARCHIVE_DIR = os.path.join(ROOT, "recordings", "archive")
manifest_path = os.path.join(ARCHIVE_DIR, f"{call_id}_manifest.json")
manifest_ok = False
sentiment_in_manifest = False
summary_in_manifest = False
if os.path.isfile(manifest_path):
    with open(manifest_path) as f:
        manifest = json.load(f)
    manifest_ok = True
    sentiment_in_manifest = "sentiment" in manifest and manifest["sentiment"] is not None
    summary_in_manifest = "summary" in manifest and manifest["summary"] is not None

check("manifest JSON written to disk",  manifest_ok, f"path={manifest_path}")
check("sentiment in manifest",          sentiment_in_manifest)
check("summary in manifest",            summary_in_manifest)

# --- Check 6: Analytics REST API ---
print("\n- Check 6: Analytics REST API Endpoints -")
try:
    # Test POST /api/pipeline/analyze
    payload = json.dumps({
        "call_id": "api-test-001",
        "transcript_turns": BILLING_TURNS,
        "metadata": {"caller_name": "API Tester"},
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/pipeline/analyze",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        analyze_resp = json.loads(resp.read())
    check("POST /api/pipeline/analyze status=ok",    analyze_resp.get("status") == "ok")
    check("analytics.sentiment returned",            "sentiment" in analyze_resp.get("analytics", {}))
    check("analytics.summary returned",              "summary" in analyze_resp.get("analytics", {}))

    # Test GET /api/pipeline/stats (regression check)
    with urllib.request.urlopen(f"{BASE_URL}/api/pipeline/stats", timeout=5) as resp:
        stats_resp = json.loads(resp.read())
    check("GET /api/pipeline/stats status=ok",       stats_resp.get("status") == "ok")

    # Enqueue a job via server to test GET /api/pipeline/analytics
    enqueue_payload = json.dumps({
        "call_id": f"analytics-get-test-{int(time.time())}",
        "transcript_turns": BILLING_TURNS,
        "execute_now": True,
    }).encode()
    enqueue_req = urllib.request.Request(
        f"{BASE_URL}/api/pipeline/enqueue",
        data=enqueue_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(enqueue_req, timeout=10) as resp:
        enqueue_resp = json.loads(resp.read())
    server_job_id = enqueue_resp.get("job", {}).get("job_id", "")

    # Test GET /api/pipeline/analytics for server-enqueued job
    with urllib.request.urlopen(
        f"{BASE_URL}/api/pipeline/analytics?job_id={server_job_id}", timeout=5
    ) as resp:
        analytics_resp = json.loads(resp.read())
    check("GET /api/pipeline/analytics by job_id",   analytics_resp.get("status") == "ok",
          f"got {analytics_resp}")
    check("sentiment returned in analytics GET",      analytics_resp.get("sentiment") is not None)
    check("summary returned in analytics GET",        analytics_resp.get("summary") is not None)

except Exception as e:
    print(f"  {FAIL} REST API check skipped -- server not reachable: {e}")
    failures.append("REST API not reachable")

# --- Summary ---
total = 29
passed = total - len(failures)
print(f"\n{'='*55}")
print(f"  Task 3.2 Analytics: {passed}/{total} checks passed")
if failures:
    print(f"  FAILED: {', '.join(failures)}")
print(f"{'='*55}\n")
sys.exit(0 if not failures else 1)
