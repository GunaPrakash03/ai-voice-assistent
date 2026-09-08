"""
Pipeline test — no microphone, no human.

Joins the room and publishes real audio frames, which is enough to get the
worker dispatched a job and run its session start to finish. It also
subscribes to the `transcript` topic, so it sees exactly what the browser
sees: the worker's transcripts coming back as data messages.

  python3 scripts/publish_audio.py            # 220Hz tone: pipeline only
  python3 scripts/publish_audio.py speech.wav # 16-bit mono WAV: transcripts too

A tone proves dispatch, session config and audio reaching the Deepgram
socket, but a tone is not speech, so nothing comes back. Give it a WAV of
someone talking and the round trip is complete. A bad key shows up either
way as a clean 401 in the worker log.

Run it from the compose network, where "livekit" resolves:

  docker run --rm --network voice-agent-service_default \
    -e LIVEKIT_URL=ws://livekit:7880 -e LIVEKIT_API_KEY=devkey \
    -e LIVEKIT_API_SECRET=<secret from .env> \
    -v "$PWD/agent:/app/agent:ro" -v "$PWD/scripts:/app/scripts:ro" \
    voice-agent:dev python /app/scripts/publish_audio.py
"""

import asyncio
import json
import math
import os
import sys
import wave

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, "/app")

from livekit import rtc                       # noqa: E402
from agent.token import join_token            # noqa: E402

URL = os.environ["LIVEKIT_URL"]
ROOM = os.environ.get("ROOM", "test-room")
TONE_SECS = int(os.environ.get("SECS", "10"))
WAV = sys.argv[1] if len(sys.argv) > 1 else None
FRAME_MS = 10

transcripts = []


def tone_frames(rate=48000):
    """A 220Hz sine, in 10ms frames."""
    samples = rate * FRAME_MS // 1000
    frame = rtc.AudioFrame.create(rate, 1, samples)
    phase = 0.0
    for _ in range(TONE_SECS * 1000 // FRAME_MS):
        for s in range(samples):
            frame.data[s] = int(6000 * math.sin(phase))
            phase += 2 * math.pi * 220 / rate
        yield frame


def wav_frames(path):
    """A 16-bit mono WAV, in 10ms frames. Rate is whatever the file uses."""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise SystemExit(f"{path}: need 16-bit mono PCM")
        rate = w.getframerate()
        samples = rate * FRAME_MS // 1000
        while True:
            raw = w.readframes(samples)
            if len(raw) < samples * 2:
                return                       # drop the ragged last frame
            frame = rtc.AudioFrame(raw, rate, 1, samples)
            yield frame


async def main():
    if WAV:
        with wave.open(WAV, "rb") as w:
            rate = w.getframerate()
            secs = w.getnframes() / rate
        print(f"publishing {WAV} — {secs:.1f}s at {rate}Hz", flush=True)
        frames = wav_frames(WAV)
    else:
        rate = 48000
        print(f"publishing a {TONE_SECS}s tone (no transcript expected)", flush=True)
        frames = tone_frames(rate)

    token = join_token(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"],
                       ROOM, "audio-test")
    room = rtc.Room()

    @room.on("data_received")
    def on_data(packet):
        if packet.topic != "transcript":
            return
        msg = json.loads(packet.data.decode())
        mark = "FINAL " if msg["is_final"] else "interim"
        print(f"  {mark} {msg['text']}", flush=True)
        if msg["is_final"]:
            transcripts.append(msg["text"])

    await room.connect(URL, token)
    print("connected to", room.name, flush=True)

    source = rtc.AudioSource(rate, 1)
    track = rtc.LocalAudioTrack.create_audio_track("audio-test", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    print("published audio track — waiting for the worker to join", flush=True)
    await asyncio.sleep(3)          # the job takes a moment to dispatch

    for frame in frames:
        await source.capture_frame(frame)
    await asyncio.sleep(3)          # let the last final transcript land
    await room.disconnect()

    if WAV:
        print(f"\n{len(transcripts)} final transcript(s) received")
        raise SystemExit(0 if transcripts else 1)
    print("\ndone — check the worker log: docker compose logs --tail 40 agent")

asyncio.run(main())
