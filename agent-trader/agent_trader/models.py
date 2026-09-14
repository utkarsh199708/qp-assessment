"""ORM models. Money columns are exact :class:`~decimal.Decimal` (see :class:`~agent_trader.db.Money`)."""

from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .charges import Exchange, ProductType, Side
from .db import Base, Money


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


class AgentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    HALTED = "HALTED"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"  # stop-loss limit: trigger_price + price
    SL_M = "SL-M"  # stop-loss market: trigger_price only


class Validity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"


class OrderStatus(str, Enum):
    OPEN = "OPEN"  # resting (limit not yet marketable, SL not triggered, or AMO waiting for open)
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED)


class LedgerKind(str, Enum):
    DEPOSIT = "DEPOSIT"
    BUY = "BUY"  # CNC purchase debit
    SELL = "SELL"  # CNC sale credit
    CHARGES = "CHARGES"
    MARGIN_BLOCK = "MARGIN_BLOCK"  # MIS margin moved cash -> blocked
    MARGIN_RELEASE = "MARGIN_RELEASE"
    REALISED_PNL = "REALISED_PNL"  # MIS realised P&L on close
    ADJUSTMENT = "ADJUSTMENT"


class EventType(str, Enum):
    AGENT_REGISTERED = "AGENT_REGISTERED"
    AGENT_HALTED = "AGENT_HALTED"
    AGENT_RESUMED = "AGENT_RESUMED"
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ORDER_MODIFIED = "ORDER_MODIFIED"
    ORDER_TRIGGERED = "ORDER_TRIGGERED"
    SQUARE_OFF = "SQUARE_OFF"
    TICK = "TICK"


class AgentRow(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("agt"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=AgentStatus.ACTIVE.value, nullable=False)
    halt_reason: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    initial_cash: Mapped[Decimal] = mapped_column(Money, nullable=False)
    cash: Mapped[Decimal] = mapped_column(Money, nullable=False)
    blocked_cash: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))

    # risk limits
    max_order_value: Mapped[Decimal] = mapped_column(Money, nullable=False)
    max_position_value_per_symbol: Mapped[Decimal] = mapped_column(Money, nullable=False)
    max_daily_loss: Mapped[Decimal] = mapped_column(Money, nullable=False)
    max_orders_per_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    max_open_orders: Mapped[int] = mapped_column(Integer, nullable=False)

    # daily loss tracking
    day_start_date: Mapped[str | None] = mapped_column(String(10))  # ISO date (IST)
    day_start_equity: Mapped[Decimal | None] = mapped_column(Money)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    orders: Mapped[list[OrderRow]] = relationship(back_populates="agent")
    positions: Mapped[list[PositionRow]] = relationship(back_populates="agent")


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("agent_id", "client_order_id", name="uq_agent_client_order"),
        Index("ix_orders_agent_created", "agent_id", "created_at"),
        Index("ix_orders_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ord"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    client_order_id: Mapped[str | None] = mapped_column(String(64))

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False)
    product: Mapped[str] = mapped_column(String(4), nullable=False)
    validity: Mapped[str] = mapped_column(String(4), nullable=False, default=Validity.DAY.value)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price: Mapped[Decimal | None] = mapped_column(Money)  # limit price
    trigger_price: Mapped[Decimal | None] = mapped_column(Money)
    triggered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(Text)
    average_price: Mapped[Decimal | None] = mapped_column(Money)
    charges: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    charges_breakdown: Mapped[dict | None] = mapped_column(JSON)
    blocked_cash: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))

    reasoning: Mapped[str | None] = mapped_column(Text)  # agent's thesis, kept for audit
    tag: Mapped[str | None] = mapped_column(String(64))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # auto square-off etc.

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    agent: Mapped[AgentRow] = relationship(back_populates="orders")
    trades: Mapped[list[TradeRow]] = relationship(back_populates="order")

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_quantity


class TradeRow(Base):
    __tablename__ = "trades"
    __table_args__ = (Index("ix_trades_agent_ts", "agent_id", "executed_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("trd"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    product: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    charges: Mapped[Decimal] = mapped_column(Money, nullable=False)
    realised_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    order: Mapped[OrderRow] = relationship(back_populates="trades")


class PositionRow(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("agent_id", "symbol", "exchange", "product", name="uq_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    product: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # signed; <0 = short (MIS)
    average_price: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    realised_pnl: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    margin_blocked: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))  # MIS only
    buy_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sell_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    agent: Mapped[AgentRow] = relationship(back_populates="positions")


class LedgerRow(Base):
    __tablename__ = "ledger"
    __table_args__ = (Index("ix_ledger_agent_ts", "agent_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)  # signed effect on free cash
    cash_after: Mapped[Decimal] = mapped_column(Money, nullable=False)
    blocked_after: Mapped[Decimal] = mapped_column(Money, nullable=False)
    ref_id: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_agent_id_id", "agent_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (Index("ix_snap_agent_ts", "agent_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    cash: Mapped[Decimal] = mapped_column(Money, nullable=False)


__all__ = [
    "AgentRow", "AgentStatus", "EquitySnapshotRow", "EventRow", "EventType", "Exchange", "LedgerKind",
    "LedgerRow", "OrderRow", "OrderStatus", "OrderType", "PositionRow", "ProductType", "Side", "TradeRow",
    "Validity", "new_id",
]
