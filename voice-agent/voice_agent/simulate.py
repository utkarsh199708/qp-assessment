"""Run a scripted call end-to-end with mock providers and return its ledger.

Used by the CLI (``voice-agent simulate``) and the tests. The point is not the
conversation - it is that every lever's effect shows up as dollars in the
ledger for whichever provider SKUs the mocks impersonate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from .clock import SimClock
from .costs import CostMeter, PriceBook
from .estimate import Profile
from .fastpath import FastPath
from .pipeline import CallSession, SessionConfig
from .profiles import get_profile
from .providers.mock import RuleLLM, ScriptedSTT, ToneTTS
from .transports.sim import ScriptLine, SimTransport

SYSTEM_PROMPT = (
    "You are the phone assistant for Northside Dental. Speak in short, natural sentences: at most two "
    "sentences per reply unless asked for detail. Never read out URLs or lists. Confirm bookings by "
    "repeating the day and time. If the caller says goodbye, end the call politely."
)

DEFAULT_SCRIPT: list[ScriptLine] = [
    ScriptLine("Hi, I'd like to book an appointment."),
    ScriptLine("Does Thursday work?"),
    ScriptLine("Morning please."),
    ScriptLine("Sorry, say that again?"),
    ScriptLine("Hold on one second."),
    ScriptLine("What are your opening hours?"),
    ScriptLine("Actually never mind, thanks.", interrupt_after_s=1.0),
    ScriptLine("Thanks, that's all. Bye!"),
]


@dataclass(frozen=True)
class Levers:
    vad_gating: bool = True
    tts_cache: bool = True
    fastpath: bool = True
    barge_in: bool = True
    prompt_caching: bool = True
    system_padding_tokens: int = (
        0  # extra tokens in the system prompt (e.g. a FAQ) - relevant to cache minimums
    )

    @classmethod
    def all_off(cls) -> Levers:
        return cls(vad_gating=False, tts_cache=False, fastpath=False, barge_in=False, prompt_caching=False)


@dataclass
class SimResult:
    meter: CostMeter
    session: CallSession
    transport: SimTransport


async def run_call(
    book: PriceBook,
    profile: Profile | str = "budget-hosted",
    script: list[ScriptLine] | None = None,
    levers: Levers | None = None,
    *,
    tts_cache=None,
    call_id: str = "sim",
) -> SimResult:
    profile = get_profile(profile) if isinstance(profile, str) else profile
    levers = levers or Levers()
    script = script or DEFAULT_SCRIPT
    if profile.llm is None or profile.stt is None or profile.tts is None:
        raise ValueError(f"profile {profile.name} is not a cascaded pipeline; simulate a cascade profile")
    llm_sku = book.get("llm", profile.llm)

    async with SimClock() as clock:
        transport = SimTransport(script, clock)
        stt = ScriptedSTT([line.say for line in script], sku=profile.stt, clock=clock)
        llm = RuleLLM(
            sku=profile.llm,
            prompt_caching=levers.prompt_caching,
            cache_min_tokens=int(llm_sku.attrs.get("cache_min_tokens", 0)),
            system_padding_tokens=levers.system_padding_tokens,
            clock=clock,
        )
        tts = ToneTTS(sku=profile.tts, clock=clock)
        config = SessionConfig(
            system_prompt=SYSTEM_PROMPT,
            telephony_sku=profile.telephony,
            telephony_rates=profile.telephony_rates,
            compute_shares=profile.compute,
            vad_gating=levers.vad_gating,
            tts_cache=levers.tts_cache,
            fastpath=levers.fastpath,
            barge_in=levers.barge_in,
        )
        session = CallSession(
            transport=transport,
            stt=stt,
            llm=llm,
            tts=tts,
            book=book,
            config=config,
            clock=clock,
            cache=tts_cache,
            fastpath=FastPath(),
            call_id=call_id,
        )
        meter = await asyncio.wait_for(session.run(), timeout=60)
    return SimResult(meter, session, transport)


async def compare_levers(book: PriceBook, profile: Profile | str = "budget-hosted") -> dict[str, CostMeter]:
    """Run the default script with all levers off, then switch each on cumulatively."""
    out: dict[str, CostMeter] = {}
    off = Levers.all_off()
    steps = [
        ("all levers off", off),
        ("+ VAD gating of STT", replace(off, vad_gating=True)),
        ("+ fast-path intents", replace(off, vad_gating=True, fastpath=True)),
        ("+ TTS cache", replace(off, vad_gating=True, fastpath=True, tts_cache=True)),
        ("+ barge-in (all on)", replace(off, vad_gating=True, fastpath=True, tts_cache=True, barge_in=True)),
        ("+ prompt caching (short prompt)", Levers()),
        (
            "4.5k-token FAQ prompt, no cache",
            replace(Levers(), prompt_caching=False, system_padding_tokens=4500),
        ),
        ("4.5k-token FAQ prompt, cached", replace(Levers(), system_padding_tokens=4500)),
    ]
    for label, levers in steps:
        out[label] = (await run_call(book, profile, levers=levers, call_id=label)).meter
    return out
