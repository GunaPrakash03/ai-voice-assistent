#!/usr/bin/env python3
"""scripts/verify_end_call.py — calls end when the agent signs off.

1. Sign-off detection (agent_builder.looks_like_closing): common goodbyes are caught; questions,
   greetings and look-alike words are not.
2. Phone calls (Twilio media stream): a goodbye spoken in full closes the stream (Twilio then hangs
   up); a question, an interrupted goodbye, the caller speaking again, or the greeting turn keep it open.
3. The backend global instructions tell every agent how to end a call.
Offline; no Twilio, LiveKit or model calls.
"""

import asyncio
import os
import sys

os.environ["DATABASE_URL"] = ""
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import agent.twilio_stream as ts  # noqa: E402
from agent.agent_builder import agent_builder, looks_like_closing  # noqa: E402
from agent.firm_context import GLOBAL_INSTRUCTIONS  # noqa: E402

passed = 0
failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label} {detail}")


print("\n1. Sign-off detection")
for text in ("Bye!", "Okay, bye now.", "Thanks again, have a blessed day.", "Thank you for calling, goodbye.",
             "Someone from our team will call you back shortly.", "Perfect, an attorney will reach out within one business day. Take care.",
             "Enjoy the rest of your day!", "Thank you for your time, John.", "Have a good one.", "We'll be in touch. Good-bye."):
    check(f"goodbye: {text}", looks_like_closing(text))
for text in ("Is there anything else I can help with?", "Thanks for calling! How can I help you today?",
             "Someone will call you back. May I have your number?", "Could you tell me what happened?", "I'm sorry to hear that.",
             "Byers Street is near our office, would that work for you?", "We handle family law and estate planning.", ""):
    check(f"not a goodbye: {text!r}", not looks_like_closing(text))

print("\n2. Phone calls hang up after the goodbye")
ts.PLAYBACK_LEAD_SECS = 0.0


async def run(reply, interrupted=False, caller_speaks=False, history_len=4, inactive=False):
    closed = []
    s = ts.TwilioMediaStreamSession(lambda o: None, lambda: closed.append(1))
    s.call_sid = "CA-test"
    s.history = [{"speaker": "x", "text": "y"}] * history_len
    s.is_active = not inactive
    agent_builder._preview_reply = lambda *a, **k: reply

    async def fake_speak(text):
        s._playing_progress = 0.4 if interrupted else 1.0
    s._speak_agent_text = fake_speak
    s._current_speech_task = asyncio.current_task()
    if caller_speaks:
        async def later():
            await asyncio.sleep(0.3)
            s._speech_accumulator.append("wait, one more thing")
        asyncio.ensure_future(later())
    await s._process_and_reply("that's all, thanks")
    return bool(closed), s.hung_up_by_agent


async def main():
    check("goodbye spoken in full ends the call", await run("Thank you for calling. Someone will call you back. Goodbye.") == (True, True))
    check("a question keeps the call open", await run("Is there anything else I can help with?") == (False, False))
    check("an interrupted goodbye keeps the call open", await run("Thanks for calling, goodbye.", interrupted=True) == (False, False))
    check("caller speaking after the goodbye keeps it open", await run("Have a great day!", caller_speaks=True) == (False, False))
    check("the greeting turn never hangs up", await run("Thanks for calling, goodbye.", history_len=1) == (False, False))
    check("an already-ended call is left alone", await run("Goodbye.", inactive=True) == (False, False))
    s = ts.TwilioMediaStreamSession(lambda o: None)
    check("sessions without a close hook still work", s.close_stream is None and not s.hung_up_by_agent)

asyncio.run(main())

print("\n3. Global instructions")
check("every agent is told how to end a call", "Ending the call" in GLOBAL_INSTRUCTIONS and "Goodbye" in GLOBAL_INSTRUCTIONS)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
