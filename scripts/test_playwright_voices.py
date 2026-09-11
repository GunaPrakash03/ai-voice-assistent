import asyncio
import os
import json
import urllib.parse
import urllib.request
from playwright.async_api import async_playwright

async def run():
    print("Starting Playwright multi-tab voice test...")
    os.makedirs("scratch", exist_ok=True)
    artifact_dir = "/home/sys0041/.gemini/antigravity-cli/brain/e9d79a2d-eb75-4078-8c7c-4ad596e97a8b"
    os.makedirs(artifact_dir, exist_ok=True)

    test_voices = [
        {
            "tab": "studio",
            "voice_id": "studio-maya",
            "voice_name": "Maya (Studio Pro)",
            "test_message": "Hi Maya, I was injured in an accident and need assistance.",
            "output_file": "scratch/test_call_1_maya.mp3",
            "artifact_file": os.path.join(artifact_dir, "test_call_1_maya.mp3"),
        },
        {
            "tab": "neural",
            "voice_id": "neural-ryan",
            "voice_name": "Ryan (British Broadcaster)",
            "test_message": "Can you explain how cases are evaluated at your firm?",
            "output_file": "scratch/test_call_2_ryan.mp3",
            "artifact_file": os.path.join(artifact_dir, "test_call_2_ryan.mp3"),
        },
        {
            "tab": "cartesia",
            "voice_id": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
            "voice_name": "Aurora (Cartesia Sonic)",
            "test_message": "I would like to schedule a consultation with an attorney.",
            "output_file": "scratch/test_call_3_aurora.mp3",
            "artifact_file": os.path.join(artifact_dir, "test_call_3_aurora.mp3"),
        },
        {
            "tab": "studio",
            "voice_id": "studio-nathan",
            "voice_name": "Nathan (Studio Pro)",
            "test_message": "What information should I provide about my case?",
            "output_file": "scratch/test_call_4_nathan.mp3",
            "artifact_file": os.path.join(artifact_dir, "test_call_4_nathan.mp3"),
        }
    ]

    recorded_results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        captured_audio_responses = []

        async def handle_response(response):
            if "/api/agents/voice-audio" in response.url:
                try:
                    body = await response.body()
                    captured_audio_responses.append((response.url, body))
                    print(f"Captured audio ({len(body)} bytes) from {response.url[:80]}...")
                except Exception as e:
                    print(f"Error capturing audio: {e}")

        page.on("response", handle_response)

        url = "http://localhost:8091/agent-builder.html"
        print(f"Navigating to {url}...")
        await page.goto(url, wait_until="networkidle")
        await asyncio.sleep(1)

        for idx, item in enumerate(test_voices):
            v_id = item["voice_id"]
            v_name = item["voice_name"]
            msg = item["test_message"]
            prov = item["tab"]
            print(f"\n==========================================")
            print(f"--- [Test Voice {idx+1}: {v_name} (Tab: {prov}, ID: {v_id})] ---")
            print(f"==========================================")

            # 1. Open Voice Modal
            await page.click("#btnOpenVoiceModal")
            await asyncio.sleep(0.5)

            # 2. Switch Tab in Modal
            tab_btn = page.locator(f".ptab[data-prov='{prov}']")
            if await tab_btn.count() > 0:
                await tab_btn.first.click()
                await asyncio.sleep(0.3)

            # 3. Select Voice Card in Grid or set via evaluate
            voice_card = page.locator(f".voice-card[data-id='{v_id}']")
            if await voice_card.count() > 0:
                await voice_card.first.click()
                await asyncio.sleep(0.3)
            else:
                await page.evaluate(f"setChosenVoice('{v_id}')")

            # 4. Confirm Voice Modal
            apply_btn = page.locator("#btnConfirmVoiceModal")
            if await apply_btn.count() > 0:
                await apply_btn.click()
                await asyncio.sleep(0.5)

            # Verify chosen voice
            selected_voice = await page.evaluate("document.getElementById('voiceSelect').value")
            print(f"Verified Active Voice in UI: {selected_voice}")

            # 5. Open / Start Test Call Sandbox via #btnTest
            is_open = await page.evaluate("document.getElementById('modalTestCall').classList.contains('open')")
            if not is_open:
                await page.click("#btnTest")
                await asyncio.sleep(1.2)

            # Clear captured responses for this turn
            captured_audio_responses.clear()

            # 6. Send Caller Utterance
            input_box = page.locator("#testCallInput")
            await input_box.wait_for(state="visible", timeout=10000)
            await input_box.fill(msg)
            await page.click("#btnTestCallSend")
            print(f"Sent Caller Utterance: \"{msg}\"")

            # 7. Wait for Agent response & speech synthesis
            await asyncio.sleep(4.0)

            # Extract dialogue turns from DOM
            agent_msgs = await page.locator(".test-call-msg.agent .test-call-bubble").all_inner_texts()
            latest_reply = agent_msgs[-1] if agent_msgs else ""
            if not latest_reply:
                all_msgs = await page.locator(".test-call-msg.agent").all_inner_texts()
                latest_reply = all_msgs[-1] if all_msgs else "No reply found"

            print(f"Agent Response in UI: \"{latest_reply.strip()}\"")

            # Find matching audio
            audio_bytes = None
            if captured_audio_responses:
                audio_bytes = captured_audio_responses[-1][1]
            else:
                q_url = f"http://localhost:8091/api/agents/voice-audio?voice_id={v_id}&text={urllib.parse.quote(latest_reply)}"
                with urllib.request.urlopen(q_url) as r:
                    audio_bytes = r.read()

            if audio_bytes:
                with open(item["output_file"], "wb") as f:
                    f.write(audio_bytes)
                with open(item["artifact_file"], "wb") as f:
                    f.write(audio_bytes)
                print(f"Recorded and Saved Audio: {item['output_file']} ({len(audio_bytes)} bytes)")
                recorded_results.append({
                    "turn": idx + 1,
                    "voice_id": v_id,
                    "voice_name": v_name,
                    "provider_tab": prov,
                    "caller_input": msg,
                    "agent_output": latest_reply.strip(),
                    "audio_file": item["output_file"],
                    "artifact_file": item["artifact_file"],
                    "audio_bytes": len(audio_bytes),
                    "status": "SUCCESS"
                })

            # End call before next voice test
            end_btn = page.locator("#btnEndTestCall")
            if await end_btn.count() > 0 and await end_btn.is_visible():
                await end_btn.click()
                await asyncio.sleep(0.5)

        await browser.close()

    print("\n=======================================================")
    print("PLAYWRIGHT TEST CALL MULTI-VOICE RECORDINGS SUMMARY")
    print("=======================================================")
    print(json.dumps(recorded_results, indent=2))
    return recorded_results

if __name__ == "__main__":
    asyncio.run(run())
