"""
Task 1.2 — streaming speech-to-text.

Joins a room, streams the caller's audio to Deepgram over a WebSocket, and
publishes transcripts back into the room as they arrive. No language model
and no synthesis yet: this task exists to prove the transcription leg on its
own, so that when the dialogue manager lands in 1.4 the input is known good.

Interim vs final is the thing to watch. Deepgram emits a best guess while
you are still speaking and revises it as more audio arrives; only the final
result is stable. The browser renders interim text greyed out so the
difference is visible rather than theoretical.
"""

import asyncio
import json
import logging
import os

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession
from livekit.plugins import deepgram

load_dotenv()
log = logging.getLogger("stt-worker")

# nova-3 is the plugin default and supersedes the nova-2 named in the
# estimation document. Override with STT_MODEL if you need to compare.
STT_MODEL = os.getenv("STT_MODEL", "nova-3")


class Transcriber(Agent):
    """No instructions to follow yet — this agent only listens."""

    def __init__(self) -> None:
        super().__init__(instructions="You transcribe. You do not reply.")


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()
    log.info("joined room %s", ctx.room.name)

    session = AgentSession(
        stt=deepgram.STT(
            model=STT_MODEL,
            language="en",
            # Both are what make this feel live rather than batched.
            interim_results=True,
            punctuate=True,
            # 25ms endpointing is aggressive on purpose: task 1.3 replaces
            # this with Silero VAD, which decides turn ends far better.
            endpointing_ms=25,
        ),
        turn_handling={
            # The SDK's default turn detector and its "adaptive" barge-in
            # model are hosted services reached over the LiveKit Cloud
            # gateway. This server is self-hosted, so both answer 401 and
            # retry forever. Deepgram's own endpointing closes turns until
            # task 1.3 brings in local VAD.
            "turn_detection": "stt",
            # Nothing to interrupt yet — the agent does not speak until 1.5.
            "interruption": {"enabled": False},
        },
    )

    # publish_data is a coroutine. Scheduling it (rather than awaiting) keeps
    # the audio pipeline unblocked; the set holds a strong reference so the
    # task is not garbage collected mid-flight.
    pending: set[asyncio.Task] = set()

    @session.on("user_input_transcribed")
    def on_transcript(ev):
        task = asyncio.create_task(
            ctx.room.local_participant.publish_data(
                json.dumps({
                    "type": "transcript",
                    "text": ev.transcript,
                    "is_final": ev.is_final,
                    "speaker": ev.speaker_id or "caller",
                }).encode(),
                topic="transcript",
                reliable=ev.is_final,   # interim frames may drop; finals may not
            )
        )
        pending.add(task)
        task.add_done_callback(pending.discard)
        log.info("%s %s", "FINAL " if ev.is_final else "interim", ev.transcript)

    await session.start(agent=Transcriber(), room=ctx.room)
    log.info("listening — model=%s", STT_MODEL)


if __name__ == "__main__":
    if not os.getenv("DEEPGRAM_API_KEY"):
        raise SystemExit(
            "DEEPGRAM_API_KEY is not set.\n"
            "  Get a key at https://console.deepgram.com (free tier includes credit),\n"
            "  add it to .env as DEEPGRAM_API_KEY=..., then: docker compose up -d agent"
        )
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
