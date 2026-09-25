#!/usr/bin/env python3
"""Interactive test script: Simulate a voice call and push it to Clio in real time."""

import argparse
import asyncio
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from agent.pipeline_worker import pipeline_worker


async def simulate_call(name: str, phone: str, case_type: str, story: str):
    print("\n" + "=" * 60)
    print(" 🎙️  VOICE AGENT CALL SIMULATION")
    print("=" * 60)
    print(f"Caller Name : {name}")
    print(f"Caller Phone: {phone}")
    print(f"Case Type   : {case_type}")
    print(f"Story       : {story}")
    print("-" * 60)
    print("▶ Call connected... Caller is speaking with AI Agent...")

    sample_turns = [
        {"role": "caller", "text": f"Hello, my name is {name}. I need help with my legal case."},
        {"role": "agent", "text": f"Hello {name}, I can certainly assist you. What occurred?"},
        {"role": "caller", "text": f"{story}. You can reach me at {phone}."},
        {"role": "agent", "text": f"Thank you {name}. We are opening an active {case_type} intake matter for you now."},
    ]

    print("⏹ Call ended! Post-call processing pipeline triggered...")
    job = pipeline_worker.enqueue_call(
        call_id=f"test-{int(asyncio.get_event_loop().time())}",
        transcript_turns=sample_turns,
        metadata={"summary": {"topics": ["legal_intake"]}},
    )

    print("⚙️  Extracting legal facts & contacting Clio API...")
    result = await pipeline_worker.execute_job(job.job_id)

    sync = result.metadata.get("clio_manage_sync", {})
    contact = sync.get("contact", {}).get("contact", {})
    matter = sync.get("matter", {}).get("matter", {})

    print("\n" + "=" * 60)
    print(" 🎉  LIVE CLIO SYNC RESULT")
    print("=" * 60)
    if contact and matter:
        print(f"✅ Contact Created in Clio: {contact.get('name')} (ID: {contact.get('id')})")
        print(f"✅ Matter Created in Clio : {matter.get('display_number')} (ID: {matter.get('id')})")
        print("\n👉 OPEN YOUR BROWSER: Go to https://app.clio.com/ to see it live!")
    else:
        print("⚠️ Sync did not return contact/matter IDs. Check .env CLIO_MANAGE_ACCESS_TOKEN.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate a call and push to Clio")
    parser.add_argument("--name", default="Tony Stark", help="Caller full name")
    parser.add_argument("--phone", default="+1-555-019-4455", help="Caller phone")
    parser.add_argument("--case", default="corporate contract", help="Case category")
    parser.add_argument("--story", default="Contractual breach of technology licensing agreement", help="Brief story")
    args = parser.parse_args()

    asyncio.run(simulate_call(args.name, args.phone, args.case, args.story))
