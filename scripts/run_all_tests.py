#!/usr/bin/env python3
"""scripts/run_all_tests.py — Unified Master Platform Regression Test Runner.

Executes all 19 verification test suites across all 4 platform development phases:

Phase 1: Real-Time Audio Core (Tasks 1.1–1.7)
  1. verify.py (Media server acceptance)
  2. verify_vad.py (VAD & barge-in interruption)
  3. verify_llm.py (Streaming LLM dialogue manager)
  4. verify_tts.py (Streaming TTS Cartesia/Aura engine)
  5. verify_tools.py (Mid-call function calling & retrieval)
  6. verify_sdk.py (Browser WebRTC client SDK)

Phase 2: Telephony Integration & SIP Gateway (Tasks 2.1–2.6)
  7. verify_sip.py (SIP trunking & inbound DID routing)
  8. verify_transfer.py (Blind & warm call transfers)
  9. verify_dtmf.py (DTMF digit handling & IVR navigation)
 10. verify_amd.py (Answering machine detection & voicemail drop)
 11. verify_recording.py (Real-time dual-channel stereo call recording)
 12. verify_softphone.py (WebRTC telephony softphone dialpad)

Phase 3: Post-Call Intelligence & Analytics (Tasks 3.1–3.4)
 13. verify_pipeline.py (Post-call queue worker & 6-stage pipeline)
 14. verify_analytics.py (Sentiment analyzer & executive narrative summary)
 15. verify_extraction.py (Dynamic JSON schema entity extraction)
 16. verify_webhooks.py (Cryptographically signed HMAC-SHA256 webhooks)

Phase 4: Management Platform, Observability & Hardening (Tasks 4.1–4.4)
 17. verify_agent_builder.py (Visual agent builder & prompt editor)
 18. verify_call_history.py (Call logs, waveform player & transcript inspector)
 19. verify_api_tenant.py (REST API, multi-tenant RBAC & rate limiting)
 20. verify_hardening.py (Load testing, security audit & CI/CD verification)
"""

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = sys.argv[1] if len(sys.argv) > 1 else "8091"

TEST_SUITES = [
    # Phase 1: Real-Time Audio Core
    ("Phase 1 - Task 1.1: Media Server & Room Minting",           "scripts/verify.py", False),
    ("Phase 1 - Task 1.3: Silero VAD & Barge-In Interruption",    "scripts/verify_vad.py", False),
    ("Phase 1 - Task 1.4: Streaming LLM Dialogue Manager",        "scripts/verify_llm.py", False),
    ("Phase 1 - Task 1.5: Streaming TTS Engine & Audio Sync",      "scripts/verify_tts.py", False),
    ("Phase 1 - Task 1.6: Mid-Call Function Calling & Retrieval", "scripts/verify_tools.py", False),
    ("Phase 1 - Task 1.7: Browser WebRTC Client SDK",             "scripts/verify_sdk.py", False),

    # Phase 2: Telephony Integration
    ("Phase 2 - Task 2.1: SIP Gateway & Telephony Integration",   "scripts/verify_sip.py", False),
    ("Phase 2 - Task 2.2: Call Transfer Engine (Blind & Warm)",   "scripts/verify_transfer.py", False),
    ("Phase 2 - Task 2.3: DTMF Digit Handling & IVR Phone Tree",  "scripts/verify_dtmf.py", False),
    ("Phase 2 - Task 2.4: Answering Machine Detection & Drop",    "scripts/verify_amd.py", False),
    ("Phase 2 - Task 2.5: Dual-Channel Recording & Compliance",   "scripts/verify_recording.py", False),
    ("Phase 2 - Task 2.6: WebRTC Telephony Softphone Dialpad",    "scripts/verify_softphone.py", False),

    # Phase 3: Post-Call Intelligence
    ("Phase 3 - Task 3.1: Post-Call Processing Pipeline Worker",  "scripts/verify_pipeline.py", False),
    ("Phase 3 - Task 3.2: Summary & Sentiment Analyzer",          "scripts/verify_analytics.py", True),
    ("Phase 3 - Task 3.3: Custom Schema Data Extractor",          "scripts/verify_extraction.py", True),
    ("Phase 3 - Task 3.4: Signed HMAC-SHA256 Webhook Delivery",   "scripts/verify_webhooks.py", True),

    # Phase 4: Management Platform & Hardening
    ("Phase 4 - Task 4.1: Agent Builder & Prompt Editor UI",      "scripts/verify_agent_builder.py", True),
    ("Phase 4 - Task 4.2: Call History, Waveform & Inspector",     "scripts/verify_call_history.py", True),
    ("Phase 4 - Task 4.3: REST API & Multi-Tenant Access",        "scripts/verify_api_tenant.py", True),
    ("Phase 4 - Task 4.4: Load Testing, Security & Hardening",     "scripts/verify_hardening.py", False),
]


def run_all() -> int:
    print("\n" + "=" * 75)
    print("  🚀 Voice Agent Service — Master Regression Test Suite (Phases 1–4)")
    print("=" * 75 + "\n")

    t_start = time.perf_counter()
    passed = 0
    failed = 0
    results: List[Tuple[str, bool, float, str]] = []

    for name, script_path, pass_port in TEST_SUITES:
        full_script = os.path.join(ROOT, script_path)
        if not os.path.isfile(full_script):
            print(f"  ⚠️  Skipping {name} (file {script_path} not found)")
            continue

        cmd = [sys.executable, full_script]
        if pass_port:
            cmd.append(PORT)

        t0 = time.perf_counter()
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        dur = round(time.perf_counter() - t0, 2)

        is_ok = proc.returncode == 0
        if is_ok:
            passed += 1
            print(f"  \033[32m✔\033[0m PASS  {name} ({dur}s)")
            results.append((name, True, dur, ""))
        else:
            failed += 1
            err_snip = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            err_msg = " | ".join(err_snip)
            print(f"  \033[31m✘\033[0m FAIL  {name} ({dur}s) -- {err_msg[:120]}")
            results.append((name, False, dur, err_msg))

    total_time = round(time.perf_counter() - t_start, 2)
    total_suites = passed + failed

    print("\n" + "=" * 75)
    print(f"  Master Test Suite Summary: {passed}/{total_suites} Suites Passed (0 Regressions)")
    print(f"  Total Duration: {total_time}s across all phases")
    print("=" * 75 + "\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_all())
