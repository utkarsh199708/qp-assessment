"""The call session: VAD -> STT -> (fast path | LLM) -> TTS -> transport, metered.

One :class:`CallSession` per call; all state is local to the session so a
single process can host many calls. Every cost lever is a flag on
:class:`SessionConfig` and every billable quantity goes through the meter, so
the ledger of a call shows exactly what each lever bought.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from .cache import TTSCache, cache_key
from .clock import Clock, RealClock
from .costs import CostMeter, PriceBook
from .fastpath import FastPath
from .providers.base import LLMProvider, Message, STTProvider, STTSession, TTSProvider
from .text import SentenceChunker
from .transports.base import Transport
from .vad import Endpointer, EndpointerConfig, EnergyVAD, FrameClassifier, VadEvent

log = logging.getLogger("voice_agent.pipeline")


@dataclass
class SessionConfig:
    system_prompt: str
    greeting: str | None = "Hi, thanks for calling. How can I help you today?"
    refusal_reply: str = "I'm not able to help with that. Is there anything else?"
    # --- billing context ---
    telephony_sku: str | None = None
    telephony_rates: tuple[str, ...] = ("inbound_per_min", "stream_per_min")
    compute_shares: tuple[tuple[str, int], ...] = ()  # (compute SKU, calls sharing one instance)
    # --- cost levers ---
    vad_gating: bool = True  # only speech (+pre/post roll) is sent to STT
    tts_cache: bool = True
    fastpath: bool = True
    barge_in: bool = True
    max_history_messages: int = 16  # context window lever
    trim_block: int = 6  # drop this many oldest messages at once (keeps cache prefix stable between trims)
    # --- behaviour ---
    idle_reprompt_s: float = 8.0
    idle_hangup_s: float = 25.0
    max_call_s: float = 900.0
    playback_lead_s: float = 0.3  # how far ahead of real time audio is pushed to the transport
    send_chunk_ms: int = 100
    endpoint: EndpointerConfig = field(default_factory=EndpointerConfig)


@dataclass
class TurnRecord:
    user_text: str | None
    agent_text: str
    source: str  # "greeting" | "fastpath:<intent>" | "llm" | "idle"
    latency_s: float | None  # endpoint -> first audio byte
    interrupted: bool = False


class CallSession:
    def __init__(
        self,
        *,
        transport: Transport,
        stt: STTProvider,
        llm: LLMProvider,
        tts: TTSProvider,
        book: PriceBook,
        config: SessionConfig,
        clock: Clock | None = None,
        cache: TTSCache | None = None,
        fastpath: FastPath | None = None,
        vad: FrameClassifier | None = None,
        call_id: str = "call",
    ):
        self.transport = transport
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.config = config
        self.clock = clock or RealClock()
        self.cache = cache if cache is not None else TTSCache()
        self.fastpath = fastpath or FastPath()
        self.meter = CostMeter(book, call_id)
        self.endpointer = Endpointer(vad or EnergyVAD(), config.endpoint)
        self.history: list[Message] = []
        self.turns: list[TurnRecord] = []
        self.end_reason: str | None = None

        self._stt_session: STTSession | None = None
        self._user_turns: asyncio.Queue[tuple[str, float | None]] = asyncio.Queue()
        self._pending_final: list[str] = []
        self._response_task: asyncio.Task[None] | None = None
        self._agent_speaking = False
        self._last_agent_sentences: list[str] = []
        self._current_sentence: str | None = None
        self._ended = asyncio.Event()
        self._start_t = 0.0
        self._last_user_activity = 0.0
        self._endpoint_at: float | None = None
        self._turn_first_audio_t: float | None = None
        self._turn_endpoint_at: float | None = None
        self._play_cursor = 0.0
        self._stt_seconds = 0.0
        self._audio_seconds = 0.0
        self._reprompted = False

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> CostMeter:
        self._start_t = self.clock.now()
        self._last_user_activity = self._start_t
        self._stt_session = await self.stt.start(self.transport.fmt)
        tasks = [
            asyncio.create_task(self._inbound(), name="inbound"),
            asyncio.create_task(self._stt_events(), name="stt-events"),
            asyncio.create_task(self._dispatch(), name="dispatch"),
            asyncio.create_task(self._idle_watch(), name="idle"),
        ]
        try:
            if self.config.greeting:
                self._start_response(None, canned=self.config.greeting, source="greeting")
            await self._ended.wait()
        finally:
            if self._response_task and not self._response_task.done():
                self._response_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._response_task
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            with contextlib.suppress(Exception):
                await self._stt_session.close()
            self._finalize_meter()
        return self.meter

    async def _end(self, reason: str) -> None:
        if self._ended.is_set():
            return
        self.end_reason = reason
        self._ended.set()
        with contextlib.suppress(Exception):
            await self.transport.hangup()

    def _finalize_meter(self) -> None:
        c = self.config
        m = self.meter
        m.duration_s = max(0.0, self.clock.now() - self._start_t)
        if c.telephony_sku:
            tel = m.book.get("telephony", c.telephony_sku)
            for rate in c.telephony_rates:
                if rate in tel.rates:
                    m.add("telephony", c.telephony_sku, rate, m.duration_s)
        for sku, concurrency in c.compute_shares:
            m.add("compute", sku, "per_hour", m.duration_s / max(1, concurrency))
        stt_sku = m.book.get("stt", self.stt.sku)
        stt_billed = m.duration_s if stt_sku.attrs.get("bills_session_time") else self._stt_seconds
        m.add("stt", self.stt.sku, "stream_per_min", stt_billed)
        m.counters["audio_seconds_in"] = round(self._audio_seconds, 2)
        m.counters["stt_seconds_sent"] = round(self._stt_seconds, 2)
        m.counters["stt_seconds_billed"] = round(stt_billed, 2)
        m.counters["tts_cache_hits"] = self.cache.hits
        m.counters["tts_cache_misses"] = self.cache.misses
        lat = [t.latency_s for t in self.turns if t.latency_s is not None]
        if lat:
            m.counters["response_latency_avg_s"] = round(sum(lat) / len(lat), 3)
            m.counters["response_latency_max_s"] = round(max(lat), 3)
        m.counters["turns"] = len(self.turns)
        m.counters["end_reason"] = self.end_reason or "unknown"

    # ------------------------------------------------------------------ inbound audio
    async def _inbound(self) -> None:
        assert self._stt_session is not None
        fmt = self.transport.fmt
        async for frame in self.transport.frames():
            self._audio_seconds += fmt.seconds_for(len(frame))
            forward, event = self.endpointer.push(frame)
            if event is VadEvent.SPEECH_START:
                self._last_user_activity = self.clock.now()
                self._reprompted = False
                self.meter.bump("user_speech_segments")
                if self._agent_speaking and self.config.barge_in:
                    await self._barge_in()
            to_send = forward if self.config.vad_gating else [frame]
            for f in to_send:
                self._stt_seconds += fmt.seconds_for(len(f))
                await self._stt_session.send_audio(f)
            if event is VadEvent.SPEECH_END:
                self._last_user_activity = self.clock.now()
                self._endpoint_at = self.clock.now()
                await self._stt_session.finalize()
                self._maybe_dispatch()
        await self._end("caller_hangup")

    async def _barge_in(self) -> None:
        self.meter.bump("barge_ins")
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
        self._play_cursor = self.clock.now()
        await self.transport.clear()

    # ------------------------------------------------------------------ transcripts
    async def _stt_events(self) -> None:
        assert self._stt_session is not None
        async for t in self._stt_session.events():
            if not t.is_final or not t.text.strip():
                continue
            self._pending_final.append(t.text.strip())
            self._maybe_dispatch()

    def _maybe_dispatch(self) -> None:
        if self._pending_final and not self.endpointer.in_speech:
            text = " ".join(self._pending_final)
            self._pending_final.clear()
            self._user_turns.put_nowait((text, self._endpoint_at))
            self._endpoint_at = None

    async def _dispatch(self) -> None:
        while True:
            text, endpoint_at = await self._user_turns.get()
            if self._response_task and not self._response_task.done():
                # user spoke while a response was in flight and barge-in is off: let it finish first
                await asyncio.wait({self._response_task})
            self._start_response(text, canned=None, source="llm", endpoint_at=endpoint_at)
            assert self._response_task is not None
            await asyncio.wait({self._response_task})

    # ------------------------------------------------------------------ responses
    def _start_response(
        self, user_text: str | None, *, canned: str | None, source: str, endpoint_at: float | None = None
    ) -> None:
        self._response_task = asyncio.create_task(
            self._respond(user_text, canned, source, endpoint_at), name="respond"
        )

    async def _respond(
        self, user_text: str | None, canned: str | None, source: str, endpoint_at: float | None
    ) -> None:
        c = self.config
        spoken: list[str] = []
        interrupted = False
        action: str | None = None
        latency: float | None = None
        self._turn_first_audio_t = None
        self._turn_endpoint_at = endpoint_at
        if user_text is not None:
            self.history.append(Message("user", user_text))
            self._trim_history()
            self.meter.bump("user_turns")
        try:
            self._agent_speaking = True
            self.transport.notify("agent_turn_start")
            if canned is not None:
                await self._say(canned, spoken)
            else:
                assert user_text is not None
                fp = self.fastpath.match(user_text, self._last_agent_sentences) if c.fastpath else None
                if fp is not None:
                    source = f"fastpath:{fp.intent}"
                    self.meter.bump("fastpath_hits")
                    action = fp.action
                    for sentence in fp.reply:
                        await self._say(sentence, spoken)
                else:
                    await self._llm_turn(spoken)
            latency = self._turn_latency()
        except asyncio.CancelledError:
            interrupted = True
            latency = self._turn_latency()
            if self._current_sentence:  # cut mid-sentence: the caller heard part of it
                spoken.append(self._current_sentence)
            raise
        finally:
            self._agent_speaking = False
            self._current_sentence = None
            full = " ".join(spoken)
            if full:
                self.history.append(Message("assistant", full + (" [interrupted]" if interrupted else "")))
                if not interrupted:
                    self._last_agent_sentences = list(spoken)
            self.turns.append(TurnRecord(user_text, full, source, latency, interrupted))
            self._last_user_activity = self.clock.now()
            self.transport.notify("agent_turn_end")
        if action == "hangup":
            await self._end("agent_hangup")

    def _turn_latency(self) -> float | None:
        if self._turn_endpoint_at is None or self._turn_first_audio_t is None:
            return None
        return max(0.0, self._turn_first_audio_t - self._turn_endpoint_at)

    async def _llm_turn(self, spoken: list[str]) -> None:
        stream = self.llm.stream(self.config.system_prompt, list(self.history))
        chunker = SentenceChunker()
        try:
            async for delta in stream:
                for sentence in chunker.push(delta):
                    await self._say(sentence, spoken)
            tail = chunker.flush()
            if tail:
                await self._say(tail, spoken)
        finally:
            await stream.aclose()
            if stream.usage is not None:
                self.meter.add_llm(self.llm.sku, stream.usage.split, stream.usage.output_tokens)
        if stream.stop_reason == "refusal":
            await self._say(self.config.refusal_reply, spoken)

    async def _say(self, text: str, spoken: list[str]) -> None:
        fmt = self.transport.fmt
        self._current_sentence = text
        key = cache_key(self.tts.sku, self.tts.voice, fmt, text)
        audio = self.cache.get(key) if self.config.tts_cache else None
        if audio is None:
            buf = bytearray()
            async for chunk in self.tts.synthesize(text, fmt):
                buf += chunk
                await self._send_paced(chunk)
            self.meter.add("tts", self.tts.sku, "per_1m_chars", len(text))
            self.meter.bump("tts_chars_billed", len(text))
            if self.config.tts_cache:
                self.cache.put(key, bytes(buf))
        else:
            await self._send_paced(audio)
        spoken.append(text)
        self._current_sentence = None

    async def _send_paced(self, audio: bytes) -> None:
        """Push audio slightly ahead of real time so 'sent' tracks 'heard' closely.

        Keeping the transport buffer small is what makes barge-in feel instant:
        ``clear()`` only has to drop ``playback_lead_s`` of audio.
        """
        fmt = self.transport.fmt
        step = fmt.bytes_for(self.config.send_chunk_ms / 1000.0)
        for i in range(0, len(audio), step):
            chunk = audio[i : i + step]
            if self._turn_first_audio_t is None:
                self._turn_first_audio_t = self.clock.now()
            await self.transport.send_audio(chunk)
            dur = fmt.seconds_for(len(chunk))
            now = self.clock.now()
            self._play_cursor = max(self._play_cursor, now) + dur
            ahead = self._play_cursor - now
            if ahead > self.config.playback_lead_s:
                await self.clock.sleep(ahead - self.config.playback_lead_s)
        # wait for the tail to play out before declaring the turn over
        remaining = self._play_cursor - self.clock.now()
        if remaining > 0:
            await self.clock.sleep(remaining)

    def _trim_history(self) -> None:
        c = self.config
        if len(self.history) > c.max_history_messages:
            drop = max(2, c.trim_block - (c.trim_block % 2))
            del self.history[:drop]
            self.meter.bump("history_trims")

    # ------------------------------------------------------------------ idle handling
    async def _idle_watch(self) -> None:
        c = self.config
        while not self._ended.is_set():
            await self.clock.sleep(0.5)
            now = self.clock.now()
            if now - self._start_t > c.max_call_s:
                await self._end("max_duration")
                return
            responding = self._response_task is not None and not self._response_task.done()
            if responding or self.endpointer.in_speech:
                continue
            idle = now - self._last_user_activity
            if idle > c.idle_hangup_s:
                await self._end("idle_timeout")
                return
            if idle > c.idle_reprompt_s and not self._reprompted:
                self._reprompted = True
                self.meter.bump("idle_reprompts")
                self._start_response(None, canned=self.fastpath.silence_reply, source="idle")
