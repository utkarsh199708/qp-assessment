"""Shared fixtures: a frozen IST clock, a seeded simulator, an in-memory engine and a TestClient.

Conventions for tests:
* ``clock`` starts on Wed 2026-09-16 10:00 IST (NORMAL session). Move it with ``clock.set(...)`` /
  ``clock.advance(...)`` and then call ``engine.tick()``.
* ``market.set_price(symbol, Exchange.NSE, Decimal(...))`` forces an LTP (clipped to the band).
* Use ``agent`` (a registered agent id) or ``client``/``headers`` for HTTP tests.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from agent_trader.api import create_app
from agent_trader.charges import Exchange
from agent_trader.clock import IST, MarketClock
from agent_trader.config import Settings
from agent_trader.db import Base, make_engine, make_session_factory
from agent_trader.engine import TradingEngine
from agent_trader.marketdata import SimulatedMarketData
from agent_trader.runtime import Runtime

FROZEN_AT = datetime(2026, 9, 16, 10, 0, tzinfo=IST)  # a normal trading Wednesday, mid-session


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:", clock_mode="frozen", frozen_at=FROZEN_AT, admin_api_key="admin-secret",
        sim_seed=7, sim_warmup_candles=30, _env_file=None,
    )


@pytest.fixture
def clock(settings) -> MarketClock:
    return MarketClock(mode="frozen", frozen_at=settings.frozen_at)


@pytest.fixture
def market(clock, settings) -> SimulatedMarketData:
    return SimulatedMarketData(clock, seed=settings.sim_seed, warmup_candles=settings.sim_warmup_candles)


@pytest.fixture
def engine(settings, clock, market) -> TradingEngine:
    db = make_engine(settings.database_url)
    Base.metadata.create_all(db)
    return TradingEngine(settings, clock, market, make_session_factory(db))


@pytest.fixture
def runtime(settings, clock, market, engine) -> Runtime:
    return Runtime(settings=settings, clock=clock, market=market, engine=engine)


@pytest.fixture
def registered(engine) -> tuple[str, str]:
    """(agent_id, api_key) for a fresh agent with ₹10 lakh."""
    agent, key = engine.register_agent("test-agent", description="pytest")
    return agent["agent_id"], key


@pytest.fixture
def agent(registered) -> str:
    return registered[0]


@pytest.fixture
def api_key(registered) -> str:
    return registered[1]


@pytest.fixture
def app(runtime):
    return create_app(runtime, start_loop=False, mount_mcp=True)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def headers(api_key) -> dict[str, str]:
    return {"X-API-Key": api_key}


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-Admin-Key": "admin-secret"}


def ltp(engine, symbol: str, exchange: Exchange = Exchange.NSE) -> Decimal:
    return Decimal(str(engine.quote(symbol, exchange)["ltp"]))
