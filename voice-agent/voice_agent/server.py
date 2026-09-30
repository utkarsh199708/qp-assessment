"""WebSocket server hosting one :class:`CallSession` per connection.

Routes: ``/twilio`` for Twilio Media Streams, anything else for the generic
PCM16 WebSocket client. One process serves many calls; a shared TTS cache means
the greeting and the fast-path phrases are synthesised once per process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection

from .cache import TTSCache
from .costs import PriceBook
from .pipeline import CallSession, SessionConfig
from .profiles import get_profile
from .providers.base import LLMProvider, STTProvider, TTSProvider
from .simulate import SYSTEM_PROMPT
from .transports.twilio import TwilioMediaStreamTransport
from .transports.websocket import WebSocketPCMTransport

log = logging.getLogger("voice_agent.server")

ProviderFactory = Callable[[], tuple[STTProvider, LLMProvider, TTSProvider]]


@dataclass
class ServerSettings:
    system_prompt: str = SYSTEM_PROMPT
    greeting: str = "Hi, thanks for calling. How can I help you today?"
    llm_model: str = "claude-haiku-4-5"
    tts_voice: str = "aura-2-thalia-en"
    stt_model: str = "nova-3"

    @classmethod
    def from_env(cls) -> ServerSettings:
        s = cls()
        if p := os.environ.get("VA_SYSTEM_PROMPT_FILE"):
            s.system_prompt = Path(p).read_text()
        s.greeting = os.environ.get("VA_GREETING", s.greeting)
        s.llm_model = os.environ.get("VA_LLM_MODEL", s.llm_model)
        s.tts_voice = os.environ.get("VA_TTS_VOICE", s.tts_voice)
        s.stt_model = os.environ.get("VA_STT_MODEL", s.stt_model)
        return s


def real_factory(settings: ServerSettings) -> ProviderFactory:
    from .providers.anthropic_llm import ClaudeLLM
    from .providers.deepgram import DeepgramSTT, DeepgramTTS

    stt = DeepgramSTT(model=settings.stt_model)
    llm = ClaudeLLM(model=settings.llm_model)
    tts = DeepgramTTS(voice=settings.tts_voice)
    return lambda: (stt, llm, tts)


def mock_factory(settings: ServerSettings) -> ProviderFactory:
    from .providers.mock import RuleLLM, ScriptedSTT, ToneTTS

    phrases = [
        "I'd like to book an appointment",
        "Thursday",
        "morning please",
        "what are your hours",
        "thanks bye",
    ]

    def make() -> tuple[STTProvider, LLMProvider, TTSProvider]:
        return ScriptedSTT(phrases * 20), RuleLLM(sku=settings.llm_model), ToneTTS()

    return make


async def serve(args: Any) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    book = PriceBook.load(args.pricebook)
    profile = get_profile(args.profile)
    settings = ServerSettings.from_env()
    factory = mock_factory(settings) if args.mock else real_factory(settings)
    cache = TTSCache()
    ledger_dir = Path(args.ledger_dir)
    ledger_dir.mkdir(parents=True, exist_ok=True)

    async def handler(ws: ServerConnection) -> None:
        path = ws.request.path if ws.request else "/"
        call_id = uuid.uuid4().hex[:12]
        transport: Any = (
            TwilioMediaStreamTransport(ws) if path.startswith("/twilio") else WebSocketPCMTransport(ws)
        )
        stt, llm, tts = factory()
        config = SessionConfig(
            system_prompt=settings.system_prompt,
            greeting=settings.greeting,
            telephony_sku=profile.telephony if path.startswith("/twilio") else "webrtc/self-hosted",
            compute_shares=profile.compute,
        )
        session = CallSession(
            transport=transport,
            stt=stt,
            llm=llm,
            tts=tts,
            book=book,
            config=config,
            cache=cache,
            call_id=call_id,
        )
        log.info("call %s started on %s", call_id, path)
        try:
            meter = await session.run()
        except Exception:
            log.exception("call %s crashed", call_id)
            return
        ledger = meter.to_dict()
        ledger["turns"] = [t.__dict__ for t in session.turns]
        (ledger_dir / f"{call_id}.json").write_text(json.dumps(ledger, indent=2))
        log.info("call %s ended (%s): %s", call_id, session.end_reason, meter.format().splitlines()[0])

    async with websockets.serve(handler, args.host, args.port, max_size=2**20):
        log.info(
            "listening on ws://%s:%d  (/twilio for Media Streams, / for PCM16 clients)", args.host, args.port
        )
        await asyncio.Future()
