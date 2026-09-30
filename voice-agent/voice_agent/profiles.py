"""Named stacks the estimator and simulator can price. SKU names are price-book keys."""

from __future__ import annotations

from .estimate import Profile

PROFILES: dict[str, Profile] = {
    "budget-hosted": Profile(
        name="budget-hosted",
        description="Cheapest all-hosted cascade: Telnyx PSTN, Deepgram Nova-3 STT, Claude Haiku 4.5, Deepgram Aura-2 TTS, "
        "one small shared VM for the orchestrator.",
        telephony="telnyx/voice",
        stt="deepgram/nova-3",
        llm="claude-haiku-4-5",
        tts="deepgram/aura-2",
        compute=(("hetzner/ccx23", 40),),
    ),
    "floor-hosted": Profile(
        name="floor-hosted",
        description="Cheapest hosted parts on paper: AssemblyAI streaming (session-billed), a hosted open-weight LLM (Qwen3-14B on "
        "DeepInfra), Inworld TTS-1.5 Mini; quality and tooling below the Claude-based stacks.",
        telephony="telnyx/voice",
        stt="assemblyai/universal-streaming",
        llm="deepinfra/qwen3-14b",
        tts="inworld/tts-1.5-mini",
        compute=(("hetzner/cx33", 20),),
    ),
    "self-hosted": Profile(
        name="self-hosted",
        description="SIP trunk into a self-run media stack; STT (Parakeet/faster-whisper) and TTS (Kokoro) on one shared "
        "L4-class GPU box that also runs the orchestrator; the LLM stays hosted (Claude Haiku 4.5).",
        telephony="telnyx/sip-trunk",
        stt="selfhost/parakeet",
        llm="claude-haiku-4-5",
        tts="selfhost/kokoro",
        compute=(("runpod/l4", 20),),
    ),
    "self-hosted-llm": Profile(
        name="self-hosted-llm",
        description="Everything self-run: the self-hosted media stack plus Qwen3-8B on vLLM on a second L4 shared by ~40 calls. "
        "Lowest marginal cost, highest fixed cost and operational load.",
        telephony="telnyx/sip-trunk",
        stt="selfhost/parakeet",
        llm="selfhost/qwen3-8b",
        tts="selfhost/kokoro",
        compute=(("runpod/l4", 20), ("runpod/l4", 40)),
    ),
    "quality-value": Profile(
        name="quality-value",
        description="Recommended production stack: Telnyx PSTN, Deepgram Nova-3, Claude Sonnet 5.5 (thinking off, cached prompt), "
        "Deepgram Aura-2. Sonnet 5.5 caches from 512 tokens, so with a normal prompt it costs no more than Haiku 4.5 "
        "and resolves harder calls.",
        telephony="telnyx/voice",
        stt="deepgram/nova-3",
        llm="claude-sonnet-5-5",
        tts="deepgram/aura-2",
        compute=(("hetzner/ccx23", 40),),
    ),
    "balanced-hosted": Profile(
        name="balanced-hosted",
        description="Same cascade with a stronger LLM (Claude Sonnet 5.5) and Cartesia Sonic TTS; Twilio PSTN.",
        telephony="twilio/voice",
        stt="deepgram/nova-3",
        llm="claude-sonnet-5-5",
        tts="cartesia/sonic",
        compute=(("hetzner/ccx23", 40),),
    ),
    "premium-hosted": Profile(
        name="premium-hosted",
        description="Best-sounding hosted stack: Claude Sonnet 5.5 and ElevenLabs Flash; Twilio PSTN.",
        telephony="twilio/voice",
        stt="deepgram/nova-3",
        llm="claude-sonnet-5-5",
        tts="elevenlabs/flash-v2.5",
        compute=(("hetzner/ccx23", 40),),
    ),
    "speech-to-speech": Profile(
        name="speech-to-speech",
        description="Single speech-native model (OpenAI Realtime) over Twilio: fewest moving parts, highest per-minute price.",
        telephony="twilio/voice",
        s2s="openai/gpt-realtime",
        compute=(("hetzner/ccx23", 40),),
    ),
    "managed-platform": Profile(
        name="managed-platform",
        description="Buy-not-build baseline: a managed voice-agent platform's all-in per-minute price plus telephony.",
        telephony="twilio/voice",
        platform="retell",
    ),
}


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError(f"unknown profile {name!r}; known: {sorted(PROFILES)}") from None
