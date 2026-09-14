"""Optional real-quote adapter backed by ``yfinance`` (``pip install agent-trader[realdata]``).

Quotes are delayed and best-effort; the platform still *simulates* execution against them.
Select with ``TRADER_MARKET_DATA_PROVIDER=yfinance``. Candles are built from the polled
quotes, so ``get_ohlc`` only has history from process start.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..charges import Exchange
from ..clock import MarketClock
from ..instruments import UNIVERSE, InstrumentSeed
from .base import Candle, InstrumentInfo, Quote
from .simulator import SimulatedMarketData, round_to_tick

SUFFIX = {Exchange.NSE: ".NS", Exchange.BSE: ".BO"}


class YFinanceMarketData(SimulatedMarketData):
    """Reuses the simulator's state/candle bookkeeping but sources prices from Yahoo Finance."""

    def __init__(self, clock: MarketClock, seeds=UNIVERSE, *, refresh_seconds: float = 15.0, **kw) -> None:
        try:
            import yfinance  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("yfinance is not installed; pip install 'agent-trader[realdata]'") from e
        super().__init__(clock, seeds, warmup_candles=0, exchanges=(Exchange.NSE,), **kw)
        self.refresh_seconds = refresh_seconds
        self._last_refresh: datetime | None = None

    def step(self, now: datetime) -> list[Quote]:
        if self._last_refresh and (now - self._last_refresh).total_seconds() < self.refresh_seconds:
            return []
        import yfinance as yf

        self._last_refresh = now
        changed: list[Quote] = []
        tickers = yf.Tickers(" ".join(f"{sym}{SUFFIX[ex]}" for sym, ex in self._states))
        for (sym, ex), st in self._states.items():
            try:
                fi = tickers.tickers[f"{sym}{SUFFIX[ex]}"].fast_info
                ltp = Decimal(str(fi["last_price"]))
                prev = Decimal(str(fi.get("previous_close") or ltp))
            except Exception:  # network hiccup: keep the last quote
                continue
            tick = st.seed.tick_size
            st.prev_close = round_to_tick(prev, tick)
            p = round_to_tick(ltp, tick)
            if st.day != now.date():
                st.open = st.high = st.low = p
                st.volume = 0
                st.day = now.date()
            st.high, st.low = max(st.high, p), min(st.low, p)
            st.ltp = p
            st.last_ts = now
            self._update_candle(st, now, p, 0)
            changed.append(self._quote(st))
        return changed
