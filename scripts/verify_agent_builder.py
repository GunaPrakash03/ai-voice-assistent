#!/usr/bin/env python3
"""scripts/verify_agent_builder.py — Task 4.1 Acceptance Tests.

7 checks:
  Check 1: Config model, voice/model/preset catalogues & tool registry binding
  Check 2: Config validation and spoken-dialogue prompt linting
  Check 3: CRUD — create, update, clone, activate, delete
  Check 4: Revision history, diffs, rollback and disk persistence
  Check 5: Sandbox test-run (turns, tool matching, latency & cost estimates)
  Check 6: REST API endpoints for the builder UI
  Check 7: Live WebRTC apply_agent_config dispatch with the agent worker
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.agent_builder import (  # noqa: E402
    AgentBuilder,
    MAX_PROMPT_CHARS,
    PROMPT_PRESETS,
    STATE_FILE,
    VOICE_CATALOG,
    agent_builder,
)

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


GOOD_PROMPT = (
    "You are the intake agent for a dental clinic.\n\n"
    "Answer in one or two short sentences, then stop.\n"
    "Never read URLs or reference numbers aloud unless asked.\n"
    "If the caller is in pain, transfer to a human immediately."
)

# ─── Check 1: Model & Catalogues ──────────────────────────────────────────────
print("\n- Check 1: Config Model, Catalogues & Tool Registry -")
voices = agent_builder.list_voices()
models = agent_builder.list_models()
presets = agent_builder.list_presets()
tools = agent_builder.available_tools()

check("voice catalogue populated",        len(voices) >= 6, str(len(voices)))
check("voices carry latency + cost",
      all(v["first_audio_ms"] > 0 and v["cost_per_1k_chars"] >= 0 for v in voices))
check("both TTS providers offered",
      {v["provider"] for v in voices} >= {"cartesia", "deepgram"},
      str({v["provider"] for v in voices}))
check("llm catalogue populated",          len(models) >= 4, str(len(models)))
check("models carry ttft + cost",
      all(m["ttft_ms"] > 0 and m["cost_per_1k_tokens"] > 0 for m in models))
check("prompt presets available",         len(presets) >= 4, str(len(presets)))
check("presets carry prompt + greeting",
      all({"title", "first_message", "system_prompt"} <= set(p) for p in presets.values()))
check("tools sourced from live registry", len(tools) >= 5, str(len(tools)))
check("check_availability tool exposed",
      any(t["name"] == "check_availability" for t in tools))
check("tools expose JSON Schema params",
      all(isinstance(t["parameters"], dict) for t in tools))
check("default agent seeded",             len(agent_builder.list_agents()) >= 1)
check("exactly one agent is live",
      sum(1 for a in agent_builder.list_agents() if a["active"]) == 1,
      str(sum(1 for a in agent_builder.list_agents() if a["active"])))

# ─── Check 2: Validation & Linting ────────────────────────────────────────────
print("\n- Check 2: Validation & Prompt Linting -")
base_cfg = {"name": "Test", "first_message": "Hello.", "system_prompt": GOOD_PROMPT,
            "temperature": 0.7, "voice_id": VOICE_CATALOG[0].voice_id,
            "llm_model": "gpt-4o-mini", "tools": ["check_availability"]}

ok_valid, errs_valid = agent_builder.validate(base_cfg)
check("valid config passes",              ok_valid and not errs_valid, str(errs_valid))
check("missing name rejected",            not agent_builder.validate({**base_cfg, "name": "  "})[0])
check("empty prompt rejected",            not agent_builder.validate({**base_cfg, "system_prompt": ""})[0])
check("missing greeting rejected",        not agent_builder.validate({**base_cfg, "first_message": ""})[0])
check("oversized prompt rejected",
      not agent_builder.validate({**base_cfg, "system_prompt": "x" * (MAX_PROMPT_CHARS + 1)})[0])
check("out-of-range temperature rejected", not agent_builder.validate({**base_cfg, "temperature": 3.0})[0])
check("non-numeric temperature rejected",  not agent_builder.validate({**base_cfg, "temperature": "warm"})[0])
check("unknown voice rejected",            not agent_builder.validate({**base_cfg, "voice_id": "nope"})[0])
check("unknown model rejected",            not agent_builder.validate({**base_cfg, "llm_model": "gpt-9"})[0])
check("unknown tool rejected",             not agent_builder.validate({**base_cfg, "tools": ["do_magic"]})[0])
check("errors name the offending field",
      any("temperature" in e for e in agent_builder.validate({**base_cfg, "temperature": 3.0})[1]))

check("clean prompt lints clean",          agent_builder.lint_prompt(GOOD_PROMPT) == [],
      str([f["id"] for f in agent_builder.lint_prompt(GOOD_PROMPT)]))
lint_ids = lambda p: {f["id"] for f in agent_builder.lint_prompt(p)}  # noqa: E731
check("missing brevity guidance flagged",
      "no_length_guidance" in lint_ids("You are an agent. Transfer to a human when asked about URLs aloud."))
check("markdown formatting flagged",
      "markdown_formatting" in lint_ids(GOOD_PROMPT + "\n\n**Never** do that."))
check("option-listing instruction flagged",
      "asks_to_list_options" in lint_ids(GOOD_PROMPT + "\nAlways list all options for the caller."))
check("missing escalation path flagged",
      "no_escalation_path" in lint_ids("You are a bot. Keep answers short. Never read URLs aloud."))
check("persona opener suggested",
      "second_person_persona" in lint_ids("Keep answers short. Transfer when asked. Never read URLs aloud."))
check("lint findings carry a hint",
      all(f["hint"] for f in agent_builder.lint_prompt("Be helpful.")))

# ─── Check 3: CRUD ────────────────────────────────────────────────────────────
print("\n- Check 3: Create, Update, Clone, Activate, Delete -")
created = agent_builder.create_agent(
    name="Verify Clinic Agent",
    first_message="Clinic line, how can I help?",
    system_prompt=GOOD_PROMPT,
    tools=["check_availability", "book_appointment"],
    voice_id=VOICE_CATALOG[3].voice_id,
    llm_model="claude-haiku-4-5-20251001",
)
check("agent_id slugified from name",     created.agent_id == "verify-clinic-agent", created.agent_id)
check("starts at revision 1",             created.revision == 1)
check("new agent is not live",            created.active is False)
check("get_agent returns the config",     agent_builder.get_agent(created.agent_id) is not None)
check("appears in list_agents",
      any(a["agent_id"] == created.agent_id for a in agent_builder.list_agents()))

duplicate = agent_builder.create_agent(
    name="Verify Clinic Agent", first_message="Hi.", system_prompt=GOOD_PROMPT)
check("duplicate name gets unique id",    duplicate.agent_id == "verify-clinic-agent-2", duplicate.agent_id)

invalid_rejected = False
try:
    agent_builder.create_agent(name="Bad", first_message="Hi.", system_prompt="Ok.", temperature=9)
except ValueError:
    invalid_rejected = True
check("create rejects invalid config",    invalid_rejected)

updated = agent_builder.update_agent(created.agent_id, {"temperature": 0.35, "name": "Verify Clinic Intake"})
check("update applies changes",           updated.temperature == 0.35 and updated.name == "Verify Clinic Intake")
check("update bumps revision",            updated.revision == 2, str(updated.revision))
check("no-op update keeps revision",
      agent_builder.update_agent(created.agent_id, {"temperature": 0.35}).revision == 2)
bad_update = False
try:
    agent_builder.update_agent(created.agent_id, {"voice_id": "not-a-voice"})
except ValueError:
    bad_update = True
check("update rejects unknown voice",     bad_update)

missing_update = False
try:
    agent_builder.update_agent("ghost-agent", {"name": "X"})
except KeyError:
    missing_update = True
check("update on unknown agent raises",   missing_update)

clone = agent_builder.clone_agent(created.agent_id)
check("clone gets a new id",              clone.agent_id != created.agent_id, clone.agent_id)
check("clone copies the prompt",          clone.system_prompt == updated.system_prompt)
check("clone is not live",                clone.active is False)

agent_builder.set_active(created.agent_id)
live = [a for a in agent_builder.list_agents() if a["active"]]
check("activation makes exactly one live", len(live) == 1 and live[0]["agent_id"] == created.agent_id,
      str([a["agent_id"] for a in live]))
check("get_active_agent matches",         agent_builder.get_active_agent().agent_id == created.agent_id)

check("delete removes the agent",         agent_builder.delete_agent(duplicate.agent_id))
check("deleted agent is gone",            agent_builder.get_agent(duplicate.agent_id) is None)
check("delete of unknown agent is False", agent_builder.delete_agent("ghost-agent") is False)
agent_builder.delete_agent(clone.agent_id)

# ─── Check 4: Revisions & Persistence ─────────────────────────────────────────
print("\n- Check 4: Revision History, Rollback & Persistence -")
agent_builder.update_agent(created.agent_id, {"system_prompt": GOOD_PROMPT + "\nAlways confirm the date."},
                           note="Add confirmation rule")
revisions = agent_builder.list_revisions(created.agent_id)
check("revisions recorded",               len(revisions) >= 3, str(len(revisions)))
check("revisions are newest first",       revisions[0]["revision"] > revisions[-1]["revision"])
check("revision records changed fields",  "system_prompt" in revisions[0]["changed_fields"],
      str(revisions[0]["changed_fields"]))
check("revision keeps the note",          revisions[0]["note"] == "Add confirmation rule")
check("revision stores a full snapshot",  "system_prompt" in revisions[0]["config"])

diff = agent_builder.diff_revisions(created.agent_id, 1, revisions[0]["revision"])
check("diff reports changed keys",        "system_prompt" in diff["changed"] and "name" in diff["changed"],
      str(list(diff["changed"])))
check("diff carries from/to values",      diff["changed"]["name"]["to"] == "Verify Clinic Intake")

before_rollback = agent_builder.get_agent(created.agent_id).revision
rolled = agent_builder.rollback(created.agent_id, 1)
check("rollback restores earlier prompt", rolled.system_prompt == GOOD_PROMPT)
check("rollback restores earlier name",   rolled.name == "Verify Clinic Agent")
check("rollback moves history forward",   rolled.revision == before_rollback + 1,
      f"{rolled.revision} vs {before_rollback}")

missing_rev = False
try:
    agent_builder.rollback(created.agent_id, 999)
except KeyError:
    missing_rev = True
check("rollback to unknown revision raises", missing_rev)

agent_builder.persist()
check("registry persisted to config/agents.json", os.path.isfile(STATE_FILE))
reloaded = AgentBuilder()
check("agents reload from disk",
      reloaded.get_agent(created.agent_id) is not None)
check("revisions reload from disk",
      len(reloaded.list_revisions(created.agent_id)) == len(agent_builder.list_revisions(created.agent_id)))
check("reloaded prompt matches",
      reloaded.get_agent(created.agent_id).system_prompt == rolled.system_prompt)

# ─── Check 5: Sandbox Test Run ────────────────────────────────────────────────
print("\n- Check 5: Sandbox Test Run -")
utterances = ["I need to book an appointment for Thursday",
              "This is urgent, can someone help right now?"]
run = agent_builder.test_run(created.agent_id, utterances)

check("opens with the first message",     run["turns"][0]["text"] == "Clinic line, how can I help?",
      run["turns"][0]["text"])
check("one agent reply per utterance",    len(run["turns"]) == 1 + 2 * len(utterances), str(len(run["turns"])))
check("caller turns are attributed",
      [t["speaker"] for t in run["turns"]] == ["agent", "caller", "agent", "caller", "agent"])
check("booking utterance picks a tool",   "book_appointment" in run["tools_triggered"],
      str(run["tools_triggered"]))
check("voice metadata returned",          run["voice"]["voice_id"] == VOICE_CATALOG[3].voice_id)
check("model metadata returned",          run["model"]["model"] == "claude-haiku-4-5-20251001")
check("latency estimate produced",        run["estimates"]["avg_first_audio_ms"] > 0)
check("latency measured against 700ms",   run["estimates"]["meets_700ms_target"] is True,
      str(run["estimates"]["max_first_audio_ms"]))
check("spoken duration estimated",        run["estimates"]["spoken_seconds"] > 0)
check("tts cost estimated",               run["estimates"]["tts_cost_usd"] > 0)
check("lint findings ride along",         isinstance(run["lint"], list))

preview = agent_builder.preview_voice(VOICE_CATALOG[0].voice_id, "Thanks for calling.")
check("voice preview returns cost",       preview["estimated_cost_usd"] > 0)
check("voice preview returns latency",    preview["estimated_first_audio_ms"] == VOICE_CATALOG[0].first_audio_ms)

missing_run = False
try:
    agent_builder.test_run("ghost-agent", [])
except KeyError:
    missing_run = True
check("test_run on unknown agent raises", missing_run)

# ─── Check 6: REST API ────────────────────────────────────────────────────────
print("\n- Check 6: REST API Endpoints -")


def api_get(path):
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def api_post(path, body):
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


api_agent_id = None
try:
    voices_resp = api_get("/api/agents/voices")
    check("GET /api/agents/voices status=ok",  voices_resp.get("status") == "ok")
    check("voices + models returned",
          len(voices_resp.get("voices", [])) >= 6 and len(voices_resp.get("models", [])) >= 4)

    tools_resp = api_get("/api/agents/tools")
    check("GET /api/agents/tools status=ok",   tools_resp.get("status") == "ok")
    check("tool list is populated",            len(tools_resp.get("tools", [])) >= 5)

    presets_resp = api_get("/api/agents/presets")
    check("GET /api/agents/presets status=ok", presets_resp.get("status") == "ok")
    check("presets match the module",
          set(presets_resp.get("presets", {})) == set(PROMPT_PRESETS))

    create_resp = api_post("/api/agents/create", {
        "name": "REST Verify Agent",
        "first_message": "Hi, how can I help?",
        "system_prompt": GOOD_PROMPT,
        "tools": ["check_availability"],
    })
    api_agent_id = create_resp.get("agent", {}).get("agent_id")
    check("POST /api/agents/create status=ok", create_resp.get("status") == "ok")
    check("created agent returned",            bool(api_agent_id), str(api_agent_id))

    list_resp = api_get("/api/agents")
    check("GET /api/agents status=ok",         list_resp.get("status") == "ok")
    check("created agent appears in list",
          any(a["agent_id"] == api_agent_id for a in list_resp.get("agents", [])))
    check("active agent reported",             list_resp.get("active_agent") is not None)

    get_resp = api_get(f"/api/agents/get?agent_id={api_agent_id}")
    check("GET /api/agents/get status=ok",     get_resp.get("status") == "ok")
    check("get returns lint findings",         isinstance(get_resp.get("lint"), list))

    update_resp = api_post("/api/agents/update", {
        "agent_id": api_agent_id, "changes": {"temperature": 0.2}, "note": "REST edit"})
    check("POST /api/agents/update status=ok", update_resp.get("status") == "ok")
    check("update bumps revision over REST",   update_resp.get("agent", {}).get("revision") == 2,
          str(update_resp.get("agent", {}).get("revision")))

    lint_resp = api_post("/api/agents/lint", {"system_prompt": "Be helpful."})
    check("POST /api/agents/lint status=ok",   lint_resp.get("status") == "ok")
    check("lint flags a weak prompt",          len(lint_resp.get("lint", [])) > 0)

    test_resp = api_post("/api/agents/test", {
        "agent_id": api_agent_id, "utterances": ["Can I check availability for Friday?"]})
    check("POST /api/agents/test status=ok",   test_resp.get("status") == "ok")
    check("sandbox returns turns over REST",   len(test_resp.get("result", {}).get("turns", [])) == 3)

    preview_resp = api_post("/api/agents/voice-preview",
                            {"voice_id": VOICE_CATALOG[1].voice_id, "text": "Hello there."})
    check("POST /api/agents/voice-preview ok", preview_resp.get("status") == "ok")
    check("preview prices the line",           preview_resp.get("preview", {}).get("estimated_cost_usd", 0) > 0)

    revisions_resp = api_get(f"/api/agents/revisions?agent_id={api_agent_id}&from=1&to=2")
    check("GET /api/agents/revisions status=ok", revisions_resp.get("status") == "ok")
    check("revision diff returned over REST",  "temperature" in revisions_resp.get("diff", {}).get("changed", {}),
          str(revisions_resp.get("diff", {}).get("changed", {}).keys()))

    rollback_resp = api_post("/api/agents/rollback", {"agent_id": api_agent_id, "revision": 1})
    check("POST /api/agents/rollback status=ok", rollback_resp.get("status") == "ok")
    check("rollback restores temperature",     rollback_resp.get("agent", {}).get("temperature") == 0.7,
          str(rollback_resp.get("agent", {}).get("temperature")))

    activate_resp = api_post("/api/agents/activate", {"agent_id": api_agent_id})
    check("POST /api/agents/activate status=ok", activate_resp.get("status") == "ok")
    check("activated agent is live",           activate_resp.get("agent", {}).get("active") is True)

    stats_resp = api_get("/api/agents/stats")
    check("GET /api/agents/stats status=ok",   stats_resp.get("status") == "ok")
    check("stats count voices and tools",
          stats_resp.get("stats", {}).get("voices", 0) >= 6
          and stats_resp.get("stats", {}).get("tools_available", 0) >= 5)

    clone_resp = api_post("/api/agents/clone", {"agent_id": api_agent_id, "name": "REST Clone"})
    check("POST /api/agents/clone status=ok",  clone_resp.get("status") == "ok")
    api_post("/api/agents/delete", {"agent_id": clone_resp.get("agent", {}).get("agent_id")})

    missing_code = None
    try:
        api_get("/api/agents/get?agent_id=ghost-agent")
    except urllib.error.HTTPError as e:
        missing_code = e.code
    check("unknown agent returns 404",         missing_code == 404, str(missing_code))

    delete_resp = api_post("/api/agents/delete", {"agent_id": api_agent_id})
    check("POST /api/agents/delete status=ok", delete_resp.get("removed") is True)

except urllib.error.URLError as e:
    print(f"  {FAIL} REST API check skipped -- server error: {e}")
    failures.append("REST API not reachable")

# ─── Check 7: Live WebRTC Dispatch ────────────────────────────────────────────
print("\n- Check 7: Live WebRTC apply_agent_config Dispatch -")
room_name = f"verify-builder-{int(time.time()*1000)}"
LIVE_SCRIPT = """
import asyncio, json, os
from livekit import rtc
from agent.token import join_token

async def test():
    key = os.environ['LIVEKIT_API_KEY']
    secret = os.environ['LIVEKIT_API_SECRET']
    url = os.environ['LIVEKIT_URL']
    token = join_token(key, secret, '__ROOM__', 'builder-probe')
    room = rtc.Room()

    loop = asyncio.get_event_loop()
    agent_ready = loop.create_future()
    listed = loop.create_future()
    applied = loop.create_future()

    @room.on('participant_connected')
    def on_p(p):
        if not agent_ready.done():
            agent_ready.set_result(p)

    @room.on('data_received')
    def on_data(packet):
        try:
            data = json.loads(packet.data.decode())
        except Exception:
            return
        if packet.topic != 'agent_config_event':
            return
        if data.get('event') == 'agents_listed' and not listed.done():
            listed.set_result(data)
        elif data.get('event') == 'agent_config_applied' and not applied.done():
            applied.set_result(data)

    await room.connect(url, token)
    for p in room.remote_participants.values():
        if not agent_ready.done():
            agent_ready.set_result(p)

    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track('mic', source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    await asyncio.wait_for(agent_ready, timeout=20.0)
    await asyncio.sleep(0.5)

    await room.local_participant.publish_data(
        json.dumps({'action': 'list_agents'}).encode(), reliable=True)
    agents = await asyncio.wait_for(listed, timeout=15.0)
    target = agents.get('active_agent') or agents['agents'][0]['agent_id']

    await room.local_participant.publish_data(
        json.dumps({'action': 'apply_agent_config', 'agent_id': target}).encode(), reliable=True)
    res = await asyncio.wait_for(applied, timeout=15.0)
    await room.disconnect()

    print(f"{res.get('agent_id')}|{res.get('revision')}|{res.get('voice_id')}|"
          f"{len(agents.get('agents', []))}|{res.get('prompt_chars')}")

asyncio.run(test())
""".replace("__ROOM__", room_name)

try:
    # The worker mounts ./agent read-only, so it needs a restart to load the
    # agent_config data-channel actions added by this task.
    subprocess.run(["docker", "restart", "voice-agent-worker"],
                   capture_output=True, text=True, timeout=90)
    time.sleep(12)
    res = subprocess.run(["docker", "exec", "voice-agent-worker", "python", "-c", LIVE_SCRIPT],
                         capture_output=True, text=True, timeout=180)
    lines = [l.strip() for l in res.stdout.splitlines() if "|" in l]
    check("worker answered over the data channel", bool(lines),
          (res.stderr or res.stdout).strip()[:160])
    if lines:
        agent_id, revision, voice_id, agent_count, prompt_chars = lines[-1].split("|", 4)
        check("worker listed its agents",        int(agent_count) >= 1, agent_count)
        check("worker applied a config",         bool(agent_id) and agent_id != "None", agent_id)
        check("applied config reports revision", int(revision) >= 1, revision)
        check("applied config reports voice",    len(voice_id) > 5, voice_id)
        check("applied prompt is non-empty",     int(prompt_chars) > 0, prompt_chars)
except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
    print(f"  {FAIL} Live dispatch check skipped -- {e}")
    failures.append("Live WebRTC dispatch unavailable")

# ─── Cleanup ──────────────────────────────────────────────────────────────────
agent_builder.delete_agent(created.agent_id)
agent_builder.persist()

# ─── Summary ──────────────────────────────────────────────────────────────────
passed = total_checks - len(failures)
print(f"\n{'='*55}")
print(f"  Task 4.1 Agent Builder: {passed}/{total_checks} checks passed")
if failures:
    print(f"  FAILED: {', '.join(failures)}")
print(f"{'='*55}\n")
sys.exit(0 if not failures else 1)
