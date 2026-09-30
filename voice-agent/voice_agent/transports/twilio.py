"""Twilio Programmable Voice Media Streams transport.

TwiML: ``<Connect><Stream url="wss://your-host/twilio"/></Connect>``. Twilio then
sends JSON events over the WebSocket: ``connected``, ``start`` (carries the
``streamSid``), ``media`` (base64 mu-law 8 kHz, 20 ms per message), ``mark``
and ``stop``. We reply with ``media`` (base64 mu-law), ``clear`` (drop queued
audio - the barge-in primitive) and ``mark``.

The pipeline works in PCM16 at 8 kHz on this transport; mu-law <-> PCM16 is a
table lookup per sample. Closing the socket ends the <Stream>; Twilio continues
with the next TwiML verb (or hangs up if there is none).
"""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from ..audio import PCM16_8K, frame_bytes, mulaw_to_pcm16, pcm16_to_mulaw


def parse_event(raw: str | bytes) -> dict[str, Any]:
    return json.loads(raw)


def media_message(stream_sid: str, pcm16_chunk: bytes) -> str:
    payload = base64.b64encode(pcm16_to_mulaw(pcm16_chunk)).decode("ascii")
    return json.dumps({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})


def clear_message(stream_sid: str) -> str:
    return json.dumps({"event": "clear", "streamSid": stream_sid})


def mark_message(stream_sid: str, name: str) -> str:
    return json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": name}})


class TwilioMediaStreamTransport:
    fmt = PCM16_8K

    def __init__(self, ws: Any):
        self.ws = ws
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self.closed = False
        self._frame = frame_bytes(self.fmt)
        self._buf = bytearray()

    async def frames(self) -> AsyncIterator[bytes]:
        async for raw in self.ws:
            msg = parse_event(raw)
            event = msg.get("event")
            if event == "start":
                self.stream_sid = msg["start"]["streamSid"]
                self.call_sid = msg["start"].get("callSid")
            elif event == "media":
                self._buf += mulaw_to_pcm16(base64.b64decode(msg["media"]["payload"]))
                while len(self._buf) >= self._frame:
                    yield bytes(self._buf[: self._frame])
                    del self._buf[: self._frame]
            elif event == "stop":
                return

    async def send_audio(self, chunk: bytes) -> None:
        if self.stream_sid and not self.closed:
            await self.ws.send(media_message(self.stream_sid, chunk))

    async def clear(self) -> None:
        if self.stream_sid and not self.closed:
            await self.ws.send(clear_message(self.stream_sid))

    async def hangup(self) -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            await self.ws.close()

    def notify(self, event: str, **data: object) -> None:
        return None
