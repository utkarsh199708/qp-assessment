"""Request/response models for the REST API (OpenAPI is generated from these)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

ExchangeStr = Literal["NSE", "BSE"]
SideStr = Literal["BUY", "SELL"]
OrderTypeStr = Literal["MARKET", "LIMIT", "SL", "SL-M"]
ProductStr = Literal["CNC", "MIS"]
ValidityStr = Literal["DAY", "IOC"]


class RiskLimits(BaseModel):
    max_order_value: Decimal | None = Field(None, description="Max ₹ value of a single order")
    max_position_value_per_symbol: Decimal | None = Field(None, description="Max ₹ exposure in one symbol")
    max_daily_loss: Decimal | None = Field(None, description="Loss for the day (₹) at which the agent is auto-halted")
    max_orders_per_minute: int | None = None
    max_open_orders: int | None = None


class RegisterAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="Agent name (shown on the leaderboard)")
    initial_cash: Decimal | None = Field(None, description="Opening paper-money balance in ₹ (default ₹10,00,000)")
    description: str | None = Field(None, description="What this agent does / which model drives it")
    metadata: dict[str, Any] | None = None
    risk_limits: RiskLimits | None = None


class RegisterAgentResponse(BaseModel):
    agent: dict[str, Any]
    api_key: str = Field(..., description="Shown once. Send as X-API-Key on every later request.")


class PlaceOrderRequest(BaseModel):
    symbol: str = Field(..., description="NSE/BSE trading symbol, e.g. RELIANCE")
    side: SideStr
    quantity: int = Field(..., ge=1)
    exchange: ExchangeStr = "NSE"
    order_type: OrderTypeStr = Field("MARKET", description="MARKET, LIMIT (needs price), SL (needs price+trigger_price), SL-M (needs trigger_price)")
    product: ProductStr = Field("CNC", description="CNC = delivery (no shorting). MIS = intraday, 5x leverage, shorting allowed, auto squared-off 15:20 IST")
    validity: ValidityStr = "DAY"
    price: Decimal | None = Field(None, description="Limit price (multiple of tick size 0.05)")
    trigger_price: Decimal | None = Field(None, description="Stop trigger for SL / SL-M")
    client_order_id: str | None = Field(None, max_length=64, description="Idempotency key: re-sending the same id returns the same order")
    reasoning: str | None = Field(None, description="Why the agent is placing this order (kept in the audit trail)")
    tag: str | None = Field(None, max_length=64)


class ModifyOrderRequest(BaseModel):
    quantity: int | None = Field(None, ge=1)
    price: Decimal | None = None
    trigger_price: Decimal | None = None


class HaltRequest(BaseModel):
    reason: str = Field("halted by agent", max_length=500)


class DepositRequest(BaseModel):
    amount: Decimal = Field(..., gt=0)
    note: str | None = None


class SetPriceRequest(BaseModel):
    symbol: str
    exchange: ExchangeStr = "NSE"
    price: Decimal


class ClockRequest(BaseModel):
    set: str | None = Field(None, description="ISO-8601 datetime (IST if naive) — frozen clock only")
    advance_seconds: float | None = Field(None, description="Advance the frozen clock by this many seconds")
    ticks: int = Field(0, ge=0, le=10_000, description="Run this many engine ticks after moving the clock")


class ErrorResponse(BaseModel):
    error: str
    message: str
    hint: str | None = None
    details: dict[str, Any] | None = None
