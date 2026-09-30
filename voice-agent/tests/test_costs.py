import json
import math

import pytest

from voice_agent.costs import CostMeter, PriceBook, Sku, base_unit, rate_unit, split_prompt_cache
from voice_agent.estimate import Assumptions, Profile, break_even_minutes, estimate_call
from voice_agent.profiles import PROFILES, get_profile


def test_rate_grammar():
    assert rate_unit("input_per_mtok") == "mtok"
    assert rate_unit("per_1m_chars") == "1m_chars"
    assert rate_unit("stream_per_min") == "min"
    assert base_unit("per_hour") == "seconds"
    with pytest.raises(ValueError):
        rate_unit("price")
    with pytest.raises(ValueError):
        rate_unit("per_furlong")


def test_sku_cost_units_and_increment():
    tel = Sku("telephony", "x", {"inbound_per_min": 0.006}, increment_s=60)
    assert tel.cost("inbound_per_min", 61) == pytest.approx(0.012)  # rounded up to 2 minutes
    assert tel.cost("inbound_per_min", 0) == 0
    tts = Sku("tts", "y", {"per_1m_chars": 30.0})
    assert tts.cost("per_1m_chars", 850) == pytest.approx(0.0255)
    llm = Sku("llm", "z", {"input_per_mtok": 1.0})
    assert llm.cost("input_per_mtok", 22_000) == pytest.approx(0.022)


def test_bundled_pricebook_is_complete(book: PriceBook):
    assert book.as_of.startswith("2026-")
    for sku in book.skus():
        assert sku.rates, f"{sku.component}/{sku.name} has no rates"
        assert sku.attrs.get("verification") in {"fetched", "snippet", "unverified", "n/a"}, sku.name
        if sku.attrs.get("verification") != "n/a":
            assert sku.source.startswith("http"), sku.name
    for llm in book.skus("llm"):
        assert {"input_per_mtok", "output_per_mtok", "cache_write_per_mtok", "cache_read_per_mtok"} <= set(
            llm.rates
        )
        assert "cache_min_tokens" in llm.attrs
    assert book.get("llm", "claude-haiku-4-5").attrs["cache_min_tokens"] == 4096
    with pytest.raises(KeyError):
        book.get("tts", "nope")


def test_every_profile_references_known_skus(book: PriceBook):
    for p in PROFILES.values():
        for comp, name in (
            ("telephony", p.telephony),
            ("stt", p.stt),
            ("tts", p.tts),
            ("llm", p.llm),
            ("s2s", p.s2s),
            ("platform", p.platform),
        ):
            if name:
                book.get(comp, name)
        for sku, conc in p.compute:
            book.get("compute", sku)
            assert conc >= 1


def test_split_prompt_cache():
    # below the model minimum: nothing caches even when enabled
    s = split_prompt_cache(3000, 0, enabled=True, min_tokens=4096)
    assert (s.uncached, s.cache_write, s.cache_read) == (3000, 0, 0)
    # first request over the minimum writes the whole prefix
    s = split_prompt_cache(5000, 0, enabled=True, min_tokens=4096)
    assert (s.uncached, s.cache_write, s.cache_read) == (0, 5000, 0)
    # next request reads the old prefix and writes only the new tail
    s = split_prompt_cache(5200, 5000, enabled=True, min_tokens=4096)
    assert (s.uncached, s.cache_write, s.cache_read) == (0, 200, 5000)
    assert s.total == 5200
    # disabled
    s = split_prompt_cache(5200, 5000, enabled=False, min_tokens=512)
    assert (s.uncached, s.cache_write, s.cache_read) == (5200, 0, 0)


def test_meter_aggregates_and_reports(book: PriceBook):
    m = CostMeter(book, "t")
    for _ in range(100):
        m.add("stt", "deepgram/nova-3", "stream_per_min", 0.02)  # 100 frames of 20 ms
    m.add("tts", "deepgram/aura-2", "per_1m_chars", 1000)
    m.add_llm("claude-haiku-4-5", split_prompt_cache(5000, 0, enabled=True, min_tokens=4096), 100)
    m.duration_s = 120
    lines = {(line.component, line.rate): line for line in m.lines()}
    assert lines[("stt", "stream_per_min")].quantity == pytest.approx(2.0)
    assert lines[("stt", "stream_per_min")].usd == pytest.approx(2 / 60 * 0.0077)
    assert lines[("tts", "per_1m_chars")].usd == pytest.approx(0.03)
    assert lines[("llm", "cache_write_per_mtok")].usd == pytest.approx(5000 / 1e6 * 1.25)
    assert lines[("llm", "output_per_mtok")].usd == pytest.approx(100 / 1e6 * 5)
    assert m.total() == pytest.approx(sum(line.usd for line in m.lines()))
    assert m.usd_per_minute() == pytest.approx(m.total() / 2)
    d = m.to_dict()
    json.dumps(d)  # serialisable
    assert d["breakdown_usd"]["llm"] > 0
    assert "call t" in m.format()
    with pytest.raises(KeyError):
        m.add("tts", "deepgram/aura-2", "per_min", 1)


def test_estimate_llm_matches_hand_calculation(book: PriceBook):
    p = Profile(name="llm-only", description="", llm="claude-haiku-4-5")
    a = Assumptions(
        call_minutes=3, turns_per_minute=4, fastpath_rate=0.0, prompt_caching=False, system_prompt_tokens=1500
    )
    est = estimate_call(book, p, a)
    turns = 12
    expected_in = sum(1500 + (i - 1) * 95 + 35 for i in range(1, turns + 1))
    assert est.meter.counters["llm_input_tokens"] == expected_in
    assert est.usd_per_call == pytest.approx(expected_in / 1e6 * 1.0 + turns * 60 / 1e6 * 5.0)
    assert est.usd_per_minute == pytest.approx(est.usd_per_call / 3)


def test_estimate_levers_change_cost_in_the_right_direction(book: PriceBook):
    p = get_profile("budget-hosted")
    base = estimate_call(book, p, Assumptions())
    no_gate = estimate_call(book, p, Assumptions(vad_gating=False))
    assert no_gate.meter.breakdown()["stt"] > base.meter.breakdown()["stt"] * 1.8
    # short prompt: caching cannot engage on Haiku 4.5 (4096 minimum), so the note is present and cost is equal
    cached = estimate_call(book, p, Assumptions(system_prompt_tokens=1500, prompt_caching=True))
    uncached = estimate_call(book, p, Assumptions(system_prompt_tokens=1500, prompt_caching=False))
    assert cached.meter.breakdown()["llm"] == pytest.approx(uncached.meter.breakdown()["llm"])
    assert any("4096" in n for n in cached.notes)
    # big knowledge prompt: caching cuts the LLM bill substantially
    cached = estimate_call(book, p, Assumptions(system_prompt_tokens=4500, prompt_caching=True))
    uncached = estimate_call(book, p, Assumptions(system_prompt_tokens=4500, prompt_caching=False))
    assert cached.meter.breakdown()["llm"] < 0.5 * uncached.meter.breakdown()["llm"]
    # session-billed STT ignores gating
    floor = get_profile("floor-hosted")
    g = estimate_call(book, floor, Assumptions(vad_gating=True))
    ng = estimate_call(book, floor, Assumptions(vad_gating=False))
    assert g.meter.breakdown()["stt"] == pytest.approx(ng.meter.breakdown()["stt"])
    assert any("session time" in n for n in g.notes)


def test_estimate_ordering_and_break_even(book: PriceBook):
    per_min = {name: estimate_call(book, p).usd_per_minute for name, p in PROFILES.items()}
    assert per_min["self-hosted-llm"] < per_min["self-hosted"] < per_min["budget-hosted"]
    assert per_min["budget-hosted"] < per_min["speech-to-speech"]
    assert per_min["budget-hosted"] < per_min["managed-platform"]
    # spending $3,000/month of ops time on self-hosting pays off only above some volume
    be = break_even_minutes(book, get_profile("budget-hosted"), get_profile("self-hosted-llm"), 3000)
    assert 10_000 < be < 1_000_000
    assert (
        break_even_minutes(book, get_profile("self-hosted-llm"), get_profile("budget-hosted"), 1) == math.inf
    )
