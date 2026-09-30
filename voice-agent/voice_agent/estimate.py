"""Analytical cost model: what a call costs under a given profile and assumptions.

This is the same arithmetic the runtime meter performs, applied to averages,
so a budget can be produced before a single call is placed and compared with
measured ledgers afterwards.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from .costs import CostMeter, PriceBook, split_prompt_cache


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    telephony: str | None = None
    telephony_rates: tuple[str, ...] = (
        "inbound_per_min",
        "stream_per_min",
    )  # rates charged per call minute, if the SKU has them
    stt: str | None = None
    tts: str | None = None
    llm: str | None = None
    s2s: str | None = None
    platform: str | None = None
    compute: tuple[tuple[str, int], ...] = ()  # (compute SKU, simultaneous calls one instance sustains)


@dataclass(frozen=True)
class Assumptions:
    call_minutes: float = 3.0
    turns_per_minute: float = 4.0  # user turns
    user_speech_fraction: float = 0.40  # share of call time the caller is talking
    agent_speech_fraction: float = 0.40  # share of call time the agent is talking
    chars_per_spoken_minute: int = 850
    system_prompt_tokens: int = 1500
    user_turn_tokens: int = 35
    assistant_turn_tokens: int = 60
    stt_roll_overhead: float = 0.12  # pre/post-roll audio sent around each speech segment
    tts_cache_hit_rate: float = 0.15
    fastpath_rate: float = 0.10  # user turns answered without the LLM
    vad_gating: bool = True
    prompt_caching: bool = True
    idle_fraction_billed_by_s2s: float = 1.0  # speech-to-speech APIs bill all inbound audio
    capacity_utilization: float = (
        0.35  # average share of provisioned compute that is actually busy (peaks, idle hours)
    )

    def with_levers(self, **kw: object) -> Assumptions:
        return replace(self, **kw)  # type: ignore[arg-type]


@dataclass
class Estimate:
    profile: Profile
    assumptions: Assumptions
    meter: CostMeter
    notes: list[str] = field(default_factory=list)

    @property
    def usd_per_minute(self) -> float:
        return self.meter.usd_per_minute()

    @property
    def usd_per_call(self) -> float:
        return self.meter.total()


def estimate_call(book: PriceBook, profile: Profile, a: Assumptions | None = None) -> Estimate:
    a = a or Assumptions()
    m = CostMeter(book, call_id=f"estimate:{profile.name}")
    notes: list[str] = []
    secs = a.call_minutes * 60.0
    m.duration_s = secs
    user_secs = secs * a.user_speech_fraction
    agent_secs = secs * a.agent_speech_fraction
    turns = max(1, int(round(a.call_minutes * a.turns_per_minute)))

    if profile.telephony:
        tel = book.get("telephony", profile.telephony)
        for rate in profile.telephony_rates:
            if rate in tel.rates:
                m.add("telephony", profile.telephony, rate, secs)

    if profile.platform:
        m.add("platform", profile.platform, "per_min", secs)

    if profile.s2s:
        sku = book.get("s2s", profile.s2s)
        m.add("s2s", profile.s2s, "audio_in_per_min", secs * a.idle_fraction_billed_by_s2s)
        m.add("s2s", profile.s2s, "audio_out_per_min", agent_secs)
        if "text_in_per_mtok" in sku.rates:
            # each turn re-sends the growing context as (cached) text tokens; crude but visible
            m.add("s2s", profile.s2s, "text_in_per_mtok", a.system_prompt_tokens * turns)

    if profile.stt:
        stt = book.get("stt", profile.stt)
        if stt.attrs.get("bills_session_time"):
            billed = secs
            notes.append(f"{profile.stt} bills session time, not audio: VAD gating does not reduce it")
        else:
            billed = user_secs * (1 + a.stt_roll_overhead) if a.vad_gating else secs
        m.add("stt", profile.stt, "stream_per_min", billed)
        m.counters["stt_seconds_billed"] = round(billed, 1)

    if profile.tts:
        chars = agent_secs / 60.0 * a.chars_per_spoken_minute * (1 - a.tts_cache_hit_rate)
        m.add("tts", profile.tts, "per_1m_chars", chars)
        m.counters["tts_chars_billed"] = round(chars)

    if profile.llm:
        sku = book.get("llm", profile.llm)
        cache_min = int(sku.attrs.get("cache_min_tokens", 0))
        llm_turns = max(0, int(round(turns * (1 - a.fastpath_rate))))
        cached_prefix = 0
        for i in range(1, llm_turns + 1):
            history = (i - 1) * (a.user_turn_tokens + a.assistant_turn_tokens) + a.user_turn_tokens
            prefix_total = a.system_prompt_tokens + history
            split = split_prompt_cache(
                prefix_total, cached_prefix, enabled=a.prompt_caching, min_tokens=cache_min
            )
            if a.prompt_caching and prefix_total >= cache_min:
                cached_prefix = prefix_total
            m.add_llm(profile.llm, split, a.assistant_turn_tokens)
        if a.prompt_caching and cache_min and a.system_prompt_tokens < cache_min:
            notes.append(
                f"{profile.llm}: prompt caching only starts once the prefix exceeds {cache_min} tokens; "
                f"a {a.system_prompt_tokens}-token system prompt is billed at full price on early turns"
            )

    for sku, concurrency in profile.compute:
        # an instance is paid for whether or not it is busy: divide by the utilisation you actually achieve
        m.add("compute", sku, "per_hour", secs / max(1, concurrency) / max(0.05, a.capacity_utilization))

    m.counters["turns"] = turns
    return Estimate(profile, a, m, notes)


def monthly(
    book: PriceBook, profile: Profile, minutes_per_month: float, a: Assumptions | None = None
) -> float:
    est = estimate_call(book, profile, a)
    return est.usd_per_minute * minutes_per_month


def break_even_minutes(
    book: PriceBook,
    cheaper_per_min_profile: Profile,
    fixed_cost_profile: Profile,
    fixed_usd_per_month: float,
    a: Assumptions | None = None,
) -> float:
    """Monthly minutes above which ``fixed_cost_profile`` (with a fixed monthly cost, e.g. reserved
    GPUs or engineering time) beats ``cheaper_per_min_profile``. ``inf`` if never."""
    p1 = estimate_call(book, cheaper_per_min_profile, a).usd_per_minute
    p2 = estimate_call(book, fixed_cost_profile, a).usd_per_minute
    if p1 <= p2:
        return math.inf
    return fixed_usd_per_month / (p1 - p2)
