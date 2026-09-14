"""Market-data provider interface.

The trading engine only ever talks to :class:`MarketDataProvider`; the default implementation
is the seedable :class:`~agent_trader.marketdata.simulator.SimulatedMarketData`. A real-data
adapter (see ``yfinance_provider.py``) plugs in behind the same interface.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from ..charges import Exchange


class InstrumentInfo(BaseModel):
    symbol: str
    exchange: Exchange
    name: str
    sector: str
    tick_size: Decimal
    lot_size: int
    band_pct: int
    isin: str | None = None


class Quote(BaseModel):
    symbol: str
    exchange: Exchange
    ltp: Decimal
    bid: Decimal
    ask: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    prev_close: Decimal
    upper_circuit: Decimal
    lower_circuit: Decimal
    volume: int
    ts: datetime

    @property
    def change(self) -> Decimal:
        return self.ltp - self.prev_close

    @property
    def change_pct(self) -> Decimal:
        if self.prev_close == 0:
            return Decimal(0)
        return (self.change / self.prev_close * 100).quantize(Decimal("0.01"))


class Candle(BaseModel):
    ts: datetime  # candle open time, IST
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@runtime_checkable
class MarketDataProvider(Protocol):
    def instruments(self) -> list[InstrumentInfo]: ...

    def instrument(self, symbol: str, exchange: Exchange) -> InstrumentInfo | None: ...

    def quote(self, symbol: str, exchange: Exchange) -> Quote | None: ...

    def quotes(self, keys: list[tuple[str, Exchange]] | None = None) -> list[Quote]: ...

    def ohlc(self, symbol: str, exchange: Exchange, interval: str = "1m", limit: int = 100) -> list[Candle]: ...

    def step(self, now: datetime) -> list[Quote]:
        """Advance the provider to ``now`` and return every quote that changed."""
        ...

    def record_trade(self, symbol: str, exchange: Exchange, quantity: int) -> None:
        """Let the provider account for volume traded on the platform (optional)."""
        ...
