"""Seedable market simulator.

Each (symbol, exchange) follows a geometric Brownian motion whose annualised volatility comes
from the instrument seed. Prices only move during the NORMAL session of the supplied
:class:`~agent_trader.clock.MarketClock`, are clipped to the day's price band (circuit
filter) and rounded to the tick size. A day roll (first step on a new trading day) sets
``prev_close`` to the last price, applies a small overnight gap and resets OHLC/volume.

Determinism: every instrument has its own :class:`random.Random` seeded from
``f"{seed}:{symbol}:{exchange}"`` so a run is reproducible given the same seed and the same
sequence of ``step(now)`` calls.
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from ..charges import Exchange
from ..clock import IST, MarketClock, MarketPhase
from ..instruments import UNIVERSE, InstrumentSeed
from .base import Candle, InstrumentInfo, Quote

TRADING_SECONDS_PER_YEAR = 252 * 6.25 * 3600  # 252 days × 6h15m sessions
MAX_1M_CANDLES = 3000  # ≈ 8 trading days of 1-minute history per instrument

INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": None}


def round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick


@dataclass
class _State:
    seed: InstrumentSeed
    exchange: Exchange
    rng: random.Random
    ltp: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    prev_close: Decimal
    volume: int = 0
    day: date | None = None
    last_ts: datetime | None = None
    candles: deque = field(default_factory=lambda: deque(maxlen=MAX_1M_CANDLES))
    cur: Candle | None = None  # candle being built for the current minute

    @property
    def upper(self) -> Decimal:
        return round_to_tick(self.prev_close * (100 + self.seed.band_pct) / 100, self.seed.tick_size)

    @property
    def lower(self) -> Decimal:
        return round_to_tick(self.prev_close * (100 - self.seed.band_pct) / 100, self.seed.tick_size)


class SimulatedMarketData:
    def __init__(
        self,
        clock: MarketClock,
        seeds: Iterable[InstrumentSeed] = UNIVERSE,
        *,
        seed: int = 42,
        vol_scale: float = 1.0,
        warmup_candles: int = 200,
        exchanges: tuple[Exchange, ...] = (Exchange.NSE, Exchange.BSE),
    ) -> None:
        self.clock = clock
        self.seed = seed
        self.vol_scale = vol_scale
        self._states: dict[tuple[str, Exchange], _State] = {}
        for s in seeds:
            for ex in exchanges:
                self._states[(s.symbol, ex)] = self._init_state(s, ex, warmup_candles)

    # ---- construction ---------------------------------------------------------------

    def _init_state(self, seed: InstrumentSeed, ex: Exchange, warmup: int) -> _State:
        rng = random.Random(f"{self.seed}:{seed.symbol}:{ex.value}")
        # BSE quotes carry a tiny basis versus NSE so the two listings are distinguishable.
        base = seed.ref_price if ex == Exchange.NSE else round_to_tick(seed.ref_price * Decimal("1.0004"), seed.tick_size)
        st = _State(seed=seed, exchange=ex, rng=rng, ltp=base, open=base, high=base, low=base, prev_close=base)
        now = self.clock.now()
        st.day = now.date()
        if warmup > 0:
            self._warmup(st, now, warmup)
        st.last_ts = now
        return st

    def _warmup(self, st: _State, now: datetime, n: int) -> None:
        """Synthesise ``n`` one-minute candles ending at ``now`` so history tools are non-empty."""
        start = (now - timedelta(minutes=n)).replace(second=0, microsecond=0)
        price = st.ltp
        sigma = st.seed.annual_vol * self.vol_scale
        dt = 60 / TRADING_SECONDS_PER_YEAR
        for i in range(n):
            ts = start + timedelta(minutes=i)
            o = price
            hi = lo = o
            for _ in range(4):  # four sub-steps per minute for a plausible high/low
                z = st.rng.gauss(0, 1)
                price = price * Decimal(str(math.exp(-0.5 * sigma * sigma * dt / 4 + sigma * math.sqrt(dt / 4) * z)))
                price = max(st.lower, min(st.upper, round_to_tick(price, st.seed.tick_size)))
                hi, lo = max(hi, price), min(lo, price)
            vol = int(st.rng.lognormvariate(8, 0.6))
            st.candles.append(Candle(ts=ts, open=o, high=hi, low=lo, close=price, volume=vol))
            st.volume += vol
        st.ltp = price
        st.open = st.candles[0].open if st.candles else price
        st.high = max(c.high for c in st.candles)
        st.low = min(c.low for c in st.candles)

    # ---- provider interface ---------------------------------------------------------

    def instruments(self) -> list[InstrumentInfo]:
        return [self._info(st) for st in self._states.values()]

    def instrument(self, symbol: str, exchange: Exchange) -> InstrumentInfo | None:
        st = self._states.get((symbol.upper(), exchange))
        return self._info(st) if st else None

    def quote(self, symbol: str, exchange: Exchange) -> Quote | None:
        st = self._states.get((symbol.upper(), exchange))
        return self._quote(st) if st else None

    def quotes(self, keys: list[tuple[str, Exchange]] | None = None) -> list[Quote]:
        if keys is None:
            return [self._quote(st) for st in self._states.values()]
        out = []
        for sym, ex in keys:
            st = self._states.get((sym.upper(), ex))
            if st:
                out.append(self._quote(st))
        return out

    def ohlc(self, symbol: str, exchange: Exchange, interval: str = "1m", limit: int = 100) -> list[Candle]:
        st = self._states.get((symbol.upper(), exchange))
        if not st:
            return []
        if interval not in INTERVAL_MINUTES:
            raise ValueError(f"unsupported interval {interval!r}; use one of {list(INTERVAL_MINUTES)}")
        candles = list(st.candles) + ([st.cur] if st.cur else [])
        mins = INTERVAL_MINUTES[interval]
        if mins == 1:
            return candles[-limit:]
        buckets: dict[datetime, Candle] = {}
        for c in candles:
            if mins is None:
                key = c.ts.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                key = c.ts.replace(minute=(c.ts.minute // mins) * mins, second=0, microsecond=0)
                if mins >= 60:
                    key = key.replace(minute=0)
            b = buckets.get(key)
            if b is None:
                buckets[key] = Candle(ts=key, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume)
            else:
                b.high = max(b.high, c.high)
                b.low = min(b.low, c.low)
                b.close = c.close
                b.volume += c.volume
        return list(buckets.values())[-limit:]

    def record_trade(self, symbol: str, exchange: Exchange, quantity: int) -> None:
        st = self._states.get((symbol.upper(), exchange))
        if st:
            st.volume += quantity
            if st.cur:
                st.cur.volume += quantity

    def step(self, now: datetime) -> list[Quote]:
        """Advance every instrument to ``now``. Prices move only in the NORMAL session."""
        now = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
        phase = self.clock.phase(now)
        changed: list[Quote] = []
        for st in self._states.values():
            if st.day != now.date() and self.clock.is_trading_day(now.date()):
                self._roll_day(st, now)
                changed.append(self._quote(st))
            if phase != MarketPhase.NORMAL:
                st.last_ts = now
                continue
            if self._tick(st, now):
                changed.append(self._quote(st))
        return changed

    # ---- test/admin hooks -----------------------------------------------------------

    def set_price(self, symbol: str, exchange: Exchange, price: Decimal) -> Quote:
        """Force the LTP (clipped to the band). Intended for tests and admin scenarios."""
        st = self._states[(symbol.upper(), exchange)]
        p = max(st.lower, min(st.upper, round_to_tick(Decimal(price), st.seed.tick_size)))
        st.ltp = p
        st.high, st.low = max(st.high, p), min(st.low, p)
        self._update_candle(st, self.clock.now(), p, 0)
        return self._quote(st)

    # ---- internals ------------------------------------------------------------------

    def _roll_day(self, st: _State, now: datetime) -> None:
        st.prev_close = st.ltp
        gap = Decimal(str(math.exp(st.rng.gauss(0, 0.004))))  # ~0.4% overnight gap
        p = round_to_tick(st.ltp * gap, st.seed.tick_size)
        p = max(st.lower, min(st.upper, p))
        st.ltp = st.open = st.high = st.low = p
        st.volume = 0
        st.day = now.date()
        st.cur = None

    def _tick(self, st: _State, now: datetime) -> bool:
        last = st.last_ts or now
        dt_s = max(0.0, (now - last).total_seconds())
        st.last_ts = now
        if dt_s == 0:
            return False
        dt = min(dt_s, 300.0) / TRADING_SECONDS_PER_YEAR  # cap the jump after long idle gaps
        sigma = st.seed.annual_vol * self.vol_scale
        z = st.rng.gauss(0, 1)
        factor = Decimal(str(math.exp(-0.5 * sigma * sigma * dt + sigma * math.sqrt(dt) * z)))
        p = round_to_tick(st.ltp * factor, st.seed.tick_size)
        p = max(st.lower, min(st.upper, p))
        vol = int(st.rng.lognormvariate(5, 0.8)) if dt_s < 5 else int(st.rng.lognormvariate(7, 0.8))
        st.volume += vol
        st.high, st.low = max(st.high, p), min(st.low, p)
        changed = p != st.ltp
        st.ltp = p
        self._update_candle(st, now, p, vol)
        return changed or vol > 0

    @staticmethod
    def _update_candle(st: _State, now: datetime, price: Decimal, vol: int) -> None:
        minute = now.replace(second=0, microsecond=0)
        if st.cur is None or st.cur.ts != minute:
            if st.cur is not None:
                st.candles.append(st.cur)
            st.cur = Candle(ts=minute, open=price, high=price, low=price, close=price, volume=vol)
            return
        st.cur.high = max(st.cur.high, price)
        st.cur.low = min(st.cur.low, price)
        st.cur.close = price
        st.cur.volume += vol

    @staticmethod
    def _info(st: _State) -> InstrumentInfo:
        s = st.seed
        return InstrumentInfo(
            symbol=s.symbol, exchange=st.exchange, name=s.name, sector=s.sector,
            tick_size=s.tick_size, lot_size=s.lot_size, band_pct=s.band_pct, isin=s.isin,
        )

    def _quote(self, st: _State) -> Quote:
        tick = st.seed.tick_size
        return Quote(
            symbol=st.seed.symbol, exchange=st.exchange, ltp=st.ltp,
            bid=max(st.lower, st.ltp - tick), ask=min(st.upper, st.ltp + tick),
            open=st.open, high=st.high, low=st.low, prev_close=st.prev_close,
            upper_circuit=st.upper, lower_circuit=st.lower, volume=st.volume,
            ts=st.last_ts or self.clock.now(),
        )
