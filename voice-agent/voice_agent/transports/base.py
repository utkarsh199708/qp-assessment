from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ..audio import AudioFormat


@runtime_checkable
class Transport(Protocol):
    """Inbound frames are PCM16 at ``fmt``; outbound chunks are PCM16 at ``fmt``.

    Encoding/decoding to the wire format (e.g. mu-law for Twilio) is the
    transport's job so the pipeline stays format-agnostic.
    """

    fmt: AudioFormat

    def frames(self) -> AsyncIterator[bytes]:
        """Inbound 20 ms frames until the far end hangs up."""
        ...

    async def send_audio(self, chunk: bytes) -> None: ...

    async def clear(self) -> None:
        """Drop any audio still queued for playback (barge-in)."""
        ...

    async def hangup(self) -> None: ...

    def notify(self, event: str, **data: object) -> None:
        """Session lifecycle hints (``agent_turn_start``/``agent_turn_end``). Optional."""
        ...
