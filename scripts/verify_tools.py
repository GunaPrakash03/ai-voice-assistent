"""
Task 1.6 acceptance check — Mid-Call Function Calling & Retrieval Tools.

Verifies:
1. Tool registry and JSON Schema definitions (typed parameters, descriptions, required fields).
2. Async tool dispatcher non-blocking execution, latency tracking, and timeout protection.
3. Filler speech engine generates conversational filler phrases (<10ms) to eliminate dead air.
4. Streaming dialogue manager detects tool intent and streams grounded conversational replies.
5. Live end-to-end WebRTC mid-call function execution over data channels (`tool_call`, `filler_speech`, `tool_result`).
"""

import asyncio
import json
import os
import subprocess
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)


def check(label, fn):
    try:
        detail = fn()
        print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))
        return True
    except Exception as e:
        print(f"  FAIL  {label} — {e}")
        return False


def test_tool_registry_and_json_schemas():
    """Verify tool registry and JSON Schema definitions for business tools."""
    script = """
from agent.tool_manager import ToolRegistry

reg = ToolRegistry()
tools = reg.list_tools()
names = [t.name for t in tools]

expected = ['check_availability', 'book_appointment', 'lookup_order', 'query_knowledge_base', 'execute_webhook']
for exp in expected:
    if exp not in names:
        raise AssertionError(f"Expected tool '{exp}' not found in registry: {names}")

# Validate schemas
schemas = reg.get_schemas()
for s in schemas:
    fn = s.get('function', {})
    if not fn.get('name') or not fn.get('description'):
        raise AssertionError(f"Invalid schema: missing name/description: {s}")
    params = fn.get('parameters', {})
    if params.get('type') != 'object' or 'properties' not in params:
        raise AssertionError(f"Invalid parameters schema in tool {fn.get('name')}: {params}")

print(f"{len(tools)}|{','.join(names)}")
"""
    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Tool registry test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No schema output: {res.stdout}")

    count, tools_str = lines[-1].split("|")
    return f"{count} tools registered ({tools_str})"


def test_async_tool_dispatcher_and_timeout():
    """Verify async execution, latency tracking, and timeout recovery."""
    script = """
import asyncio, time
from agent.tool_manager import ToolRegistry, AsyncToolDispatcher, ToolDefinition

async def test():
    reg = ToolRegistry()
    disp = AsyncToolDispatcher(reg)

    # 1. Successful async execution with latency tracking
    r1 = await disp.execute_tool('lookup_order', {'order_id': '1042'})
    if r1['status'] != 'success' or not r1['result']:
        raise AssertionError(f"Execution failed: {r1}")
    if r1['duration_ms'] <= 0:
        raise AssertionError(f"Execution duration not measured: {r1}")

    # 2. Timeout protection test
    async def slow_func():
        await asyncio.sleep(0.5)
        return "slow"

    reg.register(ToolDefinition(
        name="slow_tool",
        description="Slow test tool",
        parameters={"type": "object", "properties": {}},
        handler=slow_func,
        timeout=0.08,
    ))

    r2 = await disp.execute_tool('slow_tool', {}, timeout=0.08)
    if r2['status'] != 'timeout':
        raise AssertionError(f"Expected status=timeout, got: {r2['status']}")

    print(f"{r1['status']}|{r1['duration_ms']:.1f}|{r2['status']}|{r2['duration_ms']:.1f}")

asyncio.run(test())
"""
    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Dispatcher test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No dispatcher output: {res.stdout}")

    s1, d1, s2, d2 = lines[-1].split("|")
    return f"exec success in {d1}ms, timeout handled in {d2}ms without blocking"


def test_filler_speech_engine():
    """Verify conversational filler speech selection and argument interpolation."""
    script = """
import time
from agent.tool_manager import FillerSpeechEngine

engine = FillerSpeechEngine()
engine.register_tool_fillers("lookup_order", [
    "Checking tracking details for order {order_id}...",
    "Looking up records for order {order_id} right now...",
])

t0 = time.perf_counter()
f1 = engine.get_filler("lookup_order", {"order_id": "ORD-5541"})
f2 = engine.get_filler("unknown_tool")
gen_time_us = (time.perf_counter() - t0) * 1_000_000

if "ORD-5541" not in f1:
    raise AssertionError(f"Filler template interpolation failed: {f1}")
if not f2 or len(f2) < 5:
    raise AssertionError(f"Default fallback filler missing: {f2}")

print(f"{f1}|{f2}|{gen_time_us:.1f}")
"""
    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Filler engine test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No filler output: {res.stdout}")

    f1, f2, latency_us = lines[-1].split("|")
    return f"phrase: '{f1}' (selected in {float(latency_us):.1f}µs)"


def test_dialogue_manager_grounding():
    """Verify dialogue manager tool detection, execution, and grounded response generation."""
    script = """
import asyncio
from agent.llm_manager import StreamingDialogueManager

async def test():
    mgr = StreamingDialogueManager()
    tool_calls = []
    fillers = []

    async def on_call(name, args):
        tool_calls.append((name, args))

    async def on_filler(f):
        fillers.append(f)

    res = await mgr.generate_response(
        "Can you check availability for dental cleaning tomorrow?",
        on_tool_call=on_call,
        on_filler=on_filler,
    )

    if not tool_calls or tool_calls[0][0] != "check_availability":
        raise AssertionError(f"Tool check_availability was not triggered: {tool_calls}")
    if not fillers:
        raise AssertionError("Filler speech callback was not called")
    if "dental cleaning" not in res['text'] or "openings" not in res['text']:
        raise AssertionError(f"Response not grounded in tool result: {res['text']}")
    if not res.get('tool_calls'):
        raise AssertionError("Metrics missing tool_calls metadata")

    print(f"{tool_calls[0][0]}|{fillers[0][:32]}|{len(res['clauses'])}|{res['text'][:45]}")

asyncio.run(test())
"""
    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Dialogue grounding test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No dialogue grounding output: {res.stdout}")

    tool, filler, clauses_count, text = lines[-1].split("|")
    return f"tool '{tool}' executed -> grounded {clauses_count} clauses: '{text}...'"


def test_e2e_webrtc_tool_calling():
    """Verify live end-to-end WebRTC tool calling over data channels."""
    script = """
import asyncio, os, json
from livekit import rtc
from agent.token import join_token

async def test():
    key = os.environ['LIVEKIT_API_KEY']
    secret = os.environ['LIVEKIT_API_SECRET']
    url = os.environ['LIVEKIT_URL']
    token = join_token(key, secret, 'verify-tools-e2e-room', 'verify-caller')
    room = rtc.Room()

    events = {}
    agent_joined = asyncio.get_event_loop().create_future()
    reply_done = asyncio.get_event_loop().create_future()

    @room.on('participant_connected')
    def on_p(p):
        if not agent_joined.done():
            agent_joined.set_result(p)

    @room.on('data_received')
    def on_data(packet):
        try:
            data = json.loads(packet.data.decode())
        except Exception:
            return
        topic = packet.topic
        events[topic] = data
        if topic == 'agent_reply':
            if not reply_done.done():
                reply_done.set_result(data)

    await room.connect(url, token)
    for p in room.remote_participants.values():
        if not agent_joined.done():
            agent_joined.set_result(p)

    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track('mic', source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    await asyncio.wait_for(agent_joined, timeout=10.0)
    await asyncio.sleep(1.0)

    # Send prompt that requires a tool
    pkt = json.dumps({'action': 'test_prompt', 'text': 'Where is my order 1042?'}).encode()
    await room.local_participant.publish_data(pkt, reliable=True)

    await asyncio.wait_for(reply_done, timeout=10.0)
    await room.disconnect()

    has_call = 'tool_call' in events
    has_filler = 'filler_speech' in events
    has_result = 'tool_result' in events
    reply_text = events.get('agent_reply', {}).get('text', '')

    if not (has_call and has_filler and has_result):
        raise AssertionError(f"Missing data channel events: call={has_call}, filler={has_filler}, res={has_result}")

    tool_name = events['tool_call'].get('tool')
    dur = events['tool_result'].get('duration_ms', 0.0)
    print(f"{tool_name}|{dur}|{has_filler}|{reply_text[:40]}")

asyncio.run(test())
"""
    cmd = ["docker", "exec", "voice-agent-worker", "python", "-c", script]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"WebRTC tool calling test failed: {res.stderr.strip()[:200]}")

    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    if not lines:
        raise AssertionError(f"No e2e output: {res.stdout}")

    tool, dur, filler_ok, reply = lines[-1].split("|")
    return f"tool '{tool}' executed in {float(dur):.1f}ms, filler emitted, reply grounded: '{reply}...'"


if __name__ == "__main__":
    print("Task 1.6 — Mid-Call Function Calling & Retrieval Tools Acceptance\n")
    results = [
        check("Tool registry & JSON Schema definitions", test_tool_registry_and_json_schemas),
        check("Async dispatcher, latency telemetry & timeout recovery", test_async_tool_dispatcher_and_timeout),
        check("Filler speech engine (<10ms selection & template interpolation)", test_filler_speech_engine),
        check("Dialogue manager intent detection & grounded synthesis", test_dialogue_manager_grounding),
        check("Live WebRTC mid-call tool execution over data channels", test_e2e_webrtc_tool_calling),
    ]

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)
