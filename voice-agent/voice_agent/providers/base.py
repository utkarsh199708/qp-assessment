"""Provider protocols. The pipeline depends only on these.

Design rules that keep the pipeline cheap and swappable:
- STT is a *session* fed 20 ms frames; the pipeline decides which frames to
  send (VAD gating) and calls ``finalize()`` at the endpoint so the provider
  flushes without waiting for its own silence timer.
- TTS yields audio chunks as they are produced so playback starts before the
  sentence is fully synthesised.
- LLM replies stream text deltas; the pipeline cuts them into sentences and
  hands each to TTS immediately. Usage is reported after the stream ends so the
  meter bills exactly what the provider billed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..audio import AudioFormat
from ..costs import CacheSplit


@dataclass
class Transcript:
    text: str
    is_final: bool
    confidence: float | None = None


@runtime_checkable
class STTSession(Protocol):
    async def send_audio(self, frame: bytes) -> None: ...

    async def finalize(self) -> None:
        """The endpointer says the user stopped: flush pending audio into a final result."""
        ...

    async def close(self) -> None: ...

    def events(self) -> AsyncIterator[Transcript]: ...


@runtime_checkable
class STTProvider(Protocol):
    sku: str  # price-book key, e.g. "deepgram/nova-3"

    async def start(self, fmt: AudioFormat) -> STTSession: ...


@runtime_checkable
class TTSProvider(Protocol):
    sku: str  # price-book key, e.g. "deepgram/aura-2"
    voice: str

    def synthesize(self, text: str, fmt: AudioFormat) -> AsyncIterator[bytes]: ...


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class LLMUsage:
    split: CacheSplit
    output_tokens: int


@runtime_checkable
class LLMStream(Protocol):
    """One reply. Iterate for text deltas; read ``usage``/``stop_reason`` afterwards."""

    usage: LLMUsage | None
    stop_reason: str | None

    def __aiter__(self) -> AsyncIterator[str]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class LLMProvider(Protocol):
    sku: str  # price-book key, e.g. "claude-haiku-4-5"

    def stream(self, system: str, history: list[Message]) -> LLMStream: ...
