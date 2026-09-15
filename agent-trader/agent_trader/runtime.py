"""Builds the engine from settings and runs the background tick loop.

One :class:`Runtime` per process is shared by the REST app and the MCP server.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .clock import DEFAULT_HOLIDAYS, MarketClock
from .config import Settings, get_settings
from .db import Base, make_engine, make_session_factory
from .engine import TradingEngine
from .marketdata import SimulatedMarketData
from .marketdata.base import MarketDataProvider

log = logging.getLogger("agent_trader.runtime")


class TickLoop(threading.Thread):
    def __init__(self, engine: TradingEngine, interval: float) -> None:
        super().__init__(name="agent-trader-tick", daemon=True)
        self.engine = engine
        self.interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.engine.tick()
            except Exception:  # keep the loop alive; the error is logged
                log.exception("tick failed")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.05, self.interval - elapsed))

    def stop(self) -> None:
        self._stop.set()


@dataclass
class Runtime:
    settings: Settings
    clock: MarketClock
    market: MarketDataProvider
    engine: TradingEngine
    loop: TickLoop | None = None

    def start(self) -> None:
        if self.loop is None and self.settings.clock_mode != "frozen":
            self.loop = TickLoop(self.engine, self.settings.sim_tick_seconds)
            self.loop.start()
            log.info(
                "tick loop started (every %.2fs, clock=%s)",
                self.settings.sim_tick_seconds,
                self.settings.clock_mode,
            )

    def stop(self) -> None:
        if self.loop:
            self.loop.stop()
            self.loop.join(timeout=5)
            self.loop = None


def build_market(settings: Settings, clock: MarketClock) -> MarketDataProvider:
    if settings.market_data_provider == "yfinance":
        from .marketdata.yfinance_provider import YFinanceMarketData

        return YFinanceMarketData(clock)
    return SimulatedMarketData(
        clock,
        seed=settings.sim_seed,
        vol_scale=settings.sim_volatility_scale,
        warmup_candles=settings.sim_warmup_candles,
    )


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in (
        "httpx",
        "httpx2",
        "httpcore",
        "mcp.server.streamable_http",
        "mcp.server.streamable_http_manager",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    holidays = DEFAULT_HOLIDAYS | frozenset(settings.extra_holidays)
    clock = MarketClock(mode=settings.clock_mode, frozen_at=settings.frozen_at, holidays=holidays)
    market = build_market(settings, clock)
    db = make_engine(settings.database_url)
    Base.metadata.create_all(db)
    engine = TradingEngine(settings, clock, market, make_session_factory(db))
    return Runtime(settings=settings, clock=clock, market=market, engine=engine)


_runtime: Runtime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> Runtime:
    """Process-wide runtime (lazily built from env settings)."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = build_runtime()
        return _runtime


def set_runtime(rt: Runtime | None) -> None:
    global _runtime
    with _runtime_lock:
        _runtime = rt
