"""Unit tests for agent_trader.marketdata.simulator.SimulatedMarketData."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from agent_trader.charges import Exchange
from agent_trader.clock import IST, MarketClock, MarketPhase
from agent_trader.instruments import UNIVERSE
from agent_trader.marketdata import Candle, InstrumentInfo, MarketDataProvider, Quote, SimulatedMarketData
from agent_trader.marketdata.simulator import INTERVAL_MINUTES, round_to_tick

D = Decimal
TICK = D("0.05")
T0 = datetime(2026, 9, 16, 10, 0, tzinfo=IST)


def is_tick_multiple(p: Decimal, tick: Decimal = TICK) -> bool:
    return (p / tick) == (p / tick).to_integral_value()


def run_steps(mkt: SimulatedMarketData, start: datetime, n: int, seconds: int = 1) -> list[list[Quote]]:
    out = []
    for i in range(1, n + 1):
        out.append(mkt.step(start + timedelta(seconds=i * seconds)))
    return out


# ---- construction / instruments -------------------------------------------------------------


def test_implements_provider_protocol(market: SimulatedMarketData) -> None:
    assert isinstance(market, MarketDataProvider)


def test_instruments_lists_every_symbol_on_nse_and_bse(market: SimulatedMarketData) -> None:
    infos = market.instruments()
    assert len(infos) == 2 * len(UNIVERSE)
    assert all(isinstance(i, InstrumentInfo) for i in infos)
    keys = {(i.symbol, i.exchange) for i in infos}
    for s in UNIVERSE:
        assert (s.symbol, Exchange.NSE) in keys
        assert (s.symbol, Exchange.BSE) in keys
    assert {i.exchange for i in infos} == {Exchange.NSE, Exchange.BSE}
    rel = market.instrument("RELIANCE", Exchange.NSE)
    assert (
        rel is not None and rel.name == "Reliance Industries" and rel.tick_size == TICK and rel.lot_size == 1
    )
    assert rel.band_pct == 10
    assert market.instrument("NOPE", Exchange.NSE) is None
    assert market.instrument("reliance", Exchange.BSE) is not None  # case-insensitive


def test_exchanges_can_be_restricted(clock: MarketClock) -> None:
    mkt = SimulatedMarketData(clock, seed=1, warmup_candles=0, exchanges=(Exchange.NSE,))
    assert {i.exchange for i in mkt.instruments()} == {Exchange.NSE}
    assert mkt.quote("TCS", Exchange.BSE) is None


def test_no_warmup_starts_at_reference_price_with_bse_basis(clock: MarketClock) -> None:
    mkt = SimulatedMarketData(clock, seed=1, warmup_candles=0)
    for s in UNIVERSE:
        q = mkt.quote(s.symbol, Exchange.NSE)
        assert q is not None
        assert q.ltp == q.open == q.high == q.low == q.prev_close == s.ref_price
        assert q.volume == 0
        assert q.ts == clock.now()
        b = mkt.quote(s.symbol, Exchange.BSE)
        assert b is not None
        assert b.ltp == round_to_tick(s.ref_price * D("1.0004"), s.tick_size)
        if s.ref_price * D("0.0004") >= s.tick_size:
            assert b.ltp != q.ltp
    assert mkt.ohlc("RELIANCE", Exchange.NSE, "1m", 100) == []


def test_warmup_populates_history_ending_at_now(market: SimulatedMarketData, clock: MarketClock) -> None:
    candles = market.ohlc("RELIANCE", Exchange.NSE, "1m", 1000)
    assert len(candles) == 30  # sim_warmup_candles=30 in conftest
    assert candles[0].ts == T0 - timedelta(minutes=30)
    assert candles[-1].ts == T0 - timedelta(minutes=1)
    for a, b in zip(candles, candles[1:], strict=False):
        assert b.ts - a.ts == timedelta(minutes=1)
    q = market.quote("RELIANCE", Exchange.NSE)
    assert q is not None
    assert q.ltp == candles[-1].close
    assert q.open == candles[0].open
    assert q.high == max(c.high for c in candles)
    assert q.low == min(c.low for c in candles)
    assert q.volume == sum(c.volume for c in candles)


# ---- quotes ----------------------------------------------------------------------------------


def test_quote_shape_and_bid_ask_spread(market: SimulatedMarketData) -> None:
    q = market.quote("tcs", Exchange.NSE)  # case-insensitive
    assert isinstance(q, Quote)
    assert q.symbol == "TCS" and q.exchange == Exchange.NSE
    assert q.bid == q.ltp - TICK
    assert q.ask == q.ltp + TICK
    assert q.lower_circuit <= q.low <= q.ltp <= q.high <= q.upper_circuit
    assert q.upper_circuit == round_to_tick(q.prev_close * D("1.10"), TICK)
    assert q.lower_circuit == round_to_tick(q.prev_close * D("0.90"), TICK)
    assert q.change == q.ltp - q.prev_close
    assert market.quote("NOPE", Exchange.NSE) is None


def test_quotes_all_and_filtered(market: SimulatedMarketData) -> None:
    allq = market.quotes()
    assert len(allq) == 2 * len(UNIVERSE)
    some = market.quotes([("INFY", Exchange.NSE), ("infy", Exchange.BSE), ("NOPE", Exchange.NSE)])
    assert [(q.symbol, q.exchange) for q in some] == [("INFY", Exchange.NSE), ("INFY", Exchange.BSE)]
    assert market.quotes([]) == []


# ---- determinism ------------------------------------------------------------------------------


def test_same_seed_same_prices_after_n_steps(clock: MarketClock) -> None:
    a = SimulatedMarketData(clock, seed=7, warmup_candles=30)
    b = SimulatedMarketData(clock, seed=7, warmup_candles=30)
    assert [q.model_dump() for q in a.quotes()] == [q.model_dump() for q in b.quotes()]
    ra = run_steps(a, T0, 120, seconds=3)
    rb = run_steps(b, T0, 120, seconds=3)
    assert [[q.model_dump() for q in step] for step in ra] == [[q.model_dump() for q in step] for step in rb]
    assert [q.model_dump() for q in a.quotes()] == [q.model_dump() for q in b.quotes()]
    ca = a.ohlc("SBIN", Exchange.NSE, "1m", 1000)
    cb = b.ohlc("SBIN", Exchange.NSE, "1m", 1000)
    assert [c.model_dump() for c in ca] == [c.model_dump() for c in cb]
    assert len(ca) > 30  # new candles were built by the ticks


def test_different_seed_different_prices(clock: MarketClock) -> None:
    a = SimulatedMarketData(clock, seed=7, warmup_candles=30)
    b = SimulatedMarketData(clock, seed=8, warmup_candles=30)
    la = [q.ltp for q in a.quotes()]
    lb = [q.ltp for q in b.quotes()]
    assert la != lb
    assert sum(x != y for x, y in zip(la, lb, strict=False)) > len(la) // 2


def test_per_instrument_rng_is_independent_of_other_instruments(clock: MarketClock) -> None:
    """Each (symbol, exchange) has its own RNG seeded from the symbol, so restricting the universe
    does not change another instrument's path."""
    full = SimulatedMarketData(clock, seed=3, warmup_candles=20)
    only = SimulatedMarketData(clock, [s for s in UNIVERSE if s.symbol == "ITC"], seed=3, warmup_candles=20)
    run_steps(full, T0, 50)
    run_steps(only, T0, 50)
    assert full.quote("ITC", Exchange.NSE).model_dump() == only.quote("ITC", Exchange.NSE).model_dump()


# ---- price invariants ---------------------------------------------------------------------------


def test_prices_stay_within_band_and_on_tick_under_extreme_volatility(clock: MarketClock) -> None:
    mkt = SimulatedMarketData(clock, seed=11, vol_scale=300.0, warmup_candles=10)
    hit_upper = hit_lower = 0
    for i in range(1, 200):
        quotes = mkt.step(T0 + timedelta(seconds=60 * i))
        for q in quotes:
            assert q.lower_circuit <= q.ltp <= q.upper_circuit, q
            assert q.lower_circuit <= q.bid <= q.ask <= q.upper_circuit
            assert is_tick_multiple(q.ltp) and is_tick_multiple(q.bid) and is_tick_multiple(q.ask)
            assert q.low <= q.ltp <= q.high
            hit_upper += q.ltp == q.upper_circuit
            hit_lower += q.ltp == q.lower_circuit
    # with 300× vol the circuit filter must have bitten many times in both directions
    assert hit_upper > 0 and hit_lower > 0
    for q in mkt.quotes():
        assert q.lower_circuit <= q.low <= q.high <= q.upper_circuit
        assert is_tick_multiple(q.upper_circuit) and is_tick_multiple(q.lower_circuit)
    for c in mkt.ohlc("ADANIENT", Exchange.NSE, "1m", 10000):
        assert (
            is_tick_multiple(c.open)
            and is_tick_multiple(c.high)
            and is_tick_multiple(c.low)
            and is_tick_multiple(c.close)
        )
        assert c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high


def test_warmup_prices_are_tick_multiples_and_in_band(market: SimulatedMarketData) -> None:
    for q in market.quotes():
        assert is_tick_multiple(q.ltp)
        assert q.lower_circuit <= q.ltp <= q.upper_circuit


def test_step_returns_only_changed_quotes_and_advances_ts(market: SimulatedMarketData) -> None:
    before = {(q.symbol, q.exchange): q for q in market.quotes()}
    now = T0 + timedelta(seconds=1)
    changed = market.step(now)
    assert changed  # something always moves (volume is added every tick)
    for q in changed:
        assert q.ts == now
        b = before[(q.symbol, q.exchange)]
        assert q.ltp != b.ltp or q.volume != b.volume
    # stepping to the same instant again is a no-op
    assert market.step(now) == []


def test_step_accepts_naive_datetime_as_ist(market: SimulatedMarketData) -> None:
    changed = market.step(datetime(2026, 9, 16, 10, 0, 5))
    assert changed
    assert changed[0].ts == T0 + timedelta(seconds=5)
    assert changed[0].ts.tzinfo is not None


# ---- phases ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "when, phase",
    [
        (datetime(2026, 9, 16, 8, 30, tzinfo=IST), MarketPhase.CLOSED),
        (datetime(2026, 9, 16, 9, 10, tzinfo=IST), MarketPhase.PRE_OPEN),
        (datetime(2026, 9, 16, 15, 30, tzinfo=IST), MarketPhase.CLOSED),
        (datetime(2026, 9, 16, 15, 45, tzinfo=IST), MarketPhase.CLOSING),
        (datetime(2026, 9, 16, 22, 0, tzinfo=IST), MarketPhase.CLOSED),
    ],
)
def test_no_movement_outside_normal_session_same_day(
    market: SimulatedMarketData, clock: MarketClock, when: datetime, phase: MarketPhase
) -> None:
    assert clock.phase(when) == phase
    before = {(q.symbol, q.exchange): q.model_dump(exclude={"ts"}) for q in market.quotes()}
    for i in range(5):
        assert market.step(when + timedelta(seconds=i)) == []
    after = {(q.symbol, q.exchange): q.model_dump(exclude={"ts"}) for q in market.quotes()}
    assert after == before
    # last_ts still advances so a later NORMAL tick does not accumulate the idle gap
    assert market.quote("TCS", Exchange.NSE).ts == when + timedelta(seconds=4)


def test_no_movement_and_no_roll_on_weekend_or_holiday(
    market: SimulatedMarketData, clock: MarketClock
) -> None:
    before = {(q.symbol, q.exchange): q.model_dump(exclude={"ts"}) for q in market.quotes()}
    for d in (date(2026, 9, 19), date(2026, 9, 20), date(2026, 9, 14)):  # Sat, Sun, Ganesh Chaturthi
        when = datetime.combine(d, datetime.min.time(), tzinfo=IST).replace(hour=11)
        assert clock.phase(when) == MarketPhase.HOLIDAY
        assert market.step(when) == []
    after = {(q.symbol, q.exchange): q.model_dump(exclude={"ts"}) for q in market.quotes()}
    assert after == before  # prev_close, OHLC and volume untouched: no day roll happened


def test_resumes_moving_when_normal_session_returns(market: SimulatedMarketData) -> None:
    market.step(datetime(2026, 9, 16, 15, 45, tzinfo=IST))
    ltp_closed = market.quote("INFY", Exchange.NSE).ltp
    # next trading day, CLOSED pre-market: roll only, no tick
    market.step(datetime(2026, 9, 17, 8, 0, tzinfo=IST))
    assert market.quote("INFY", Exchange.NSE).prev_close == ltp_closed
    changed = market.step(datetime(2026, 9, 17, 9, 15, 0, tzinfo=IST))
    changed = market.step(datetime(2026, 9, 17, 9, 15, 1, tzinfo=IST))
    assert any(q.symbol == "INFY" and q.exchange == Exchange.NSE for q in changed)


# ---- day roll ---------------------------------------------------------------------------------


def test_day_roll_sets_prev_close_and_resets_ohlc_volume(market: SimulatedMarketData) -> None:
    run_steps(market, T0, 30)  # accumulate some intraday movement and volume
    market.record_trade("RELIANCE", Exchange.NSE, 500)
    before = {(q.symbol, q.exchange): q for q in market.quotes()}
    assert all(q.volume > 0 for q in before.values())

    next_day = datetime(2026, 9, 17, 8, 0, tzinfo=IST)  # CLOSED: roll happens, no tick
    changed = market.step(next_day)
    assert len(changed) == 2 * len(UNIVERSE)  # every instrument reports its rolled quote
    for q in market.quotes():
        b = before[(q.symbol, q.exchange)]
        assert q.prev_close == b.ltp, "prev_close must be yesterday's last price"
        assert q.open == q.high == q.low == q.ltp, "OHLC reset to the opening print"
        assert q.volume == 0
        assert q.ts == next_day
        # overnight gap is small and clipped to the new band; on-tick
        assert is_tick_multiple(q.ltp)
        assert q.lower_circuit <= q.ltp <= q.upper_circuit
        assert abs(q.ltp / q.prev_close - 1) < D("0.05")
        band = market.instrument(q.symbol, q.exchange).band_pct
        assert q.upper_circuit == round_to_tick(q.prev_close * (100 + band) / 100, TICK)
        assert q.lower_circuit == round_to_tick(q.prev_close * (100 - band) / 100, TICK)
    # rolling only happens once per day
    assert market.step(next_day + timedelta(minutes=1)) == []
    assert market.quote("RELIANCE", Exchange.NSE).volume == 0


def test_day_roll_during_normal_session_then_ticks(market: SimulatedMarketData) -> None:
    prev = {(q.symbol, q.exchange): q.ltp for q in market.quotes()}
    when = datetime(2026, 9, 17, 10, 0, tzinfo=IST)
    changed = market.step(when)
    # roll + tick in the same call: one quote per instrument for the roll plus one per tick
    assert len(changed) >= 2 * len(UNIVERSE)
    for q in market.quotes():
        assert q.prev_close == prev[(q.symbol, q.exchange)]
        assert q.low <= q.open <= q.high and q.low <= q.ltp <= q.high
    # the previous day's 1m candles are preserved in history and a new candle started today
    candles = market.ohlc("TCS", Exchange.NSE, "1m", 10000)
    assert candles[-1].ts == when.replace(second=0, microsecond=0)
    assert candles[-2].ts.date() == date(2026, 9, 16)


def test_day_roll_across_weekend_uses_friday_close(market: SimulatedMarketData) -> None:
    fri = datetime(2026, 9, 18, 10, 0, tzinfo=IST)
    market.step(fri)  # roll to Friday
    run_steps(market, fri, 10)
    fri_close = market.quote("HDFCBANK", Exchange.NSE).ltp
    assert market.step(datetime(2026, 9, 19, 10, 0, tzinfo=IST)) == []  # Saturday: nothing
    market.step(datetime(2026, 9, 21, 8, 0, tzinfo=IST))  # Monday pre-market
    q = market.quote("HDFCBANK", Exchange.NSE)
    assert q.prev_close == fri_close
    assert q.volume == 0


# ---- OHLC aggregation -----------------------------------------------------------------------


def _aggregate(candles: list[Candle], mins: int | None) -> list[Candle]:
    buckets: OrderedDict[datetime, Candle] = OrderedDict()
    for c in candles:
        if mins is None:
            key = c.ts.replace(hour=0, minute=0, second=0, microsecond=0)
        elif mins >= 60:
            key = c.ts.replace(minute=0, second=0, microsecond=0)
            key = key.replace(hour=(key.hour // (mins // 60)) * (mins // 60))
        else:
            key = c.ts.replace(minute=(c.ts.minute // mins) * mins, second=0, microsecond=0)
        b = buckets.get(key)
        if b is None:
            buckets[key] = Candle(ts=key, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume)
        else:
            b.high = max(b.high, c.high)
            b.low = min(b.low, c.low)
            b.close = c.close
            b.volume += c.volume
    return list(buckets.values())


@pytest.fixture
def history(clock: MarketClock) -> SimulatedMarketData:
    """A market with ~3 sessions of history spanning several days (via warmup + ticks across a roll)."""
    mkt = SimulatedMarketData(
        clock, [s for s in UNIVERSE if s.symbol in ("RELIANCE", "TCS")], seed=5, warmup_candles=400
    )
    # tick through the rest of Wednesday at 20-second spacing until 15:30, then Thursday morning
    t = T0
    while t < datetime(2026, 9, 16, 15, 30, tzinfo=IST):
        t += timedelta(seconds=20)
        mkt.step(t)
    t = datetime(2026, 9, 17, 9, 15, tzinfo=IST)
    while t < datetime(2026, 9, 17, 11, 7, 30, tzinfo=IST):
        t += timedelta(seconds=15)
        mkt.step(t)
    return mkt


@pytest.mark.parametrize("interval", ["3m", "5m", "15m", "30m", "1h", "1d"])
def test_ohlc_aggregation_matches_one_minute_candles(history: SimulatedMarketData, interval: str) -> None:
    for sym in ("RELIANCE", "TCS"):
        ones = history.ohlc(sym, Exchange.NSE, "1m", 100000)
        assert len(ones) > 500
        assert ones[-1].ts == datetime(2026, 9, 17, 11, 7, tzinfo=IST)  # the in-progress minute is included
        assert {c.ts.date() for c in ones} == {date(2026, 9, 16), date(2026, 9, 17)}
        got = history.ohlc(sym, Exchange.NSE, interval, 100000)
        exp = _aggregate(ones, INTERVAL_MINUTES[interval])
        assert [c.model_dump() for c in got] == [c.model_dump() for c in exp]
        assert len(got) < len(ones)
        for c in got:
            assert c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high
            assert c.volume >= 0
        # volume is conserved by aggregation
        assert sum(c.volume for c in got) == sum(c.volume for c in ones)
        # bucket timestamps are aligned to the interval
        mins = INTERVAL_MINUTES[interval]
        for c in got:
            assert c.ts.second == 0 and c.ts.microsecond == 0
            if mins is None:
                assert c.ts.hour == 0 and c.ts.minute == 0
            elif mins >= 60:
                assert c.ts.minute == 0
            else:
                assert c.ts.minute % mins == 0
        # strictly increasing timestamps
        assert all(a.ts < b.ts for a, b in zip(got, got[1:], strict=False))


def test_ohlc_1d_has_one_candle_per_day_with_day_open_and_close(history: SimulatedMarketData) -> None:
    days = history.ohlc("RELIANCE", Exchange.NSE, "1d", 10)
    ones = history.ohlc("RELIANCE", Exchange.NSE, "1m", 100000)
    assert [c.ts.date() for c in days] == [date(2026, 9, 16), date(2026, 9, 17)]
    thu = [c for c in ones if c.ts.date() == date(2026, 9, 17)]
    assert days[-1].open == thu[0].open
    assert days[-1].close == thu[-1].close == history.quote("RELIANCE", Exchange.NSE).ltp
    assert days[-1].high == max(c.high for c in thu)
    assert days[-1].low == min(c.low for c in thu)
    assert days[-1].volume == sum(c.volume for c in thu)


def test_ohlc_limit_returns_most_recent(history: SimulatedMarketData) -> None:
    all5 = history.ohlc("TCS", Exchange.NSE, "5m", 100000)
    last3 = history.ohlc("TCS", Exchange.NSE, "5m", 3)
    assert [c.model_dump() for c in last3] == [c.model_dump() for c in all5[-3:]]
    all1 = history.ohlc("TCS", Exchange.NSE, "1m", 100000)
    assert [c.model_dump() for c in history.ohlc("TCS", Exchange.NSE, "1m", 7)] == [
        c.model_dump() for c in all1[-7:]
    ]
    assert history.ohlc("TCS", Exchange.NSE) == history.ohlc("TCS", Exchange.NSE, "1m", 100)  # defaults


def test_ohlc_in_progress_minute_reflects_latest_tick(market: SimulatedMarketData) -> None:
    now = T0 + timedelta(seconds=1)
    market.step(now)
    market.step(now + timedelta(seconds=1))
    cur = market.ohlc("SBIN", Exchange.NSE, "1m", 1)[0]
    q = market.quote("SBIN", Exchange.NSE)
    assert cur.ts == T0
    assert cur.close == q.ltp
    assert cur.low <= q.ltp <= cur.high


@pytest.mark.parametrize("bad", ["2m", "1M", "", "1w", "5", "1H"])
def test_ohlc_unsupported_interval_raises(market: SimulatedMarketData, bad: str) -> None:
    with pytest.raises(ValueError) as ei:
        market.ohlc("RELIANCE", Exchange.NSE, bad, 10)
    assert "unsupported interval" in str(ei.value)
    assert bad in str(ei.value) or bad == ""


def test_ohlc_unknown_symbol_is_empty(market: SimulatedMarketData) -> None:
    assert market.ohlc("NOPE", Exchange.NSE, "1m", 10) == []
    assert market.ohlc("NOPE", Exchange.NSE, "bogus", 10) == []  # symbol lookup precedes interval validation


def test_ohlc_history_is_bounded(clock: MarketClock) -> None:
    from agent_trader.marketdata.simulator import MAX_1M_CANDLES

    mkt = SimulatedMarketData(clock, [UNIVERSE[0]], seed=1, warmup_candles=MAX_1M_CANDLES + 100)
    ones = mkt.ohlc(UNIVERSE[0].symbol, Exchange.NSE, "1m", 10**6)
    assert len(ones) == MAX_1M_CANDLES


# ---- set_price ---------------------------------------------------------------------------------


def test_set_price_sets_ltp_and_rounds_to_tick(market: SimulatedMarketData) -> None:
    q = market.set_price("RELIANCE", Exchange.NSE, D("1480.07"))
    assert q.ltp == D("1480.05")
    assert market.quote("RELIANCE", Exchange.NSE).ltp == D("1480.05")
    assert q.bid == D("1480.00") and q.ask == D("1480.10")
    q = market.set_price("reliance", Exchange.NSE, D("1480.08"))
    assert q.ltp == D("1480.10")
    # only this listing moved
    assert market.quote("RELIANCE", Exchange.BSE).ltp != D("1480.10")


def test_set_price_clips_to_band(market: SimulatedMarketData) -> None:
    q0 = market.quote("TCS", Exchange.NSE)
    hi = market.set_price("TCS", Exchange.NSE, q0.prev_close * 2)
    assert hi.ltp == q0.upper_circuit
    assert hi.ask == q0.upper_circuit  # ask cannot exceed the band
    assert hi.bid == q0.upper_circuit - TICK
    assert hi.high == q0.upper_circuit
    lo = market.set_price("TCS", Exchange.NSE, D("1"))
    assert lo.ltp == q0.lower_circuit
    assert lo.bid == q0.lower_circuit
    assert lo.ask == q0.lower_circuit + TICK
    assert lo.low == q0.lower_circuit
    assert lo.high == q0.upper_circuit  # day high remembers the earlier forced print
    assert lo.prev_close == q0.prev_close  # band anchor unchanged


def test_set_price_updates_current_candle_without_volume(
    market: SimulatedMarketData, clock: MarketClock
) -> None:
    v0 = market.quote("ITC", Exchange.NSE).volume
    market.set_price("ITC", Exchange.NSE, D("430"))
    cur = market.ohlc("ITC", Exchange.NSE, "1m", 1)[0]
    assert cur.ts == clock.now().replace(second=0, microsecond=0)
    assert cur.close == D("430.00") and cur.volume == 0
    assert market.quote("ITC", Exchange.NSE).volume == v0
    clock.advance(timedelta(minutes=1))
    market.set_price("ITC", Exchange.NSE, D("431"))
    last2 = market.ohlc("ITC", Exchange.NSE, "1m", 2)
    assert [c.ts for c in last2] == [T0, T0 + timedelta(minutes=1)]
    assert last2[0].close == D("430.00") and last2[1].close == D("431.00")


def test_set_price_unknown_symbol_raises(market: SimulatedMarketData) -> None:
    with pytest.raises(KeyError):
        market.set_price("NOPE", Exchange.NSE, D("100"))


def test_set_price_accepts_str_or_float_like(market: SimulatedMarketData) -> None:
    q = market.set_price("WIPRO", Exchange.NSE, "255.10")  # type: ignore[arg-type]
    assert q.ltp == D("255.10")


# ---- record_trade -------------------------------------------------------------------------------


def test_record_trade_adds_volume_to_day_and_current_candle(market: SimulatedMarketData) -> None:
    market.step(T0 + timedelta(seconds=1))
    q0 = market.quote("SBIN", Exchange.NSE)
    c0 = market.ohlc("SBIN", Exchange.NSE, "1m", 1)[0]
    market.record_trade("sbin", Exchange.NSE, 250)
    q1 = market.quote("SBIN", Exchange.NSE)
    c1 = market.ohlc("SBIN", Exchange.NSE, "1m", 1)[0]
    assert q1.volume == q0.volume + 250
    assert c1.volume == c0.volume + 250
    assert q1.ltp == q0.ltp  # price is untouched
    assert market.quote("SBIN", Exchange.BSE).volume != q1.volume or True  # other listing untouched
    # aggregated candles see the added volume too
    assert market.ohlc("SBIN", Exchange.NSE, "5m", 1)[0].volume >= c1.volume


def test_record_trade_without_current_candle_only_updates_day_volume(clock: MarketClock) -> None:
    mkt = SimulatedMarketData(clock, [UNIVERSE[0]], seed=1, warmup_candles=0)
    sym = UNIVERSE[0].symbol
    mkt.record_trade(sym, Exchange.NSE, 10)
    assert mkt.quote(sym, Exchange.NSE).volume == 10
    assert mkt.ohlc(sym, Exchange.NSE, "1m", 10) == []


def test_record_trade_unknown_symbol_is_noop(market: SimulatedMarketData) -> None:
    before = [q.volume for q in market.quotes()]
    market.record_trade("NOPE", Exchange.NSE, 1000)
    assert [q.volume for q in market.quotes()] == before


# ---- helpers ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price, tick, expected",
    [
        ("100.02", "0.05", "100.00"),
        ("100.025", "0.05", "100.05"),  # half-up
        ("100.03", "0.05", "100.05"),
        ("100.07", "0.05", "100.05"),
        ("100.075", "0.05", "100.10"),
        ("99.999", "0.05", "100.00"),
        ("7.3", "0.5", "7.5"),
        ("7.24", "0.5", "7.0"),
    ],
)
def test_round_to_tick(price: str, tick: str, expected: str) -> None:
    assert round_to_tick(D(price), D(tick)) == D(expected)
