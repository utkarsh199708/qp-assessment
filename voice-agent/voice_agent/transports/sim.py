"""A scripted caller for simulations and tests.

Each :class:`ScriptLine` is spoken as a burst of tone frames (real energy for
the VAD) after the agent finishes its turn, or - for barge-in tests - after the
agent has been talking for ``interrupt_after_s`` seconds.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ..audio import PCM16_16K, AudioFormat, frame_bytes, silence, tone
from ..clock import Clock
from ..providers.mock import CHARS_PER_SECOND


@dataclass
class ScriptLine:
    say: str
    interrupt_after_s: float | None = None  # barge in once the agent has spoken this long
    pause_before_s: float = 0.6  # think time after the agent stops
    speak_seconds: float | None = None  # default: derived from text length
    max_wait_s: float = 15.0  # give up waiting for the agent and speak anyway


class SimTransport:
    def __init__(
        self,
        script: list[ScriptLine],
        clock: Clock,
        fmt: AudioFormat = PCM16_16K,
        frame_ms: int = 20,
        trailing_silence_s: float = 0.8,
        max_tail_s: float = 60.0,
    ):
        self.script = script
        self.clock = clock
        self.fmt = fmt
        self.frame_ms = frame_ms
        self.trailing_silence_s = trailing_silence_s
        self.max_tail_s = max_tail_s
        self.closed = False
        self.cleared = 0
        self.sent_seconds = 0.0
        self.agent_turn_active = False
        self.agent_audio_in_turn = 0.0
        self.agent_turns_ended = 0
        self.events: list[tuple[float, str]] = []
        self._frame = frame_bytes(fmt, frame_ms)
        self._silence = silence(fmt, frame_ms / 1000.0)

    # --- Transport protocol ---
    def notify(self, event: str, **data: object) -> None:
        self.events.append((self.clock.now(), event))
        if event == "agent_turn_start":
            self.agent_turn_active = True
            self.agent_audio_in_turn = 0.0
        elif event == "agent_turn_end":
            self.agent_turn_active = False
            self.agent_turns_ended += 1

    async def send_audio(self, chunk: bytes) -> None:
        secs = self.fmt.seconds_for(len(chunk))
        self.sent_seconds += secs
        self.agent_audio_in_turn += secs

    async def clear(self) -> None:
        self.cleared += 1

    async def hangup(self) -> None:
        self.closed = True

    async def _tick(self) -> bytes:
        await self.clock.sleep(self.frame_ms / 1000.0)
        return self._silence

    async def frames(self) -> AsyncIterator[bytes]:
        for line in self.script:
            waited = 0.0
            target_turns = self.agent_turns_ended + 1
            # 1. wait for the right moment
            while not self.closed and waited < line.max_wait_s:
                if line.interrupt_after_s is not None:
                    if self.agent_turn_active and self.agent_audio_in_turn >= line.interrupt_after_s:
                        break
                    if self.agent_turns_ended >= target_turns:
                        break  # agent finished before we could interrupt; just talk
                elif self.agent_turns_ended >= target_turns:
                    break
                yield await self._tick()
                waited += self.frame_ms / 1000.0
            if self.closed:
                return
            # 2. think, then speak, then go quiet long enough for the endpointer
            pause = 0.0 if line.interrupt_after_s is not None else line.pause_before_s
            for _ in range(int(pause * 1000 / self.frame_ms)):
                yield await self._tick()
            self.events.append((self.clock.now(), f"caller_says:{line.say}"))
            secs = line.speak_seconds or max(0.6, len(line.say) / CHARS_PER_SECOND)
            audio = tone(self.fmt, secs, freq=180.0, amplitude=0.4)
            for i in range(0, len(audio) - self._frame + 1, self._frame):
                await self.clock.sleep(self.frame_ms / 1000.0)
                yield audio[i : i + self._frame]
            for _ in range(int(self.trailing_silence_s * 1000 / self.frame_ms)):
                yield await self._tick()
        # 3. stay on the line until the agent hangs up (or the cap)
        tail = 0.0
        while not self.closed and tail < self.max_tail_s:
            yield await self._tick()
            tail += self.frame_ms / 1000.0
        await asyncio.sleep(0)
