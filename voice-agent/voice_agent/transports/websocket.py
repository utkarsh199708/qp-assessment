"""Generic WebSocket transport: binary PCM16 frames in and out, JSON control messages.

Client contract (browser, native app, load generator):
- send binary messages of PCM16 mono at ``fmt.sample_rate`` (any chunk size);
- receive binary PCM16 chunks to play, ``{"type": "clear"}`` to drop queued
  audio (barge-in) and ``{"type": "hangup"}`` before the socket closes;
- optionally send ``{"type": "hangup"}``.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from ..audio import PCM16_16K, AudioFormat, frame_bytes


class WebSocketPCMTransport:
    def __init__(self, ws: Any, fmt: AudioFormat = PCM16_16K):
        self.ws = ws
        self.fmt = fmt
        self._frame = frame_bytes(fmt)
        self._buf = bytearray()
        self.closed = False

    async def frames(self) -> AsyncIterator[bytes]:
        async for msg in self.ws:
            if isinstance(msg, bytes | bytearray):
                self._buf += msg
                while len(self._buf) >= self._frame:
                    yield bytes(self._buf[: self._frame])
                    del self._buf[: self._frame]
            else:
                try:
                    ctl = json.loads(msg)
                except ValueError:
                    continue
                if ctl.get("type") == "hangup":
                    return

    async def send_audio(self, chunk: bytes) -> None:
        if not self.closed:
            await self.ws.send(chunk)

    async def clear(self) -> None:
        if not self.closed:
            await self.ws.send(json.dumps({"type": "clear"}))

    async def hangup(self) -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            await self.ws.send(json.dumps({"type": "hangup"}))
        with contextlib.suppress(Exception):
            await self.ws.close()

    def notify(self, event: str, **data: object) -> None:
        return None
