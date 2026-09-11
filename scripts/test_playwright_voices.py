#!/usr/bin/env python3
"""
scripts/test_playwright_voices.py — End-to-end browser flow tests for the Agent Builder
voice picker and the Test Call sandbox, driven through a real (headless) Chromium.

Flows covered
  F1  Page loads with no JS errors / failed requests
  F2  Voice modal: every provider tab renders the expected card count
  F3  Select a voice per provider tab and confirm it lands in #voiceSelect
  F4  Play Sample returns real audio (MPEG/WAV) for each selected voice
  F5  Rapid double Play must NOT trigger the Web Speech fallback (AbortError bug)
  F6  Test Call: send a caller utterance, wait for the agent reply, capture the
      synthesized reply audio for that voice
  F7  Retell tab: sample-backed voices present, preview plays the original recording

Recordings are written to web/audio/ (served at http://localhost:8091/audio/...).
Exit code is non-zero if any assertion fails.

Usage:  python3 scripts/test_playwright_voices.py [base_url]
"""
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request

from playwright.async_api import async_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8091"
OUT_DIR = os.path.join(ROOT, "web", "audio")

# (provider tab, voice id, display name, caller utterance, output file)
TEST_VOICES = [
    ("studio", "studio-maya", "Maya (Studio Pro)",
     "Hi Maya, I was injured in an accident and need assistance.", "test_call_1_maya"),
    ("neural", "neural-ryan", "Ryan (British Broadcaster)",
     "Can you explain how cases are evaluated at your firm?", "test_call_2_ryan"),
    ("cartesia", "f786b574-daa5-4673-aa0c-cbe3e8534c02", "Aurora (Cartesia Sonic)",
     "I would like to schedule a consultation with an attorney.", "test_call_3_aurora"),
    ("studio", "studio-nathan", "Nathan (Studio Pro)",
     "What information should I provide about my case?", "test_call_4_nathan"),
    ("elevenlabs", "EXAVITQu4vr4xnSDxMaL", "Sarah (ElevenLabs Turbo)",
     "Hi, I am calling about a personal injury claim.", "test_call_5_sarah"),
    ("retell", "retell-cimo", "Cimo (Retell AI)",
     "Can someone call me back tomorrow morning?", "test_call_6_cimo"),
]

REPLY_SEL = ".test-msg.agent .test-msg-bubble"
RESULTS = []
FAILS = []


def check(cond, label, detail=""):
    status = "PASS" if cond else "FAIL"
    RESULTS.append({"check": label, "status": status, "detail": detail})
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)
    return cond


def audio_kind(b: bytes) -> str:
    if b[:3] == b"ID3" or b[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mpeg"
    if b[:4] == b"RIFF":
        return "wav"
    return "unknown"


async def run() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"Playwright voice flow tests against {BASE}")
    t0 = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        page_errors, console_errors, failed_requests, audio_responses = [], [], [], []
        page.on("pageerror", lambda e: page_errors.append(str(e)[:200]))
        page.on("console", lambda m: console_errors.append(m.text[:200]) if m.type == "error" else None)

        async def on_response(r):
            if r.status >= 400:
                failed_requests.append((r.status, r.url))
            if "/api/agents/voice-audio" in r.url:
                try:
                    audio_responses.append((r.url, await r.body()))
                except Exception:
                    pass
        page.on("response", on_response)

        # ── F1: page load ──────────────────────────────────────────────
        print("\nF1  Page load")
        await page.goto(f"{BASE}/agent-builder.html", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(1)
        check(not page_errors, "no uncaught JS errors on load", "; ".join(page_errors[:2]))
        check(not failed_requests, "no failed HTTP requests on load", str(failed_requests[:2]))

        # ── F2: provider tabs ──────────────────────────────────────────
        print("\nF2  Voice modal provider tabs")
        await page.click("#btnOpenVoiceModal")
        await page.wait_for_selector("#providerTabs .ptab", timeout=5000)
        with urllib.request.urlopen(f"{BASE}/api/agents/voices") as r:
            api_voices = json.load(r)["voices"]
        by_prov = {}
        for v in api_voices:
            by_prov[v["provider"]] = by_prov.get(v["provider"], 0) + 1
        for prov, expected in sorted(by_prov.items()):
            tab = page.locator(f".ptab[data-prov='{prov}']")
            if await tab.count() == 0:
                check(False, f"tab exists for provider '{prov}'")
                continue
            await tab.first.click()
            await asyncio.sleep(0.25)
            cards = await page.locator("#voiceCardsGrid .voice-card").count()
            check(cards == expected, f"tab '{prov}' shows {expected} cards", f"got {cards}")
        await page.click("#btnConfirmVoiceModal")
        await asyncio.sleep(0.3)

        # ── F5: rapid double-play must not fall back to Web Speech ─────
        print("\nF5  Rapid double Play Sample (AbortError regression)")
        await page.evaluate("""() => { window.__spoken = [];
            const orig = window.speechSynthesis.speak.bind(window.speechSynthesis);
            window.speechSynthesis.speak = (u) => { window.__spoken.push(u.text.slice(0, 40)); return orig(u); }; }""")
        await page.click("#btnOpenVoiceModal")
        await page.locator(".ptab[data-prov='studio']").click()
        await asyncio.sleep(0.25)
        btns = page.locator("#voiceCardsGrid .voice-card .vc-btn-preview")
        await btns.nth(0).click()
        await asyncio.sleep(0.15)
        await btns.nth(1).click()
        await asyncio.sleep(3)
        spoken = await page.evaluate("window.__spoken")
        check(not spoken, "no Web Speech fallback fired on rapid double play", f"spoken={spoken}")
        await page.click("#btnConfirmVoiceModal")
        await asyncio.sleep(0.3)

        # ── F3/F4/F6 per voice ─────────────────────────────────────────
        for prov, vid, vname, utterance, out_name in TEST_VOICES:
            print(f"\nVoice: {vname}  [{prov} / {vid}]")

            # F3 select through the modal like a user
            await page.click("#btnOpenVoiceModal")
            await page.locator(f".ptab[data-prov='{prov}']").click()
            await asyncio.sleep(0.25)
            card = page.locator(f".voice-card[data-id='{vid}']")
            if not check(await card.count() > 0, f"F3 card present for {vid}"):
                await page.click("#btnConfirmVoiceModal")
                continue
            await card.first.click()
            await asyncio.sleep(0.2)
            await page.click("#btnConfirmVoiceModal")
            await asyncio.sleep(0.3)
            selected = await page.evaluate("document.getElementById('voiceSelect').value")
            check(selected == vid, "F3 voice applied to #voiceSelect", f"got {selected}")

            # F4 sample preview audio
            audio_responses.clear()
            await page.click("#btnPreviewVoice")
            for _ in range(40):
                if audio_responses:
                    break
                await asyncio.sleep(0.25)
            sample = audio_responses[-1][1] if audio_responses else b""
            check(len(sample) > 4000 and audio_kind(sample) != "unknown",
                  "F4 Play Sample returned real audio", f"{len(sample)} bytes, {audio_kind(sample)}")

            # F6 test call turn
            is_open = await page.evaluate("document.getElementById('modalTestCall').classList.contains('open')")
            if not is_open:
                await page.click("#btnTest")
                await page.wait_for_selector("#testCallInput", state="visible", timeout=10000)
                await asyncio.sleep(1.0)
            before = await page.locator(REPLY_SEL).count()
            audio_responses.clear()
            await page.fill("#testCallInput", utterance)
            await page.click("#btnTestCallSend")
            reply = ""
            try:
                await page.wait_for_function(
                    f"document.querySelectorAll('{REPLY_SEL}').length > {before}", timeout=15000)
                replies = await page.locator(REPLY_SEL).all_inner_texts()
                reply = (replies[-1] if replies else "").strip()
            except Exception:
                pass
            check(bool(reply), "F6 agent reply rendered in transcript", reply[:70])

            # wait for the reply audio request (URL must carry this voice id and the reply text)
            # match on BOTH the voice id and the reply text, so a late-arriving greeting
            # synthesis is never mistaken for the reply
            reply_audio = b""
            needle = reply[:30].lower()
            for _ in range(40):
                hits = [b for (u, b) in audio_responses
                        if f"voice_id={urllib.parse.quote(vid, safe='')}" in u
                        and needle and needle in urllib.parse.unquote(u).lower()]
                if hits:
                    reply_audio = hits[-1]
                    break
                await asyncio.sleep(0.25)
            ok = check(len(reply_audio) > 4000 and audio_kind(reply_audio) != "unknown",
                       "F6 reply synthesized with the selected voice",
                       f"{len(reply_audio)} bytes, {audio_kind(reply_audio)}")
            if ok:
                ext = "mp3" if audio_kind(reply_audio) == "mpeg" else "wav"
                path = os.path.join(OUT_DIR, f"{out_name}.{ext}")
                with open(path, "wb") as fh:
                    fh.write(reply_audio)
                print(f"        saved {os.path.relpath(path, ROOT)}  ->  {BASE}/audio/{out_name}.{ext}")

            end_btn = page.locator("#btnEndTestCall")
            if await end_btn.count() and await end_btn.is_visible():
                await end_btn.click()
                await asyncio.sleep(0.4)

        # ── F7: Retell sample-backed previews ──────────────────────────
        print("\nF7  Retell AI sample-backed voices")
        retell = [v for v in api_voices if v["provider"] == "retell"]
        check(len(retell) >= 18, "at least 18 Retell voices in catalogue", f"{len(retell)}")
        with urllib.request.urlopen(f"{BASE}/api/agents/voice-audio?voice_id=retell-cimo") as r:
            cimo = r.read()
            ctype = r.headers.get("Content-Type")
        check(audio_kind(cimo) in ("wav", "mpeg") and len(cimo) > 100000,
              "retell-cimo preview is the original recording", f"{len(cimo)} bytes, {ctype}")

        # ── wrap-up ────────────────────────────────────────────────────
        print("\nSession diagnostics")
        check(not page_errors, "no uncaught JS errors during flows", "; ".join(page_errors[:2]))
        check(not failed_requests, "no failed HTTP requests during flows", str(failed_requests[:3]))
        if console_errors:
            print(f"  (info) console.error lines: {len(console_errors)} — first: {console_errors[0]}")
        await browser.close()

    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    print("\n" + "=" * 60)
    print(f"  Playwright voice flows: {passed}/{len(RESULTS)} checks passed  ({time.time() - t0:.1f}s)")
    if FAILS:
        print("  FAILED: " + "; ".join(FAILS))
    print("=" * 60)
    with open(os.path.join(ROOT, "scratch", "playwright_voice_results.json"), "w") as fh:
        json.dump(RESULTS, fh, indent=2)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, "scratch"), exist_ok=True)
    sys.exit(asyncio.run(run()))
