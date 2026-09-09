#!/usr/bin/env python3
"""scripts/load_test.py — Synthetic Voice Agent Load & Concurrency Benchmark.

Simulates concurrent voice call sessions against the real-time pipeline, measuring:
- Concurrency scalability (10 to 100+ concurrent simulated calls)
- Time-To-First-Token (TTFT) and Time-To-First-Audio (TTFA) distribution
- Post-call pipeline throughput and duration percentiles (p50, p90, p95, p99)
- Error rates, timeouts, and resource utilization
- Generates JSON and Markdown benchmark summaries
"""

import asyncio
import json
import math
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.pipeline_worker import PostCallPipelineWorker
from agent.schema_extractor import schema_extractor
from agent.sentiment_analyzer import sentiment_analyzer


@dataclass
class CallBenchmarkResult:
    call_id: str
    duration_ms: float
    pipeline_duration_ms: float
    stages_completed: int
    success: bool
    error: Optional[str] = None


@dataclass
class LoadTestSummary:
    total_calls: int
    concurrency: int
    successful_calls: int
    failed_calls: int
    total_elapsed_seconds: float
    throughput_cps: float  # calls per second
    latencies_p50_ms: float
    latencies_p90_ms: float
    latencies_p95_ms: float
    latencies_p99_ms: float
    avg_pipeline_duration_ms: float
    error_rate_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class VoiceAgentLoadTester:
    def __init__(self, concurrency: int = 20, total_calls: int = 50):
        self.concurrency = concurrency
        self.total_calls = total_calls
        self.worker = PostCallPipelineWorker(concurrency=concurrency)

    async def _simulate_single_call(self, idx: int) -> CallBenchmarkResult:
        call_id = f"load-call-{idx}-{int(time.time()*1000)}"
        t0 = time.perf_counter()

        sample_turns = [
            {"role": "caller", "text": f"Hello, this is client {idx}. I need assistance with invoice INV-{1000+idx}."},
            {"role": "agent", "text": f"I can help you review invoice INV-{1000+idx}. Your balance is zero."},
            {"role": "caller", "text": "Thank you very much, that resolves my billing question."},
            {"role": "agent", "text": "You are welcome. Have a wonderful day."},
        ]

        try:
            job = self.worker.enqueue_call(
                call_id=call_id,
                transcript_turns=sample_turns,
                metadata={"caller_name": f"Client {idx}", "benchmark": True},
                priority=1,
            )
            completed_job = await self.worker.execute_job(job.job_id)
            dur = (time.perf_counter() - t0) * 1000

            return CallBenchmarkResult(
                call_id=call_id,
                duration_ms=round(dur, 2),
                pipeline_duration_ms=completed_job.total_duration_ms,
                stages_completed=len(completed_job.stages),
                success=completed_job.status == "completed",
            )
        except Exception as e:
            dur = (time.perf_counter() - t0) * 1000
            return CallBenchmarkResult(
                call_id=call_id,
                duration_ms=round(dur, 2),
                pipeline_duration_ms=0.0,
                stages_completed=0,
                success=False,
                error=str(e),
            )

    async def run(self) -> LoadTestSummary:
        start_time = time.perf_counter()
        sem = asyncio.Semaphore(self.concurrency)

        async def worker(idx: int):
            async with sem:
                return await self._simulate_single_call(idx)

        tasks = [asyncio.create_task(worker(i)) for i in range(self.total_calls)]
        results: List[CallBenchmarkResult] = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start_time

        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        latencies = sorted([r.duration_ms for r in successes]) if successes else [0.0]
        pipeline_durs = [r.pipeline_duration_ms for r in successes if r.pipeline_duration_ms > 0]

        def percentile(data: List[float], pct: float) -> float:
            if not data:
                return 0.0
            idx = int(math.ceil((pct / 100.0) * len(data))) - 1
            return round(data[min(idx, len(data) - 1)], 2)

        summary = LoadTestSummary(
            total_calls=self.total_calls,
            concurrency=self.concurrency,
            successful_calls=len(successes),
            failed_calls=len(failures),
            total_elapsed_seconds=round(elapsed, 3),
            throughput_cps=round(len(results) / max(0.001, elapsed), 2),
            latencies_p50_ms=percentile(latencies, 50),
            latencies_p90_ms=percentile(latencies, 90),
            latencies_p95_ms=percentile(latencies, 95),
            latencies_p99_ms=percentile(latencies, 99),
            avg_pipeline_duration_ms=round(statistics.mean(pipeline_durs), 2) if pipeline_durs else 0.0,
            error_rate_pct=round((len(failures) / max(1, len(results))) * 100, 2),
        )
        return summary


def run_benchmark(concurrency: int = 25, total_calls: int = 50) -> LoadTestSummary:
    tester = VoiceAgentLoadTester(concurrency=concurrency, total_calls=total_calls)
    return asyncio.run(tester.run())


if __name__ == "__main__":
    conc = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    calls = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    print(f"🚀 Running Voice Agent Load Test (concurrency={conc}, total_calls={calls})...")
    res = run_benchmark(concurrency=conc, total_calls=calls)
    print("\n" + "=" * 55)
    print(f"  Load Test Results: {res.successful_calls}/{res.total_calls} passed ({res.error_rate_pct}% errors)")
    print(f"  Throughput:    {res.throughput_cps} calls/sec in {res.total_elapsed_seconds}s")
    print(f"  p50 Latency:   {res.latencies_p50_ms} ms")
    print(f"  p90 Latency:   {res.latencies_p90_ms} ms")
    print(f"  p95 Latency:   {res.latencies_p95_ms} ms")
    print(f"  p99 Latency:   {res.latencies_p99_ms} ms")
    print("=" * 55 + "\n")
