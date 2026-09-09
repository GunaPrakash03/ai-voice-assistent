#!/usr/bin/env python3
"""scripts/verify_hardening.py — Task 4.4 Acceptance Tests.

Verifies:
  Check 1: Synthetic Load Testing & Concurrency Scalability (percentile calculations, throughput)
  Check 2: Security & PII Redaction Audit Engine (SSN, credit card, secrets, compliance)
  Check 3: CI/CD Workflow & GitHub Actions Integrity (.github/workflows/ci.yml)
  Check 4: Production Hardening, Error Handling & Graceful Degradation
"""

import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.load_test import VoiceAgentLoadTester, run_benchmark
from scripts.security_audit import SecurityAuditor, run_security_audit

PASS = "\033[32m\u2714\033[0m"
FAIL = "\033[31m\u2718\033[0m"
failures = []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}{' -- ' + detail if detail else ''}")
        failures.append(name)


# ─── Check 1: Synthetic Load Testing & Scalability ──────────────────────────
print("\n- Check 1: Synthetic Load Testing & Scalability -")
load_res = run_benchmark(concurrency=10, total_calls=15)

check("total calls executed",             load_res.total_calls == 15)
check("concurrency set to 10",            load_res.concurrency == 10)
check("successful calls >= 14",           load_res.successful_calls >= 14)
check("throughput > 0 cps",               load_res.throughput_cps > 0)
check("p50 latency computed",             load_res.latencies_p50_ms > 0)
check("p90 latency >= p50",               load_res.latencies_p90_ms >= load_res.latencies_p50_ms)
check("p95 latency >= p90",               load_res.latencies_p95_ms >= load_res.latencies_p90_ms)
check("p99 latency >= p95",               load_res.latencies_p99_ms >= load_res.latencies_p95_ms)
check("error rate <= 10%",                load_res.error_rate_pct <= 10.0)
check("to_dict() serialization works",    isinstance(load_res.to_dict(), dict))


# ─── Check 2: Security & PII Redaction Audit Engine ─────────────────────────
print("\n- Check 2: Security & PII Redaction Audit Engine -")
auditor = SecurityAuditor(root_dir=ROOT)

# 1. PII detection
ssn_samples = auditor.check_pii_redaction_rules("Caller SSN is 123-45-6789 and needs update.")
check("SSN detected in unredacted text",   len(ssn_samples) > 0 and ssn_samples[0][0] == "SSN")

ccn_samples = auditor.check_pii_redaction_rules("Card number 4111-2222-3333-4444 provided over voice.")
check("CCN detected in unredacted text",   len(ccn_samples) > 0 and ccn_samples[0][0] == "CCN")

clean_samples = auditor.check_pii_redaction_rules("Patient called to reschedule appointment for Friday.")
check("clean text has 0 PII findings",    len(clean_samples) == 0)

# 2. Audit run
audit_report = run_security_audit()
check("security audit status ok/passed",  audit_report["status"] in ("passed", "ok"))
check("audit findings non-empty",         len(audit_report["findings"]) >= 3)
check("all audit findings passed",        all(f["passed"] for f in audit_report["findings"]))


# ─── Check 3: CI/CD Workflow & GitHub Actions Integrity ─────────────────────
print("\n- Check 3: CI/CD Workflow & GitHub Actions Integrity -")
ci_file = os.path.join(ROOT, ".github", "workflows", "ci.yml")
check("ci.yml file exists on disk",       os.path.isfile(ci_file))

with open(ci_file, "r", encoding="utf-8") as f:
    ci_content = f.read()

check("triggers on push to main",         "branches: [ main ]" in ci_content or "branches: [main]" in ci_content)
check("runs full regression test suite",  "scripts/run_all_tests.py" in ci_content)
check("runs security audit step",         "scripts/security_audit.py" in ci_content)
check("runs concurrency load test",       "scripts/load_test.py" in ci_content)
check("uses redis service container",     "image: redis:" in ci_content)


# ─── Check 4: Production Hardening & Error Handling ─────────────────────────
print("\n- Check 4: Production Hardening & Graceful Degradation -")
from agent.auth_manager import auth_manager

# Rate limiter recovery
rl = auth_manager.rate_limiter
rl.reset()
res = rl.check("hardening-test-key", limit=2)
check("rate limiter allows initial req",  res.allowed is True)
rl.check("hardening-test-key", limit=2)
res_block = rl.check("hardening-test-key", limit=2)
check("rate limiter enforces ceiling",    res_block.allowed is False)
rl.reset("hardening-test-key")
res_reset = rl.check("hardening-test-key", limit=2)
check("rate limiter resets cleanly",      res_reset.allowed is True)


# ─── Summary ────────────────────────────────────────────────────────────────
passed = 22 - len(failures)
print(f"\n{'='*55}")
print(f"  Task 4.4 Hardening & Testing: {passed}/22 checks passed")
if failures:
    print(f"  FAILED: {', '.join(failures)}")
print(f"{'='*55}\n")
sys.exit(0 if not failures else 1)
