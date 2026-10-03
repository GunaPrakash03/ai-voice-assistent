#!/usr/bin/env python3
"""scripts/backfill_cases.py — register cases for calls processed before case tracking existed.

Reads the saved post-call jobs (the ``call_jobs`` collection in PostgreSQL, or
recordings/pipeline_jobs.json without a database) and runs each completed job through
``case_manager.register_from_call``, the same rule new calls use. Calls that already have a case
are skipped, and calls without a caller name or phone are not registered.

Dry run by default: prints what it would register. Add --apply to write the cases.
  python3 scripts/backfill_cases.py
  python3 scripts/backfill_cases.py --apply
"""

import datetime
import json
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from agent import storage  # noqa: E402  (provider_manager-free: reads DATABASE_URL from the environment)
from agent.case_manager import case_manager  # noqa: E402

JOBS_FILE = os.path.join(ROOT_DIR, "recordings", "pipeline_jobs.json")


def load_jobs():
    if storage.database_url() and storage.available():
        return storage.load_collection("call_jobs"), "PostgreSQL call_jobs"
    if os.path.isfile(JOBS_FILE):
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("jobs") or [], JOBS_FILE
    return [], "nothing"


class _DryRun:
    """Stands in for case_manager.create so a dry run decides exactly like --apply, writing nothing."""
    def __init__(self):
        self.would = []

    def __call__(self, workspace_id, data, created_by="", source="manual"):
        self.would.append((workspace_id, data))
        return type("Planned", (), {"case_id": "(dry run)", "workspace_id": workspace_id})()


def main():
    apply = "--apply" in sys.argv
    jobs, source = load_jobs()
    print(f"Read {len(jobs)} jobs from {source}")
    dry = None
    if not apply:
        dry = _DryRun()
        case_manager.create = dry      # instance attribute: only this process, only this run

    made = skipped_existing = skipped_empty = 0
    for job in sorted(jobs, key=lambda j: j.get("created_at") or 0):
        call_id = job.get("call_id") or ""
        if job.get("status") != "completed" or not call_id:
            continue
        if case_manager.find_by_call(call_id):
            skipped_existing += 1
            continue
        md = dict(job.get("metadata") or {})
        md.setdefault("ended_at", job.get("completed_at") or job.get("created_at"))
        case = case_manager.register_from_call(call_id, md)
        if not case:
            skipped_empty += 1
            continue
        made += 1
        if dry:
            ws, data = dry.would[-1]
            when = datetime.datetime.fromtimestamp(data["registered_at"]).strftime("%Y-%m-%d %H:%M") if data.get("registered_at") else "?"
            print(f"  would register  {when}  {ws:<14} {data['client_name'][:30]:<30} {str(data.get('case_type') or '')[:24]:<24} call {call_id}")
        else:
            print(f"  registered {case.case_id}  {case.workspace_id}  {case.client_name}  call {call_id}")

    verb = "Registered" if apply else "Would register"
    print(f"\n{verb} {made} cases; {skipped_existing} calls already had one; {skipped_empty} calls had no caller name or phone.")
    if not apply and made:
        print("Run again with --apply to write them.")


if __name__ == "__main__":
    main()
