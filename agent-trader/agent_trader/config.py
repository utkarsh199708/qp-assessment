"""Runtime configuration (env vars, prefix ``TRADER_``)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRADER_", env_file=".env", extra="ignore")

    # --- storage -----------------------------------------------------------------------
    database_url: str = "sqlite:///./agent_trader.db"
    """SQLAlchemy URL. ``sqlite:///:memory:`` is used by the test-suite."""

    # --- clock -------------------------------------------------------------------------
    clock_mode: Literal["real", "frozen", "always_open"] = "real"
    """``real`` follows NSE hours; ``always_open`` lets agents trade 24x7 (dev); ``frozen`` for tests."""
    frozen_at: datetime | None = None
    extra_holidays: list[date] = Field(default_factory=list)
    """Additional trading holidays (ISO dates, comma separated in the env var)."""

    # --- market data -------------------------------------------------------------------
    market_data_provider: Literal["simulator", "yfinance"] = "simulator"
    sim_seed: int = 42
    """Seed for the simulator; the same seed + same agent actions reproduce the same run."""
    sim_tick_seconds: float = 1.0
    """How often the background loop advances prices and matches resting orders."""
    sim_warmup_candles: int = 200
    """Number of synthetic 1-minute candles generated at boot so ``get_ohlc`` is non-empty."""
    sim_volatility_scale: float = 1.0
    """Multiplier on every instrument's annualised volatility (raise it to stress agents)."""

    # --- agents & auth -----------------------------------------------------------------
    admin_api_key: str | None = None
    """If set, required (``X-Admin-Key``) for admin endpoints; if unset, admin endpoints are open (dev)."""
    default_initial_cash: Decimal = Decimal("1000000")
    """₹10 lakh paper money for a newly registered agent unless it asks for a different amount."""
    max_initial_cash: Decimal = Decimal("100000000")
    open_registration: bool = True
    """When False, only the admin key can register agents."""

    # --- default risk limits (per agent, overridable at registration) ------------------
    risk_max_order_value: Decimal = Decimal("500000")
    risk_max_position_value_per_symbol: Decimal = Decimal("1000000")
    risk_max_daily_loss: Decimal = Decimal("50000")
    """Realised + unrealised loss for the day at which the agent is auto-halted."""
    risk_max_orders_per_minute: int = 60
    risk_max_open_orders: int = 100

    # --- execution ---------------------------------------------------------------------
    market_slippage_bps: int = 5
    """Market orders fill at LTP ± this many basis points (adverse), then rounded to tick."""
    max_fill_fraction_per_tick: float = 1.0
    """<1.0 makes large resting limit orders fill partially across ticks."""
    mis_leverage: Decimal = Decimal("5")
    """Intraday (MIS) leverage: margin blocked = order value / mis_leverage."""

    # --- server ------------------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    @field_validator("extra_holidays", mode="before")
    @classmethod
    def _split_holidays(cls, v):
        if isinstance(v, str):
            return [date.fromisoformat(s.strip()) for s in v.split(",") if s.strip()]
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
