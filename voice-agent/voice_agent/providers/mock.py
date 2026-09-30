"""Offline providers: free, deterministic, and fast. Used by tests and ``simulate``.

They bill through the same meter as real providers so a simulated call produces
a realistic ledger for whichever SKUs they impersonate.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

from ..audio import AudioFormat, rms_pcm16, tone
from ..clock import Clock, RealClock
from ..costs import CacheSplit, estimate_tokens, split_prompt_cache
from .base import LLMUsage, Message, Transcript

CHARS_PER_SECOND = 850 / 60.0  # ~150 wpm at ~5.7 chars/word incl. spaces


class ScriptedSTTSession:
    """Emits the next scripted utterance whenever it has heard speech and is finalised.

    It looks at audio energy so VAD gating and pre/post-roll are exercised the
    same way a real provider would see them.
    """

    def __init__(
        self,
        utterances: list[str],
        speech_rms: float = 0.005,
        clock: Clock | None = None,
        final_delay_s: float = 0.15,
    ):
        self._utterances = utterances  # shared with the provider so several sessions consume one script
        self._clock = clock or RealClock()
        self._final_delay_s = final_delay_s
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._speech_bytes = 0
        self._speech_rms = speech_rms
        self._interim_sent = False
        self.audio_bytes_received = 0
        self.closed = False

    async def send_audio(self, frame: bytes) -> None:
        self.audio_bytes_received += len(frame)
        if rms_pcm16(frame) > self._speech_rms:
            self._speech_bytes += len(frame)
            if not self._interim_sent and self._utterances:
                self._interim_sent = True
                first = self._utterances[0].split(" ")[0]
                await self._queue.put(Transcript(text=first, is_final=False))

    async def finalize(self) -> None:
        if self._speech_bytes > 0 and self._utterances:
            text = self._utterances.pop(0)
            asyncio.create_task(self._emit_final(text))
        self._speech_bytes = 0
        self._interim_sent = False

    async def _emit_final(self, text: str) -> None:
        await self._clock.sleep(self._final_delay_s)  # provider finalisation latency
        await self._queue.put(Transcript(text=text, is_final=True, confidence=0.95))

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[Transcript]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class ScriptedSTT:
    def __init__(
        self,
        utterances: list[str],
        sku: str = "deepgram/nova-3",
        clock: Clock | None = None,
        final_delay_s: float = 0.15,
    ):
        self.sku = sku
        self.utterances = list(utterances)
        self.clock = clock
        self.final_delay_s = final_delay_s
        self.sessions: list[ScriptedSTTSession] = []

    async def start(self, fmt: AudioFormat) -> ScriptedSTTSession:
        s = ScriptedSTTSession(self.utterances, clock=self.clock, final_delay_s=self.final_delay_s)
        self.sessions.append(s)
        return s


class ToneTTS:
    """Synthesises a tone whose length matches the spoken duration of the text."""

    def __init__(
        self,
        sku: str = "deepgram/aura-2",
        voice: str = "mock",
        chunk_ms: int = 100,
        clock: Clock | None = None,
        ttfb_s: float = 0.12,
    ):
        self.sku = sku
        self.voice = voice
        self.chunk_ms = chunk_ms
        self.clock = clock or RealClock()
        self.ttfb_s = ttfb_s
        self.requests: list[str] = []

    async def synthesize(self, text: str, fmt: AudioFormat) -> AsyncIterator[bytes]:
        self.requests.append(text)
        await self.clock.sleep(self.ttfb_s)  # time to first byte
        seconds = max(0.2, len(text) / CHARS_PER_SECOND)
        audio = tone(fmt, seconds)
        step = fmt.bytes_for(self.chunk_ms / 1000.0)
        for i in range(0, len(audio), step):
            yield audio[i : i + step]
            await asyncio.sleep(0)


class _RuleStream:
    def __init__(self, text: str, usage: LLMUsage, clock: Clock, ttft_s: float, tokens_per_s: float):
        self._text = text
        self._clock = clock
        self._ttft_s = ttft_s
        self._tokens_per_s = tokens_per_s
        self.usage: LLMUsage | None = None
        self._pending_usage = usage
        self.stop_reason: str | None = None
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[str]:
        # stream word by word so the sentence chunker is exercised
        await self._clock.sleep(self._ttft_s)  # time to first token
        words = self._text.split(" ")
        for i, w in enumerate(words):
            yield w if i == len(words) - 1 else w + " "
            await self._clock.sleep(1.3 / self._tokens_per_s)  # ~1.3 tokens per word
        self.usage = self._pending_usage
        self.stop_reason = "end_turn"

    async def aclose(self) -> None:
        self.closed = True
        if self.usage is None:
            # cancelled mid-stream: the provider still bills what it generated so far
            self.usage = self._pending_usage
            self.stop_reason = "cancelled"


class RuleLLM:
    """A tiny appointment-booking brain with token accounting that mimics Claude billing.

    ``cache_min_tokens`` and ``prompt_caching`` reproduce the prompt-cache
    rules so the ledger shows where caching does and does not pay.
    """

    def __init__(
        self,
        sku: str = "claude-haiku-4-5",
        *,
        prompt_caching: bool = True,
        cache_min_tokens: int = 4096,
        system_padding_tokens: int = 0,
        clock: Clock | None = None,
        ttft_s: float = 0.35,
        tokens_per_s: float = 120.0,
    ):
        self.sku = sku
        self.prompt_caching = prompt_caching
        self.cache_min_tokens = cache_min_tokens
        self.system_padding_tokens = system_padding_tokens
        self.clock = clock or RealClock()
        self.ttft_s = ttft_s
        self.tokens_per_s = tokens_per_s
        self._cached_prefix = 0
        self.calls = 0

    def _reply(self, history: list[Message]) -> str:
        last = history[-1].content.lower() if history else ""
        said = [m.content.lower() for m in history if m.role == "user"]
        if re.search(r"\b(book|appointment|schedule|reserve)\b", last):
            return "Sure, I can help you book an appointment. What day works best for you?"
        if re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow)\b", last):
            day = re.search(
                r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow)\b", last
            ).group(1)
            return f"Great, {day} it is. Would you prefer the morning or the afternoon?"
        if re.search(r"\b(morning|afternoon|evening)\b", last):
            slot = re.search(r"\b(morning|afternoon|evening)\b", last).group(1)
            return f"Done. You're booked for the {slot}. Is there anything else I can help with?"
        if re.search(r"\b(hours|open|close)\b", last):
            return "We're open nine to six on weekdays and ten to four on Saturdays. Anything else?"
        if any("book" in s for s in said):
            return "Got it. Anything else I can do for you today?"
        return "I can help with bookings and opening hours. What would you like to do?"

    def stream(self, system: str, history: list[Message]) -> _RuleStream:
        self.calls += 1
        text = self._reply(history)
        system_tokens = estimate_tokens(system) + self.system_padding_tokens
        history_tokens = sum(estimate_tokens(m.content) for m in history)
        prefix_total = system_tokens + history_tokens
        split: CacheSplit = split_prompt_cache(
            prefix_total, self._cached_prefix, enabled=self.prompt_caching, min_tokens=self.cache_min_tokens
        )
        if self.prompt_caching and prefix_total >= self.cache_min_tokens:
            self._cached_prefix = prefix_total
        return _RuleStream(
            text,
            LLMUsage(split=split, output_tokens=estimate_tokens(text)),
            self.clock,
            self.ttft_s,
            self.tokens_per_s,
        )
