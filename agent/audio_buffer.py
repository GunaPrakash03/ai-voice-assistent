"""
Playout jitter buffer for the agent's voice.

TTS audio arrives over the network in bursts; the room output plays it straight through a 200 ms
queue, so whenever a burst is late the caller hears a gap ("flush audio emitter due to slow audio
generation" in the log, choppy voice on the recording). This sink holds the first ``prebuffer_ms``
of every reply before letting playout start, so short delivery hiccups are covered by audio that is
already in hand. Costs that much extra time-to-first-audio and nothing else; interruptions still
clear the buffer immediately.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Deque

from livekit import rtc
from livekit.agents.voice import io

log = logging.getLogger("audio-buffer")


class BufferedAudioOutput(io.AudioOutput):
    def __init__(self, next_in_chain: io.AudioOutput, prebuffer_ms: int = 400) -> None:
        super().__init__(label="playout-jitter-buffer",
                         capabilities=io.AudioOutputCapabilities(pause=bool(getattr(next_in_chain, "can_pause", False))),
                         next_in_chain=next_in_chain, sample_rate=next_in_chain.sample_rate)
        self.prebuffer = prebuffer_ms / 1000.0
        self._held: Deque[rtc.AudioFrame] = deque()
        self._held_seconds = 0.0
        self._flowing = False          # once true, frames pass straight through until the segment ends
        self._lock = asyncio.Lock()

    async def _release(self) -> None:
        while self._held:
            frame = self._held.popleft()
            await self.next_in_chain.capture_frame(frame)
        self._held_seconds = 0.0

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        async with self._lock:
            if self._flowing:
                await self.next_in_chain.capture_frame(frame)
                return
            self._held.append(frame)
            self._held_seconds += frame.duration
            if self._held_seconds >= self.prebuffer:
                self._flowing = True
                await self._release()

    def flush(self) -> None:
        super().flush()
        # End of a reply: whatever is still held (a reply shorter than the cushion) goes out now.
        if self._held:
            held = list(self._held)
            self._held.clear()
            self._held_seconds = 0.0

            async def drain() -> None:
                async with self._lock:
                    for f in held:
                        await self.next_in_chain.capture_frame(f)
                    self.next_in_chain.flush()
            asyncio.ensure_future(drain())
        else:
            self.next_in_chain.flush()
        self._flowing = False

    def clear_buffer(self) -> None:
        self._held.clear()
        self._held_seconds = 0.0
        self._flowing = False
        self.next_in_chain.clear_buffer()

    def on_attached(self) -> None:
        self.next_in_chain.on_attached()

    def on_detached(self) -> None:
        self.next_in_chain.on_detached()

    def pause(self) -> None:
        self.next_in_chain.pause()

    def resume(self) -> None:
        self.next_in_chain.resume()
