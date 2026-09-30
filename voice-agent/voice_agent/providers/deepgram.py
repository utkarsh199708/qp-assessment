"""Deepgram Nova-3 streaming STT (WebSocket) and Aura-2 TTS (streaming REST).

Written against Deepgram's documented API (listen v1 WebSocket, speak v1 REST);
these adapters are not exercised by the offline test-suite. Cost-relevant
behaviour:
- Deepgram bills the audio you send. With VAD gating the session goes quiet
  between utterances, so the session sends ``KeepAlive`` messages (free) to stop
  Deepgram closing the socket after 10 s of silence.
- ``Finalize`` flushes a result immediately at the VAD endpoint instead of
  waiting for Deepgram's own endpointing timer.
- TTS is requested in the transport's native format (e.g. mu-law 8 kHz for
  Twilio) so no transcoding happens in the pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import httpx
import websockets

from ..audio import AudioFormat
from .base import Transcript

LISTEN_URL = "wss://api.deepgram.com/v1/listen"
SPEAK_URL = "https://api.deepgram.com/v1/speak"


def _api_key(explicit: str | None) -> str:
    key = explicit or os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY is not set")
    return key


class DeepgramSTTSession:
    def __init__(self, ws: websockets.asyncio.client.ClientConnection, keepalive_s: float):
        self._ws = ws
        self._keepalive_s = keepalive_s
        self._last_send = asyncio.get_running_loop().time()
        self._keepalive_task = asyncio.create_task(self._keepalive())
        self._closed = False

    async def send_audio(self, frame: bytes) -> None:
        self._last_send = asyncio.get_running_loop().time()
        await self._ws.send(frame)

    async def finalize(self) -> None:
        await self._ws.send(json.dumps({"type": "Finalize"}))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._keepalive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._keepalive_task
        with contextlib.suppress(Exception):
            await self._ws.send(json.dumps({"type": "CloseStream"}))
        with contextlib.suppress(Exception):
            await self._ws.close()

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(self._keepalive_s)
            if asyncio.get_running_loop().time() - self._last_send >= self._keepalive_s:
                with contextlib.suppress(Exception):
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))

    async def events(self) -> AsyncIterator[Transcript]:
        async for raw in self._ws:
            if isinstance(raw, bytes):
                continue
            t = parse_listen_message(raw)
            if t is not None:
                yield t


def parse_listen_message(raw: str) -> Transcript | None:
    msg = json.loads(raw)
    if msg.get("type") != "Results":
        return None
    try:
        alt = msg["channel"]["alternatives"][0]
    except (KeyError, IndexError):
        return None
    text = (alt.get("transcript") or "").strip()
    if not text:
        return None
    return Transcript(text=text, is_final=bool(msg.get("is_final")), confidence=alt.get("confidence"))


class DeepgramSTT:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "nova-3",
        language: str = "en",
        endpointing_ms: int = 300,
        keepalive_s: float = 5.0,
        url: str = LISTEN_URL,
    ):
        self.sku = f"deepgram/{model}"
        self.api_key = api_key
        self.model = model
        self.language = language
        self.endpointing_ms = endpointing_ms
        self.keepalive_s = keepalive_s
        self.url = url

    def listen_params(self, fmt: AudioFormat) -> dict[str, str]:
        return {
            "model": self.model,
            "language": self.language,
            "encoding": "mulaw" if fmt.encoding == "mulaw" else "linear16",
            "sample_rate": str(fmt.sample_rate),
            "channels": str(fmt.channels),
            "interim_results": "true",
            "endpointing": str(self.endpointing_ms),
            "smart_format": "true",
            "punctuate": "true",
        }

    async def start(self, fmt: AudioFormat) -> DeepgramSTTSession:
        uri = f"{self.url}?{urlencode(self.listen_params(fmt))}"
        ws = await websockets.connect(
            uri, additional_headers={"Authorization": f"Token {_api_key(self.api_key)}"}
        )
        return DeepgramSTTSession(ws, self.keepalive_s)


class DeepgramTTS:
    def __init__(
        self,
        api_key: str | None = None,
        voice: str = "aura-2-thalia-en",
        url: str = SPEAK_URL,
        client: httpx.AsyncClient | None = None,
    ):
        self.sku = "deepgram/aura-2"
        self.voice = voice
        self.url = url
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))

    def speak_params(self, fmt: AudioFormat) -> dict[str, str]:
        return {
            "model": self.voice,
            "encoding": "mulaw" if fmt.encoding == "mulaw" else "linear16",
            "sample_rate": str(fmt.sample_rate),
            "container": "none",
        }

    async def synthesize(self, text: str, fmt: AudioFormat) -> AsyncIterator[bytes]:
        headers = {"Authorization": f"Token {_api_key(self.api_key)}", "Content-Type": "application/json"}
        async with self._client.stream(
            "POST", self.url, params=self.speak_params(fmt), headers=headers, json={"text": text}
        ) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                if chunk:
                    yield chunk

    async def aclose(self) -> None:
        await self._client.aclose()
