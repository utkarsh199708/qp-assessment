import json
from dataclasses import replace

import pytest

from voice_agent.cache import TTSCache
from voice_agent.simulate import DEFAULT_SCRIPT, Levers, compare_levers, run_call
from voice_agent.transports.sim import ScriptLine


async def test_default_call_uses_every_lever(book):
    r = await run_call(book, "budget-hosted")
    m, s = r.meter, r.session
    assert s.end_reason == "agent_hangup"
    c = m.counters
    assert c["user_turns"] == len(DEFAULT_SCRIPT)
    assert c["fastpath_hits"] == 3  # repeat, hold, goodbye
    assert c["barge_ins"] == 1
    assert r.transport.cleared == 1
    assert c["stt_seconds_billed"] < 0.4 * c["audio_seconds_in"]
    assert c["tts_cache_hits"] >= 2
    assert c["llm_calls"] == len(DEFAULT_SCRIPT) - 3
    assert 0.3 < c["response_latency_avg_s"] < 1.0
    interrupted = [t for t in s.turns if t.interrupted]
    assert len(interrupted) == 1 and interrupted[0].agent_text.startswith("We're open")
    assert any(msg.content.endswith("[interrupted]") for msg in s.history if msg.role == "assistant")
    assert s.history[0].role == "assistant"  # greeting is on the record for the LLM
    assert m.total() > 0 and 0 < m.usd_per_minute() < 0.1
    d = m.to_dict()
    json.dumps(d)
    assert {line["component"] for line in d["lines"]} == {"telephony", "stt", "llm", "tts", "compute"}
    assert d["lines"][0]["sku"]


async def test_levers_off_bills_everything(book):
    r = await run_call(book, "budget-hosted", levers=Levers.all_off())
    c = r.meter.counters
    assert c["stt_seconds_billed"] == pytest.approx(c["audio_seconds_in"], rel=0.01)
    assert c.get("fastpath_hits", 0) == 0
    assert c.get("barge_ins", 0) == 0
    assert c["tts_cache_hits"] == 0
    assert c["llm_calls"] == len(DEFAULT_SCRIPT)
    # without barge-in the interrupting line waits for the agent to finish, then is answered
    assert not any(t.interrupted for t in r.session.turns)


async def test_prompt_caching_needs_the_model_minimum(book):
    short = await run_call(book, "budget-hosted", levers=Levers())
    assert short.meter.counters["llm_cache_read_tokens"] == 0  # 70-token prompt < 4096 on Haiku
    big_nocache = await run_call(
        book, "budget-hosted", levers=Levers(prompt_caching=False, system_padding_tokens=4500)
    )
    big_cache = await run_call(book, "budget-hosted", levers=Levers(system_padding_tokens=4500))
    assert big_cache.meter.counters["llm_cache_read_tokens"] > 10_000
    assert big_cache.meter.breakdown()["llm"] < 0.5 * big_nocache.meter.breakdown()["llm"]
    # Sonnet 5.5 caches from 512 tokens, so even the short prompt gets cache reads once history grows
    sonnet = await run_call(book, "balanced-hosted", levers=Levers(system_padding_tokens=600))
    assert sonnet.meter.counters["llm_cache_read_tokens"] > 0


async def test_tts_cache_is_shared_across_calls(book):
    cache = TTSCache()
    first = await run_call(book, "budget-hosted", tts_cache=cache)
    second = await run_call(book, "budget-hosted", tts_cache=cache)
    assert second.meter.counters.get("tts_chars_billed", 0) < first.meter.counters["tts_chars_billed"]
    # greeting + every fast-path phrase + identical LLM sentences are free the second time
    assert second.meter.breakdown().get("tts", 0.0) < 0.6 * first.meter.breakdown()["tts"]
    assert second.meter.counters["tts_cache_hits"] > first.meter.counters["tts_cache_hits"]


async def test_silent_caller_is_reprompted_then_dropped(book):
    r = await run_call(book, "budget-hosted", script=[ScriptLine("", speak_seconds=0.0, max_wait_s=0.0)])
    assert r.session.end_reason == "idle_timeout"
    assert r.meter.counters["idle_reprompts"] == 1
    assert [t.source for t in r.session.turns] == ["greeting", "idle"]


async def test_history_trimming_keeps_prefix_stable(book):
    script = [ScriptLine(f"Does Monday work? Question {i}.") for i in range(12)] + [ScriptLine("bye")]
    r = await run_call(book, "budget-hosted", script=script, levers=replace(Levers(), fastpath=True))
    assert r.meter.counters["history_trims"] >= 1
    assert len(r.session.history) <= r.session.config.max_history_messages + 2


async def test_compare_levers_is_monotone_for_the_cumulative_steps(book):
    results = await compare_levers(book, "budget-hosted")
    labels = list(results)
    off, gated = results[labels[0]], results[labels[1]]
    assert gated.total() < off.total()
    fast = results[labels[2]]
    assert fast.counters["llm_calls"] < gated.counters["llm_calls"]
    assert (
        results["4.5k-token FAQ prompt, cached"].total() < results["4.5k-token FAQ prompt, no cache"].total()
    )


async def test_non_cascade_profile_is_rejected(book):
    with pytest.raises(ValueError):
        await run_call(book, "speech-to-speech")
