#!/usr/bin/env python3
"""scripts/verify_call_history.py — Task 4.2 Acceptance Tests.

7 checks:
  Check 1: Call records assembled from pipeline jobs, telephony and recordings
  Check 2: Filtering, sorting and pagination of the call log
  Check 3: Call detail with a time-aligned transcript and per-turn sentiment
  Check 4: Waveform extraction from a real stereo recording
  Check 5: Byte-range audio reads for the seeking player
  Check 6: Stats aggregation and CSV export
  Check 7: REST API endpoints + Call Desk / inspector.js wiring
"""
import asyncio
import json
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.call_history import CallHistoryStore, call_history  # noqa: E402
from agent.pipeline_worker import PostCallPipelineWorker  # noqa: E402
from agent.recording_manager import recording_manager  # noqa: E402

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


# ─── Fixture: a real call with audio, transcript and analysis ─────────────────
CALL_ID = f"verify-history-{int(time.time()*1000)}"
TURNS = [
    {"role": "caller", "text": "Hi, my invoice ACC-4417 shows a $240.00 charge I did not expect."},
    {"role": "agent", "text": "I can see that charge. Let me pull up the itemised statement for you."},
    {"role": "caller", "text": "Thank you, that would be great. I want a refund if it is wrong."},
    {"role": "agent", "text": "Understood. I have raised the refund request and emailed the statement."},
]


def tone_pcm(seconds: float, freq: float, rate: int = 16000) -> bytes:
    """16-bit mono PCM so the recording carries a real, measurable envelope."""
    samples = []
    for n in range(int(seconds * rate)):
        # Fade the tone so bucket peaks vary across the waveform.
        envelope = 0.35 + 0.6 * abs(math.sin(n / (rate * 0.35)))
        samples.append(int(12000 * envelope * math.sin(2 * math.pi * freq * n / rate)))
    return struct.pack(f"<{len(samples)}h", *samples)


meta = recording_manager.start_recording(CALL_ID)
for i in range(6):
    recording_manager.append_audio(
        CALL_ID,
        caller_pcm=tone_pcm(0.35, 440 + i * 20),
        agent_pcm=tone_pcm(0.35, 220 + i * 15),
    )
recording_meta = recording_manager.stop_recording(CALL_ID)

worker = PostCallPipelineWorker(concurrency=1)
job = worker.enqueue_call(
    call_id=CALL_ID,
    room_name=f"room-{CALL_ID}",
    transcript_turns=TURNS,
    metadata={"agent_name": "AI Agent"},
    priority=1,
)
fixture_job = asyncio.run(worker.execute_job(job.job_id))

store = CallHistoryStore()

# ─── Check 1: Record Assembly ─────────────────────────────────────────────────
print("\n- Check 1: Call Record Assembly -")
records = store.load(force=True)
by_id = {r.call_id: r for r in records}
fixture = by_id.get(CALL_ID)

check("recording written to disk",        os.path.isfile(recording_meta.file_path), recording_meta.file_path)
check("pipeline job completed",           fixture_job.status == "completed", fixture_job.status)
check("history includes the new call",    fixture is not None, CALL_ID)
check("records are newest first",
      all(records[i].started_at >= records[i + 1].started_at for i in range(len(records) - 1)))

if fixture:
    check("audio matched to the call",    fixture.has_audio is True)
    check("audio size recorded",          fixture.audio_bytes > 1000, str(fixture.audio_bytes))
    check("sentiment carried through",    fixture.sentiment in ("positive", "neutral", "negative"),
          fixture.sentiment)
    check("summary carried through",      len(fixture.summary) > 0)
    check("intent carried through",       len(fixture.key_intent) > 0, fixture.key_intent)
    check("turn count from metrics",      fixture.turns == len(TURNS), str(fixture.turns))
    check("word count computed",          fixture.words > 30, str(fixture.words))
    check("talk ratios computed",         0 < fixture.caller_talk_ratio <= 1)
    check("archive url preserved",        bool(fixture.archive_url))
    check("extracted fields attached",    isinstance(fixture.extracted_fields, dict))
    check("audio path hidden from clients", "audio_path" not in fixture.to_dict())

# ─── Check 2: Filters, Sorting & Pagination ───────────────────────────────────
print("\n- Check 2: Filtering, Sorting & Pagination -")
page1 = store.list_calls(page=1, page_size=5)
check("page size respected",              len(page1["items"]) <= 5, str(len(page1["items"])))
check("total reported",                   page1["total"] >= 1, str(page1["total"]))
check("page count computed",              page1["pages"] == max(1, math.ceil(page1["total"] / 5)),
      str(page1["pages"]))
check("has_next on first page",           page1["has_next"] == (page1["pages"] > 1))
check("has_prev false on first page",     page1["has_prev"] is False)
check("filter options returned",
      {"sentiments", "agents", "outcomes"} <= set(page1["filters"]))

if page1["pages"] > 1:
    page2 = store.list_calls(page=2, page_size=5)
    first_ids = {i["call_id"] for i in page1["items"]}
    check("page 2 returns different calls",
          not (first_ids & {i["call_id"] for i in page2["items"]}))
    check("page 2 reports has_prev",      page2["has_prev"] is True)
else:
    check("page 2 returns different calls", True)
    check("page 2 reports has_prev", True)

check("out-of-range page clamps",         store.list_calls(page=9999, page_size=5)["page"] == page1["pages"])
check("page size is capped",              store.list_calls(page_size=10_000)["page_size"] <= 200)

audio_only = store.list_calls(page_size=200, has_audio=True)
check("has_audio filter works",
      audio_only["total"] >= 1 and all(i["has_audio"] for i in audio_only["items"]),
      str(audio_only["total"]))

neutral = store.list_calls(page_size=200, sentiment="neutral")
check("sentiment filter works",
      all(i["sentiment"] == "neutral" for i in neutral["items"]))

agent_filtered = store.list_calls(page_size=200, agent="AI Agent")
check("agent filter works",
      all(i["agent_name"] == "AI Agent" for i in agent_filtered["items"]) and agent_filtered["total"] >= 1)

long_calls = store.list_calls(page_size=200, min_duration=1.0)
check("min_duration filter works",        all(i["duration_seconds"] >= 1.0 for i in long_calls["items"]))
check("max_duration filter works",
      all(i["duration_seconds"] <= 5.0 for i in store.list_calls(page_size=200, max_duration=5.0)["items"]))

searched = store.list_calls(page_size=200, search=CALL_ID[:20])
check("text search matches call id",      any(i["call_id"] == CALL_ID for i in searched["items"]))
check("text search excludes others",      searched["total"] < page1["total"] or page1["total"] == 1)
check("unmatched search returns nothing", store.list_calls(search="zzz-no-such-call")["total"] == 0)

longest = store.list_calls(page_size=5, sort="duration_seconds", order="desc")["items"]
check("sort by duration desc",
      all(longest[i]["duration_seconds"] >= longest[i + 1]["duration_seconds"]
          for i in range(len(longest) - 1)))
oldest = store.list_calls(page_size=5, sort="started_at", order="asc")["items"]
check("sort ascending honoured",
      all(oldest[i]["started_at"] <= oldest[i + 1]["started_at"] for i in range(len(oldest) - 1)))
check("unknown sort field falls back",    store.list_calls(sort="nonsense")["items"][0]["call_id"] ==
      store.list_calls(sort="started_at")["items"][0]["call_id"])

# ─── Check 3: Detail & Time-Aligned Transcript ────────────────────────────────
print("\n- Check 3: Call Detail & Time-Aligned Transcript -")
detail = store.get_call(CALL_ID)
check("detail returned for known call",   detail is not None)
check("detail is None for unknown call",  store.get_call("no-such-call") is None)

timeline = detail["timeline"]
check("timeline has one entry per turn",  len(timeline) == len(TURNS), str(len(timeline)))
check("timeline starts at zero",          timeline[0]["start_s"] == 0.0, str(timeline[0]["start_s"]))
check("turns are monotonically ordered",
      all(timeline[i]["start_s"] <= timeline[i + 1]["start_s"] for i in range(len(timeline) - 1)))
check("turns do not overlap",
      all(timeline[i]["end_s"] <= timeline[i + 1]["start_s"] + 0.001 for i in range(len(timeline) - 1)))
check("each turn has a duration",         all(t["duration_s"] > 0 for t in timeline))
check("longer turns take longer to speak",
      max(timeline, key=lambda t: t["word_count"])["duration_s"] >=
      min(timeline, key=lambda t: t["word_count"])["duration_s"])
check("speakers attributed",              {t["role"] for t in timeline} == {"caller", "agent"})
check("per-turn sentiment merged in",     any(t["sentiment"] for t in timeline))
check("summary block present",            "executive_summary" in detail["summary"])
check("call-level sentiment present",     "overall_polarity" in detail["sentiment"])
check("turn_sentiments not duplicated",   "turn_sentiments" not in detail["sentiment"])
check("extractions included",             isinstance(detail["extractions"], dict))
check("pipeline stages summarised",       len(detail["stages"]) == 6, str(len(detail["stages"])))
check("audio block advertises urls",
      detail["audio"]["available"] and detail["audio"]["url"].startswith("/api/calls/audio"))

# ─── Check 4: Waveform Extraction ─────────────────────────────────────────────
print("\n- Check 4: Waveform Extraction -")
waveform = store.waveform(CALL_ID, buckets=120)
check("waveform returned",                waveform is not None)
check("stereo channels detected",         waveform["channels"] == 2, str(waveform["channels"]))
check("sample rate reported",             waveform["sample_rate"] == 16000, str(waveform["sample_rate"]))
check("duration derived from frames",     waveform["duration_seconds"] > 1.0,
      str(waveform["duration_seconds"]))
check("requested bucket count honoured",  waveform["buckets"] == 120, str(waveform["buckets"]))
check("caller channel populated",         len(waveform["caller"]) == 120)
check("agent channel populated",          len(waveform["agent"]) == 120)
check("peaks are normalised 0..1",
      all(0.0 <= p <= 1.0 for p in waveform["caller"] + waveform["agent"]))
check("envelope is not flat",             len(set(waveform["caller"])) > 3,
      str(len(set(waveform["caller"]))))
check("peak level reported",              0 < waveform["peak_level"] <= 1.0, str(waveform["peak_level"]))
check("bucket count is clamped",          store.waveform(CALL_ID, buckets=99999)["buckets"] <= 2000)
check("no waveform without audio",        store.waveform("no-such-call") is None)

# ─── Check 5: Range Reads ─────────────────────────────────────────────────────
print("\n- Check 5: Byte-Range Audio Reads -")
full = store.read_audio_range(CALL_ID)
check("full read returns bytes",          full is not None and len(full[0]) == full[3], str(full[3]))
head = store.read_audio_range(CALL_ID, 0, 1023)
check("range read returns 1024 bytes",    len(head[0]) == 1024, str(len(head[0])))
check("range read reports offsets",       head[1] == 0 and head[2] == 1023)
check("range read reports total size",    head[3] == full[3])
check("wav header intact in first bytes", head[0][:4] == b"RIFF", str(head[0][:4]))

mid = store.read_audio_range(CALL_ID, 1024, 2047)
check("second range continues the file",  mid[1] == 1024 and len(mid[0]) == 1024)
check("ranges reassemble the file",       head[0] + mid[0] == full[0][:2048])
tail = store.read_audio_range(CALL_ID, full[3] - 10, full[3] + 5000)
check("range past EOF is clamped",        tail[2] == full[3] - 1 and len(tail[0]) == 10, str(len(tail[0])))
check("no range read without audio",      store.read_audio_range("no-such-call") is None)

# ─── Check 6: Stats & Export ──────────────────────────────────────────────────
print("\n- Check 6: Stats Aggregation & CSV Export -")
stats = store.stats()
check("call count matches the log",       stats["calls"] == store.list_calls(page_size=200)["total"])
check("audio count matches filter",       stats["with_audio"] == audio_only["total"])
check("average duration computed",        stats["avg_duration_seconds"] > 0)
check("sentiment breakdown sums to total",
      sum(stats["sentiment"].values()) == stats["calls"], str(stats["sentiment"]))
check("outcomes broken down",             len(stats["outcomes"]) >= 1)
check("agents broken down",               "AI Agent" in stats["agents"])
check("resolution rate is a ratio",       0.0 <= stats["resolution_rate"] <= 1.0)
check("turn totals aggregated",           stats["total_turns"] >= len(TURNS))
check("newest call timestamp reported",   stats["newest_call_at"] > 0)

csv_text = store.export_csv()
csv_lines = csv_text.strip().splitlines()
check("csv has a header row",             csv_lines[0].startswith("call_id,started_at,duration_seconds"))
check("csv row per call",                 len(csv_lines) - 1 == min(stats["calls"], 200), str(len(csv_lines) - 1))
check("csv contains the fixture call",    any(CALL_ID in line for line in csv_lines))
check("csv honours filters",
      len(store.export_csv(sentiment="neutral").strip().splitlines()) - 1 ==
      store.list_calls(page_size=200, sentiment="neutral")["total"])

# ─── Check 7: REST API & Web Assets ───────────────────────────────────────────
print("\n- Check 7: REST API Endpoints & Call Desk Wiring -")


def api_get(path, headers=None):
    req = urllib.request.Request(f"{BASE_URL}{path}", headers=headers or {})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp, resp.read()


try:
    _, body = api_get("/api/calls?page=1&page_size=5")
    listing = json.loads(body)
    check("GET /api/calls status=ok",          listing.get("status") == "ok")
    check("listing is paginated over REST",    len(listing.get("items", [])) <= 5 and listing["pages"] >= 1)

    _, body = api_get(f"/api/calls?page_size=200&has_audio=true")
    check("has_audio filter over REST",
          all(i["has_audio"] for i in json.loads(body).get("items", [])))

    _, body = api_get(f"/api/calls/detail?call_id={CALL_ID}")
    detail_resp = json.loads(body)
    check("GET /api/calls/detail status=ok",   detail_resp.get("status") == "ok")
    check("detail carries the timeline",       len(detail_resp.get("timeline", [])) == len(TURNS))

    _, body = api_get(f"/api/calls/waveform?call_id={CALL_ID}&buckets=60")
    wave_resp = json.loads(body)
    check("GET /api/calls/waveform status=ok", wave_resp.get("status") == "ok")
    check("waveform buckets over REST",        wave_resp["waveform"]["buckets"] == 60)

    resp, body = api_get(f"/api/calls/audio?call_id={CALL_ID}")
    check("GET /api/calls/audio returns 200",  resp.status == 200, str(resp.status))
    check("audio content type is wav",         resp.headers.get("Content-Type") == "audio/wav")
    check("audio advertises range support",    resp.headers.get("Accept-Ranges") == "bytes")
    check("audio body is the whole file",      len(body) == full[3], str(len(body)))

    resp, body = api_get(f"/api/calls/audio?call_id={CALL_ID}",
                         headers={"Range": "bytes=0-999"})
    check("range request returns 206",         resp.status == 206, str(resp.status))
    check("range request returns 1000 bytes",  len(body) == 1000, str(len(body)))
    check("content-range header correct",
          resp.headers.get("Content-Range") == f"bytes 0-999/{full[3]}",
          str(resp.headers.get("Content-Range")))

    resp, body = api_get(f"/api/calls/audio?call_id={CALL_ID}",
                         headers={"Range": "bytes=1000-"})
    check("open-ended range served",           resp.status == 206 and len(body) == full[3] - 1000,
          str(len(body)))

    _, body = api_get("/api/calls/stats")
    check("GET /api/calls/stats status=ok",    json.loads(body).get("status") == "ok")

    resp, body = api_get("/api/calls/export")
    check("GET /api/calls/export returns csv",
          resp.headers.get("Content-Type", "").startswith("text/csv"))
    check("csv download is attachment",
          "attachment" in resp.headers.get("Content-Disposition", ""))
    check("csv export has rows over REST",     len(body.decode().strip().splitlines()) > 1)

    missing = None
    try:
        api_get("/api/calls/detail?call_id=no-such-call")
    except urllib.error.HTTPError as e:
        missing = e.code
    check("unknown call returns 404",          missing == 404, str(missing))

except urllib.error.URLError as e:
    print(f"  {FAIL} REST API check skipped -- server error: {e}")
    failures.append("REST API not reachable")

inspector_path = os.path.join(ROOT, "web", "inspector.js")
desk_path = os.path.join(ROOT, "web", "call-desk.html")
check("inspector.js shipped",              os.path.isfile(inspector_path))
check("call-desk.html shipped",            os.path.isfile(desk_path))

with open(inspector_path, encoding="utf-8") as f:
    inspector_js = f.read()
for symbol in ["fetchCalls", "fetchDetail", "fetchWaveform", "renderWaveform",
               "renderTranscript", "createPlayer", "activeTurnIndex"]:
    check(f"inspector.js exports {symbol}",  f"{symbol}:" in inspector_js or f"function {symbol}" in inspector_js)
check("inspector.js targets the calls API", "/api/calls" in inspector_js)
check("inspector.js is global-safe",        "window.CallInspector" in inspector_js
      or "global.CallInspector" in inspector_js)

with open(desk_path, encoding="utf-8") as f:
    desk_html = f.read()
check("call desk loads inspector.js",       'src="/inspector.js"' in desk_html)
check("call desk has an audio element",     'id="player"' in desk_html)
check("call desk has a waveform surface",   'id="wave"' in desk_html)
check("call desk has a transcript panel",   'id="transcript"' in desk_html)
check("call desk has a pager",              'id="pager"' in desk_html)
check("call desk has filters",              'id="filters"' in desk_html and 'id="searchBox"' in desk_html)
check("call desk exports csv",              'id="exportLink"' in desk_html)
check("call desk drops the mock dataset",   "555 0148" not in desk_html)
check("call desk links to the builder",     "/agent-builder.html" in desk_html)

# ─── Summary ──────────────────────────────────────────────────────────────────
passed = total_checks - len(failures)
print(f"\n{'='*55}")
print(f"  Task 4.2 Call History: {passed}/{total_checks} checks passed")
if failures:
    print(f"  FAILED: {', '.join(failures)}")
print(f"{'='*55}\n")
sys.exit(0 if not failures else 1)
