"""The trading engine: agents, risk checks, order lifecycle, fills, positions, ledger.

Single-process design: every public method takes ``self.lock`` and runs inside one DB
session, so FastAPI worker threads, the MCP server and the background tick loop can all
call it safely. Prices come from the pluggable market-data provider; execution is simulated
against its quotes.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import math
import secrets
import threading
from collections import deque
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..charges import ChargeSchedule, Exchange, ProductType, Side, compute_charges, to_paise
from ..clock import IST, NORMAL_END, MarketClock, MarketPhase
from ..config import Settings
from ..marketdata.base import MarketDataProvider, Quote
from ..marketdata.simulator import round_to_tick
from ..models import (
    AgentRow,
    AgentStatus,
    EquitySnapshotRow,
    EventRow,
    EventType,
    LedgerKind,
    LedgerRow,
    OrderRow,
    OrderStatus,
    OrderType,
    PositionRow,
    TradeRow,
    Validity,
)
from .errors import (
    AgentHalted,
    Conflict,
    InvalidRequest,
    NotFound,
    OrderRejected,
    RateLimited,
    TradingError,
    Unauthorized,
)
from .events import Event, EventBus
from .positions import apply_fill
from .serialize import (
    candle_to_dict,
    instrument_to_dict,
    ledger_to_dict,
    num,
    order_to_dict,
    position_to_dict,
    quote_to_dict,
    trade_to_dict,
    ts,
)

log = logging.getLogger("agent_trader.engine")

ZERO = Decimal("0")
PAISE = Decimal("0.01")
OPEN_STATUSES = (OrderStatus.OPEN.value, OrderStatus.PARTIALLY_FILLED.value)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class TradingEngine:
    def __init__(
        self, settings: Settings, clock: MarketClock, market: MarketDataProvider, session_factory
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.market = market
        self.session_factory = session_factory
        self.lock = threading.RLock()
        self.bus = EventBus()
        self.charge_schedule = ChargeSchedule()
        self.mis_leverage = Decimal(str(settings.mis_leverage))
        self._order_times: dict[str, deque[datetime]] = {}
        self._squared_off_on: date | None = None
        self._expired_on: date | None = None
        self._last_snapshot_at: datetime | None = None
        self._last_risk_check_at: datetime | None = None
        self.tick_count = 0

    # =====================================================================================
    # helpers
    # =====================================================================================

    def _session(self) -> Session:
        return self.session_factory()

    def _now(self) -> datetime:
        return self.clock.now()

    def _emit(
        self,
        s: Session,
        type_: EventType,
        agent_id: str | None,
        payload: dict[str, Any],
        *,
        persist: bool = True,
    ) -> Event:
        now = self._now()
        if persist:
            s.add(EventRow(ts=now, agent_id=agent_id, type=type_.value, payload=payload))
        return self.bus.publish(now, type_.value, agent_id, payload)

    def _ledger(
        self,
        s: Session,
        agent: AgentRow,
        kind: LedgerKind,
        amount: Decimal,
        ref_id: str | None,
        note: str | None = None,
    ) -> None:
        s.add(
            LedgerRow(
                agent_id=agent.id,
                ts=self._now(),
                kind=kind.value,
                amount=to_paise(amount),
                cash_after=agent.cash,
                blocked_after=agent.blocked_cash,
                ref_id=ref_id,
                note=note,
            )
        )

    def _get_agent(self, s: Session, agent_id: str) -> AgentRow:
        agent = s.get(AgentRow, agent_id)
        if agent is None:
            raise NotFound(f"agent {agent_id} not found")
        return agent

    @staticmethod
    def parse_symbol(symbol: str, exchange: Exchange | str = Exchange.NSE) -> tuple[str, Exchange]:
        """Accept ``"INFY"`` + exchange or the exchange-qualified form ``"NSE:INFY"`` / ``"BSE:INFY"``."""
        symbol = (symbol or "").strip().upper()
        if ":" in symbol:
            prefix, _, rest = symbol.partition(":")
            if prefix not in Exchange.__members__:
                raise InvalidRequest(
                    f"unknown exchange prefix {prefix!r} in {symbol!r}", hint="Use NSE:SYMBOL or BSE:SYMBOL."
                )
            return rest, Exchange(prefix)
        try:
            return symbol, Exchange(exchange)
        except ValueError as e:
            raise InvalidRequest(f"unknown exchange {exchange!r}", hint="Use NSE or BSE.") from e

    def _quote_or_raise(self, symbol: str, exchange: Exchange) -> Quote:
        q = self.market.quote(symbol, exchange)
        if q is None:
            universe = [i.symbol for i in self.market.instruments() if i.exchange == exchange]
            close = difflib.get_close_matches(symbol, universe, n=3, cutoff=0.6)
            hint = "Call search_instruments to find valid symbols (e.g. RELIANCE, TCS, INFY)."
            if close:
                hint = f"Did you mean {', '.join(close)}? " + hint
            raise NotFound(
                f"unknown instrument {symbol} on {exchange.value}", hint=hint, details={"did_you_mean": close}
            )
        return q

    def _expiry_for(self, now: datetime) -> datetime:
        """DAY orders expire at 15:30 IST of the session they belong to (AMO → next session)."""
        if self.clock.mode == "always_open":
            return now + timedelta(days=365)
        d = now.date()
        if self.clock.is_trading_day(d) and now.time() < NORMAL_END:
            return datetime.combine(d, NORMAL_END, tzinfo=IST)
        return datetime.combine(self.clock.next_trading_day(d), NORMAL_END, tzinfo=IST)

    # =====================================================================================
    # agents
    # =====================================================================================

    def register_agent(
        self,
        name: str,
        *,
        initial_cash: Decimal | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
        risk: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        st = self.settings
        cash = Decimal(str(initial_cash)) if initial_cash is not None else st.default_initial_cash
        if cash <= 0 or cash > st.max_initial_cash:
            raise InvalidRequest(
                f"initial_cash must be between 1 and {st.max_initial_cash}",
                details={"max_initial_cash": num(st.max_initial_cash)},
            )
        if not name or len(name) > 120:
            raise InvalidRequest("name is required (max 120 chars)")
        risk = risk or {}
        api_key = "atk_" + secrets.token_urlsafe(24)
        with self.lock, self._session() as s:
            now = self._now()
            agent = AgentRow(
                name=name,
                api_key_hash=_hash_key(api_key),
                description=description,
                metadata_=metadata,
                initial_cash=cash,
                cash=cash,
                blocked_cash=ZERO,
                max_order_value=Decimal(str(risk.get("max_order_value", st.risk_max_order_value))),
                max_position_value_per_symbol=Decimal(
                    str(risk.get("max_position_value_per_symbol", st.risk_max_position_value_per_symbol))
                ),
                max_daily_loss=Decimal(str(risk.get("max_daily_loss", st.risk_max_daily_loss))),
                max_orders_per_minute=int(risk.get("max_orders_per_minute", st.risk_max_orders_per_minute)),
                max_open_orders=int(risk.get("max_open_orders", st.risk_max_open_orders)),
                day_start_date=now.date().isoformat(),
                day_start_equity=cash,
                created_at=now,
                last_seen_at=now,
            )
            s.add(agent)
            s.flush()
            self._ledger(s, agent, LedgerKind.DEPOSIT, cash, None, "initial paper-money deposit")
            self._emit(s, EventType.AGENT_REGISTERED, agent.id, {"name": name, "initial_cash": num(cash)})
            s.commit()
            return self.agent_to_dict(agent), api_key

    def authenticate(self, api_key: str | None) -> dict[str, Any]:
        if not api_key:
            raise Unauthorized(
                "missing API key", hint="Send the key from register_agent in the X-API-Key header."
            )
        with self._session() as s:
            agent = s.scalar(select(AgentRow).where(AgentRow.api_key_hash == _hash_key(api_key)))
            if agent is None:
                raise Unauthorized("invalid API key")
            agent.last_seen_at = self._now()
            s.commit()
            return self.agent_to_dict(agent)

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        with self._session() as s:
            return self.agent_to_dict(self._get_agent(s, agent_id))

    def list_agents(self) -> list[dict[str, Any]]:
        with self._session() as s:
            return [self.agent_to_dict(a) for a in s.scalars(select(AgentRow).order_by(AgentRow.created_at))]

    def update_risk_limits(self, agent_id: str, risk: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "max_order_value",
            "max_position_value_per_symbol",
            "max_daily_loss",
            "max_orders_per_minute",
            "max_open_orders",
        }
        bad = set(risk) - allowed
        if bad:
            raise InvalidRequest(f"unknown risk fields: {sorted(bad)}", details={"allowed": sorted(allowed)})
        with self.lock, self._session() as s:
            agent = self._get_agent(s, agent_id)
            for k, v in risk.items():
                if k in ("max_orders_per_minute", "max_open_orders"):
                    setattr(agent, k, int(v))
                else:
                    setattr(agent, k, Decimal(str(v)))
            s.commit()
            return self.agent_to_dict(agent)

    def halt_agent(self, agent_id: str, reason: str, *, cancel_orders: bool = True) -> dict[str, Any]:
        with self.lock, self._session() as s:
            agent = self._get_agent(s, agent_id)
            self._halt(s, agent, reason, cancel_orders=cancel_orders)
            s.commit()
            return self.agent_to_dict(agent)

    def resume_agent(self, agent_id: str) -> dict[str, Any]:
        with self.lock, self._session() as s:
            agent = self._get_agent(s, agent_id)
            agent.status = AgentStatus.ACTIVE.value
            agent.halt_reason = None
            self._emit(s, EventType.AGENT_RESUMED, agent.id, {})
            s.commit()
            return self.agent_to_dict(agent)

    def deposit(self, agent_id: str, amount: Decimal | float, note: str | None = None) -> dict[str, Any]:
        """Admin: add paper money to an agent's account."""
        amt = to_paise(Decimal(str(amount)))
        if amt <= 0:
            raise InvalidRequest("amount must be positive")
        with self.lock, self._session() as s:
            agent = self._get_agent(s, agent_id)
            agent.cash += amt
            agent.initial_cash += amt  # keeps return % meaningful after top-ups
            if agent.day_start_equity is not None:
                agent.day_start_equity += amt
            self._ledger(s, agent, LedgerKind.DEPOSIT, amt, None, note or "admin deposit")
            s.commit()
            return self.agent_to_dict(agent)

    def _halt(self, s: Session, agent: AgentRow, reason: str, *, cancel_orders: bool) -> None:
        if agent.status == AgentStatus.HALTED.value:
            return
        agent.status = AgentStatus.HALTED.value
        agent.halt_reason = reason
        cancelled = []
        if cancel_orders:
            for o in self._open_orders(s, agent.id):
                self._cancel(s, agent, o, f"agent halted: {reason}")
                cancelled.append(o.id)
        self._emit(s, EventType.AGENT_HALTED, agent.id, {"reason": reason, "cancelled_orders": cancelled})
        log.warning("agent %s halted: %s", agent.id, reason)

    def agent_to_dict(self, a: AgentRow) -> dict[str, Any]:
        return {
            "agent_id": a.id,
            "name": a.name,
            "status": a.status,
            "halt_reason": a.halt_reason,
            "description": a.description,
            "metadata": a.metadata_,
            "cash": num(a.cash),
            "blocked_cash": num(a.blocked_cash),
            "initial_cash": num(a.initial_cash),
            "risk_limits": {
                "max_order_value": num(a.max_order_value),
                "max_position_value_per_symbol": num(a.max_position_value_per_symbol),
                "max_daily_loss": num(a.max_daily_loss),
                "max_orders_per_minute": a.max_orders_per_minute,
                "max_open_orders": a.max_open_orders,
            },
            "created_at": ts(a.created_at),
        }

    # =====================================================================================
    # market data
    # =====================================================================================

    def market_status(self) -> dict[str, Any]:
        st = self.clock.status()
        st["provider"] = type(self.market).__name__
        st["instrument_count"] = len(self.market.instruments())
        return st

    def instruments(
        self, query: str | None = None, exchange: Exchange | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        out = []
        q = (query or "").strip().upper()
        for i in self.market.instruments():
            if exchange and i.exchange != exchange:
                continue
            if q and q not in i.symbol.upper() and q not in i.name.upper() and q not in i.sector.upper():
                continue
            out.append(instrument_to_dict(i))
            if len(out) >= limit:
                break
        return out

    def quote(self, symbol: str, exchange: Exchange | str = Exchange.NSE) -> dict[str, Any]:
        symbol, exchange = self.parse_symbol(symbol, exchange)
        return quote_to_dict(self._quote_or_raise(symbol, exchange))

    def quotes(
        self, symbols: list[str] | None = None, exchange: Exchange | str = Exchange.NSE
    ) -> list[dict[str, Any]]:
        exchange = Exchange(exchange)
        if symbols is None:
            return [quote_to_dict(q) for q in self.market.quotes() if q.exchange == exchange]
        return [
            quote_to_dict(q)
            for q in self.market.quotes([self.parse_symbol(sym, exchange) for sym in symbols])
        ]

    def ohlc(
        self, symbol: str, exchange: Exchange | str = Exchange.NSE, interval: str = "1m", limit: int = 100
    ) -> list[dict[str, Any]]:
        symbol, exchange = self.parse_symbol(symbol, exchange)
        self._quote_or_raise(symbol, exchange)
        try:
            candles = self.market.ohlc(symbol, exchange, interval, max(1, min(limit, 1000)))
        except ValueError as e:
            raise InvalidRequest(str(e)) from e
        return [candle_to_dict(c) for c in candles]

    # =====================================================================================
    # orders
    # =====================================================================================

    def place_order(
        self,
        agent_id: str,
        *,
        symbol: str,
        side: Side | str,
        quantity: int,
        exchange: Exchange | str = Exchange.NSE,
        order_type: OrderType | str = OrderType.MARKET,
        product: ProductType | str = ProductType.CNC,
        validity: Validity | str = Validity.DAY,
        price: Decimal | float | None = None,
        trigger_price: Decimal | float | None = None,
        client_order_id: str | None = None,
        reasoning: str | None = None,
        tag: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Validate, risk-check and (unless ``dry_run``) accept an order, executing it immediately if marketable."""
        request = {
            "symbol": symbol,
            "exchange": str(getattr(exchange, "value", exchange)),
            "side": str(getattr(side, "value", side)),
            "quantity": quantity,
            "order_type": str(getattr(order_type, "value", order_type)),
            "product": str(getattr(product, "value", product)),
            "validity": str(getattr(validity, "value", validity)),
            "price": None if price is None else float(price),
            "trigger_price": None if trigger_price is None else float(trigger_price),
            "client_order_id": client_order_id,
            "reasoning": reasoning,
            "tag": tag,
        }
        try:
            return self._place_order(agent_id, request, dry_run=dry_run)
        except TradingError as e:
            if not dry_run:
                self._record_rejection(agent_id, request, e)
            raise

    def _record_rejection(self, agent_id: str, request: dict[str, Any], err: TradingError) -> None:
        """Rejected orders never become order rows, but they are part of the audit trail."""
        try:
            with self.lock, self._session() as s:
                self._emit(s, EventType.ORDER_REJECTED, agent_id, {"request": request, **err.to_dict()})
                s.commit()
        except Exception:  # auditing must never mask the original error
            log.exception("failed to record rejection")

    def _place_order(self, agent_id: str, req: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        symbol, exchange = self.parse_symbol(req["symbol"], req["exchange"])
        try:
            side = Side(req["side"].upper())
            order_type = OrderType(req["order_type"].upper())
            product = ProductType(req["product"].upper())
            validity = Validity(req["validity"].upper())
        except ValueError as e:
            raise InvalidRequest(
                str(e),
                hint="side: BUY|SELL; order_type: MARKET|LIMIT|SL|SL-M; product: CNC|MIS; validity: DAY|IOC.",
            ) from e
        quantity = req["quantity"]
        price = req["price"]
        trigger_price = req["trigger_price"]
        client_order_id, reasoning, tag = req["client_order_id"], req["reasoning"], req["tag"]
        if not isinstance(quantity, int) or isinstance(quantity, bool):
            raise InvalidRequest("quantity must be an integer number of shares")
        price_d = Decimal(str(price)) if price is not None else None
        trig_d = Decimal(str(trigger_price)) if trigger_price is not None else None

        with self.lock, self._session() as s:
            agent = self._get_agent(s, agent_id)
            now = self._now()

            # idempotency: same client_order_id → return the existing order (or 409 if different)
            if client_order_id:
                existing = s.scalar(
                    select(OrderRow).where(
                        OrderRow.agent_id == agent.id, OrderRow.client_order_id == client_order_id
                    )
                )
                if existing is not None:
                    same = (
                        existing.symbol == symbol
                        and existing.exchange == exchange.value
                        and existing.side == side.value
                        and existing.quantity == quantity
                        and existing.order_type == order_type.value
                        and existing.product == product.value
                        and existing.price == price_d
                        and existing.trigger_price == trig_d
                    )
                    if same:
                        d = order_to_dict(existing)
                        d["idempotent_replay"] = True
                        return d
                    raise Conflict(
                        f"client_order_id {client_order_id!r} was already used for a different order",
                        hint="Use a fresh client_order_id per distinct order.",
                        details={"existing_order_id": existing.id},
                    )

            self._check_daily_loss(s, agent, now)
            if agent.status != AgentStatus.ACTIVE.value:
                raise AgentHalted(
                    f"agent is halted: {agent.halt_reason}",
                    hint="No new orders are accepted while halted. Existing orders can still be cancelled. "
                    "An admin (or the next trading day's reset) can resume the agent.",
                )

            quote = self._quote_or_raise(symbol, exchange)
            info = self.market.instrument(symbol, exchange)
            assert info is not None
            tick = info.tick_size

            # --- static validation ---------------------------------------------------
            if quantity <= 0:
                raise InvalidRequest("quantity must be a positive integer")
            if quantity % info.lot_size:
                raise InvalidRequest(f"quantity must be a multiple of lot size {info.lot_size}")

            needs_price = order_type in (OrderType.LIMIT, OrderType.SL)
            needs_trigger = order_type in (OrderType.SL, OrderType.SL_M)
            if needs_price and price_d is None:
                raise InvalidRequest(f"{order_type.value} orders require price")
            if not needs_price and price_d is not None:
                raise InvalidRequest(
                    f"{order_type.value} orders must not carry price",
                    hint="Omit price, or use order_type=LIMIT.",
                )
            if needs_trigger and trig_d is None:
                raise InvalidRequest(f"{order_type.value} orders require trigger_price")
            if not needs_trigger and trig_d is not None:
                raise InvalidRequest(
                    f"{order_type.value} orders must not carry trigger_price",
                    hint="Use order_type=SL or SL-M for stop orders.",
                )

            for label, v in (("price", price_d), ("trigger_price", trig_d)):
                if v is None:
                    continue
                if v <= 0:
                    raise InvalidRequest(f"{label} must be positive")
                if round_to_tick(v, tick) != v:
                    raise InvalidRequest(
                        f"{label} {v} is not a multiple of tick size {tick}",
                        hint=f"Round to {tick}: e.g. {round_to_tick(v, tick)}",
                        details={"tick_size": num(tick), "suggested": num(round_to_tick(v, tick))},
                    )
                if not (quote.lower_circuit <= v <= quote.upper_circuit):
                    raise OrderRejected(
                        f"{label} {v} is outside today's price band {quote.lower_circuit}–{quote.upper_circuit}",
                        hint="Orders outside the circuit band are rejected by the exchange; choose a price inside the band.",
                        details={
                            "lower_circuit": num(quote.lower_circuit),
                            "upper_circuit": num(quote.upper_circuit),
                        },
                    )

            if order_type in (OrderType.SL, OrderType.SL_M):
                if side == Side.BUY and trig_d <= quote.ltp:
                    raise InvalidRequest(
                        f"BUY stop trigger {trig_d} must be above the last price {quote.ltp}",
                        hint="A BUY stop triggers when price rises to trigger_price. To buy now, use MARKET/LIMIT.",
                    )
                if side == Side.SELL and trig_d >= quote.ltp:
                    raise InvalidRequest(
                        f"SELL stop trigger {trig_d} must be below the last price {quote.ltp}",
                        hint="A SELL stop triggers when price falls to trigger_price. To sell now, use MARKET/LIMIT.",
                    )
                if order_type == OrderType.SL:
                    if side == Side.BUY and price_d < trig_d:
                        raise InvalidRequest("for a BUY SL order, price must be >= trigger_price")
                    if side == Side.SELL and price_d > trig_d:
                        raise InvalidRequest("for a SELL SL order, price must be <= trigger_price")

            # --- session rules ----------------------------------------------------------
            phase = self.clock.phase(now)
            if validity == Validity.IOC and phase != MarketPhase.NORMAL:
                raise OrderRejected(
                    "IOC orders need an open market",
                    hint=f"Market phase is {phase.value}; use validity=DAY (queued until open at {self.clock.next_open(now).isoformat()}).",
                )
            if product == ProductType.MIS and self.clock.is_past_square_off(now):
                raise OrderRejected(
                    "MIS orders are not accepted after 15:20 IST (intraday square-off window)",
                    hint="Use product=CNC for delivery, or place the MIS order after market close as an AMO for the next session.",
                )

            # --- risk checks ------------------------------------------------------------
            self._check_rate_limit(agent, now)
            open_count = s.scalar(
                select(func.count())
                .select_from(OrderRow)
                .where(OrderRow.agent_id == agent.id, OrderRow.status.in_(OPEN_STATUSES))
            )
            if open_count >= agent.max_open_orders:
                raise OrderRejected(
                    f"open order limit reached ({agent.max_open_orders})",
                    hint="Cancel resting orders before placing new ones.",
                    details={"open_orders": open_count},
                )

            ref_price = self._reference_price(order_type, side, price_d, trig_d, quote)
            order_value = to_paise(ref_price * quantity)
            if order_value > agent.max_order_value:
                raise OrderRejected(
                    f"order value ₹{order_value} exceeds max_order_value ₹{agent.max_order_value}",
                    hint="Reduce quantity or raise the limit via update_risk_limits.",
                    details={"order_value": num(order_value), "max_order_value": num(agent.max_order_value)},
                )

            pos = self._get_position(s, agent.id, symbol, exchange, product)
            cur_qty = pos.quantity if pos else 0
            signed = quantity if side == Side.BUY else -quantity
            new_qty = cur_qty + signed
            if abs(new_qty) > abs(cur_qty):
                projected = to_paise(quote.ltp * abs(new_qty))
                if projected > agent.max_position_value_per_symbol:
                    raise OrderRejected(
                        f"projected position value ₹{projected} in {symbol} exceeds max_position_value_per_symbol ₹{agent.max_position_value_per_symbol}",
                        details={
                            "projected_position_value": num(projected),
                            "limit": num(agent.max_position_value_per_symbol),
                        },
                    )

            if product == ProductType.CNC and side == Side.SELL:
                committed = self._committed_sell_qty(s, agent.id, symbol, exchange)
                available = cur_qty - committed
                if quantity > available:
                    raise OrderRejected(
                        f"cannot sell {quantity} {symbol} CNC: holdings {cur_qty}, already committed to open sell orders {committed}",
                        hint="Short selling is only allowed intraday: use product=MIS. Otherwise reduce quantity.",
                        details={"holdings": cur_qty, "committed": committed, "available": max(0, available)},
                    )

            est_charges = compute_charges(
                side=side,
                product=product,
                exchange=exchange,
                quantity=quantity,
                price=ref_price,
                schedule=self.charge_schedule,
            ).total
            block = self._required_block(product, side, quantity, ref_price, cur_qty, est_charges)
            if block > agent.cash:
                raise OrderRejected(
                    f"insufficient funds: need ₹{block} (incl. est. charges ₹{est_charges}), free cash ₹{agent.cash}",
                    hint="Reduce quantity, cancel resting BUY orders to unblock cash, or use MIS which needs only "
                    f"{(100 / self.mis_leverage):.0f}% margin.",
                    details={
                        "required": num(block),
                        "available_cash": num(agent.cash),
                        "blocked_cash": num(agent.blocked_cash),
                    },
                )

            if dry_run:
                return {
                    "dry_run": True,
                    "would_be_accepted": True,
                    "symbol": symbol,
                    "exchange": exchange.value,
                    "side": side.value,
                    "quantity": quantity,
                    "order_type": order_type.value,
                    "product": product.value,
                    "validity": validity.value,
                    "reference_price": num(to_paise(ref_price)),
                    "order_value": num(order_value),
                    "estimated_charges": num(est_charges),
                    "cash_to_block": num(block),
                    "free_cash_after_block": num(agent.cash - block),
                    "would_execute_now": phase == MarketPhase.NORMAL
                    and order_type in (OrderType.MARKET,)
                    or (
                        order_type == OrderType.LIMIT
                        and phase == MarketPhase.NORMAL
                        and (
                            (side == Side.BUY and quote.ask <= price_d)
                            or (side == Side.SELL and quote.bid >= price_d)
                        )
                    ),
                    "market_phase": phase.value,
                    "ltp": num(quote.ltp),
                    "bid": num(quote.bid),
                    "ask": num(quote.ask),
                }

            # --- accept -----------------------------------------------------------------
            order = OrderRow(
                agent_id=agent.id,
                client_order_id=client_order_id,
                symbol=symbol,
                exchange=exchange.value,
                side=side.value,
                order_type=order_type.value,
                product=product.value,
                validity=validity.value,
                quantity=quantity,
                price=price_d,
                trigger_price=trig_d,
                status=OrderStatus.OPEN.value,
                blocked_cash=block,
                reasoning=reasoning,
                tag=tag,
                created_at=now,
                updated_at=now,
                expires_at=self._expiry_for(now),
            )
            s.add(order)
            s.flush()
            if block > 0:
                agent.cash -= block
                agent.blocked_cash += block
                self._ledger(s, agent, LedgerKind.MARGIN_BLOCK, -block, order.id, "blocked for order")
            self._order_times.setdefault(agent.id, deque(maxlen=1000)).append(now)
            self._emit(s, EventType.ORDER_PLACED, agent.id, {"order": order_to_dict(order)})

            self._try_execute(s, agent, order, now, phase)
            if validity == Validity.IOC and order.status in OPEN_STATUSES:
                self._cancel(s, agent, order, "IOC: not immediately fillable")
            if order.status == OrderStatus.OPEN.value and phase != MarketPhase.NORMAL:
                order.status_reason = f"queued (market phase {phase.value}); executes when the market opens at {self.clock.next_open(now).isoformat()}"
            s.commit()
            return order_to_dict(order)

    def cancel_order(self, agent_id: str, order_id: str) -> dict[str, Any]:
        with self.lock, self._session() as s:
            agent = self._get_agent(s, agent_id)
            order = self._get_order(s, agent, order_id)
            if order.status not in OPEN_STATUSES:
                raise Conflict(f"order {order_id} is {order.status} and cannot be cancelled")
            self._cancel(s, agent, order, "cancelled by agent")
            s.commit()
            return order_to_dict(order)

    def cancel_all_orders(self, agent_id: str) -> list[dict[str, Any]]:
        with self.lock, self._session() as s:
            agent = self._get_agent(s, agent_id)
            out = []
            for o in self._open_orders(s, agent.id):
                self._cancel(s, agent, o, "cancelled by agent (cancel_all)")
                out.append(order_to_dict(o))
            s.commit()
            return out

    def modify_order(
        self,
        agent_id: str,
        order_id: str,
        *,
        quantity: int | None = None,
        price: Decimal | float | None = None,
        trigger_price: Decimal | float | None = None,
    ) -> dict[str, Any]:
        with self.lock, self._session() as s:
            agent = self._get_agent(s, agent_id)
            order = self._get_order(s, agent, order_id)
            if order.status not in OPEN_STATUSES:
                raise Conflict(f"order {order_id} is {order.status} and cannot be modified")
            if order.is_system:
                raise Conflict("system orders cannot be modified")
            new_qty = order.quantity if quantity is None else int(quantity)
            new_price = order.price if price is None else Decimal(str(price))
            new_trig = order.trigger_price if trigger_price is None else Decimal(str(trigger_price))
            if new_qty < order.filled_quantity or new_qty <= 0:
                raise InvalidRequest(f"quantity must be >= filled quantity ({order.filled_quantity})")
            if order.order_type in (OrderType.MARKET.value, OrderType.SL_M.value) and price is not None:
                raise InvalidRequest(f"{order.order_type} orders have no price")
            if (
                order.order_type in (OrderType.MARKET.value, OrderType.LIMIT.value)
                and trigger_price is not None
            ):
                raise InvalidRequest(f"{order.order_type} orders have no trigger_price")
            exchange = Exchange(order.exchange)
            quote = self._quote_or_raise(order.symbol, exchange)
            info = self.market.instrument(order.symbol, exchange)
            for label, v in (("price", new_price), ("trigger_price", new_trig)):
                if v is None:
                    continue
                if round_to_tick(v, info.tick_size) != v:
                    raise InvalidRequest(f"{label} {v} is not a multiple of tick size {info.tick_size}")
                if not (quote.lower_circuit <= v <= quote.upper_circuit):
                    raise OrderRejected(
                        f"{label} {v} is outside the price band {quote.lower_circuit}–{quote.upper_circuit}"
                    )

            # re-block cash for the new remaining size
            side, product = Side(order.side), ProductType(order.product)
            self._release_block(s, agent, order, order.blocked_cash, "released on modify")
            remaining = new_qty - order.filled_quantity
            ref = self._reference_price(OrderType(order.order_type), side, new_price, new_trig, quote)
            pos = self._get_position(s, agent.id, order.symbol, exchange, product)
            est = compute_charges(
                side=side,
                product=product,
                exchange=exchange,
                quantity=remaining,
                price=ref,
                schedule=self.charge_schedule,
            ).total
            block = self._required_block(product, side, remaining, ref, pos.quantity if pos else 0, est)
            if block > agent.cash:
                self._cancel(s, agent, order, "insufficient funds after modification")
                s.commit()
                raise OrderRejected(
                    "insufficient funds for the modified order; the order was cancelled",
                    details={"required": num(block), "available_cash": num(agent.cash)},
                )
            if block > 0:
                agent.cash -= block
                agent.blocked_cash += block
                order.blocked_cash = block
                self._ledger(s, agent, LedgerKind.MARGIN_BLOCK, -block, order.id, "blocked on modify")
            order.quantity, order.price, order.trigger_price = new_qty, new_price, new_trig
            order.updated_at = self._now()
            self._emit(s, EventType.ORDER_MODIFIED, agent.id, {"order": order_to_dict(order)})
            self._try_execute(s, agent, order, self._now(), self.clock.phase())
            s.commit()
            return order_to_dict(order)

    def get_order(self, agent_id: str, order_id: str) -> dict[str, Any]:
        with self._session() as s:
            agent = self._get_agent(s, agent_id)
            return order_to_dict(self._get_order(s, agent, order_id))

    def list_orders(
        self, agent_id: str, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._session() as s:
            q = select(OrderRow).where(OrderRow.agent_id == agent_id)
            if status == "open":
                q = q.where(OrderRow.status.in_(OPEN_STATUSES))
            elif status:
                q = q.where(OrderRow.status == status.upper())
            q = q.order_by(OrderRow.created_at.desc()).limit(max(1, min(limit, 500)))
            return [order_to_dict(o) for o in s.scalars(q)]

    def list_trades(self, agent_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._session() as s:
            q = (
                select(TradeRow)
                .where(TradeRow.agent_id == agent_id)
                .order_by(TradeRow.executed_at.desc())
                .limit(max(1, min(limit, 500)))
            )
            return [trade_to_dict(t) for t in s.scalars(q)]

    def ledger(self, agent_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._session() as s:
            q = (
                select(LedgerRow)
                .where(LedgerRow.agent_id == agent_id)
                .order_by(LedgerRow.id.desc())
                .limit(max(1, min(limit, 1000)))
            )
            return [ledger_to_dict(row) for row in s.scalars(q)]

    # =====================================================================================
    # portfolio
    # =====================================================================================

    def positions(self, agent_id: str, *, include_closed: bool = False) -> list[dict[str, Any]]:
        with self._session() as s:
            rows = s.scalars(select(PositionRow).where(PositionRow.agent_id == agent_id)).all()
            out = []
            for p in rows:
                if p.quantity == 0 and not include_closed:
                    continue
                q = self.market.quote(p.symbol, Exchange(p.exchange))
                out.append(position_to_dict(p, q.ltp if q else None))
            return out

    def portfolio(self, agent_id: str) -> dict[str, Any]:
        with self._session() as s:
            agent = self._get_agent(s, agent_id)
            self._check_daily_loss(s, agent, self._now(), commit=False)
            summary = self._portfolio_summary(s, agent)
            s.commit()
            return summary

    def _portfolio_summary(self, s: Session, agent: AgentRow) -> dict[str, Any]:
        rows = s.scalars(select(PositionRow).where(PositionRow.agent_id == agent.id)).all()
        holdings_value = ZERO
        cnc_cost = ZERO
        mis_unrealised = ZERO
        realised = ZERO
        positions = []
        for p in rows:
            realised += p.realised_pnl
            if p.quantity == 0:
                continue
            q = self.market.quote(p.symbol, Exchange(p.exchange))
            ltp = q.ltp if q else p.average_price
            if p.product == ProductType.CNC.value:
                holdings_value += ltp * p.quantity
                cnc_cost += p.average_price * p.quantity
            else:
                mis_unrealised += (ltp - p.average_price) * p.quantity
            positions.append(position_to_dict(p, ltp))
        equity = to_paise(agent.cash + agent.blocked_cash + holdings_value + mis_unrealised)
        charges = (
            s.scalar(select(func.count()).select_from(TradeRow).where(TradeRow.agent_id == agent.id)) or 0
        )
        total_charges = sum(
            (t.charges for t in s.scalars(select(TradeRow).where(TradeRow.agent_id == agent.id))), ZERO
        )
        open_orders = (
            s.scalar(
                select(func.count())
                .select_from(OrderRow)
                .where(OrderRow.agent_id == agent.id, OrderRow.status.in_(OPEN_STATUSES))
            )
            or 0
        )
        day_start = agent.day_start_equity if agent.day_start_equity is not None else agent.initial_cash
        return {
            "agent_id": agent.id,
            "name": agent.name,
            "status": agent.status,
            "halt_reason": agent.halt_reason,
            "cash": num(agent.cash),
            "blocked_cash": num(agent.blocked_cash),
            "holdings_value": num(to_paise(holdings_value)),
            "equity": num(equity),
            "initial_cash": num(agent.initial_cash),
            "total_pnl": num(to_paise(equity - agent.initial_cash)),
            "total_return_pct": num(
                ((equity - agent.initial_cash) / agent.initial_cash * 100).quantize(PAISE)
            ),
            "day_pnl": num(to_paise(equity - day_start)),
            "unrealised_pnl": num(to_paise((holdings_value - cnc_cost) + mis_unrealised)),
            "realised_pnl": num(to_paise(realised)),
            "total_charges": num(to_paise(total_charges)),
            "trade_count": charges,
            "open_orders": open_orders,
            "positions": positions,
            "risk_limits": self.agent_to_dict(agent)["risk_limits"],
            "as_of": ts(self._now()),
        }

    def performance(self, agent_id: str, *, points: int = 200) -> dict[str, Any]:
        with self._session() as s:
            agent = self._get_agent(s, agent_id)
            summary = self._portfolio_summary(s, agent)
            trades = s.scalars(select(TradeRow).where(TradeRow.agent_id == agent.id)).all()
            closing = [t for t in trades if t.realised_pnl != 0]
            wins = [t for t in closing if t.realised_pnl > 0]
            snaps = s.scalars(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.agent_id == agent.id)
                .order_by(EquitySnapshotRow.ts.desc())
                .limit(points)
            ).all()[::-1]
            curve = [{"ts": ts(x.ts), "equity": num(x.equity)} for x in snaps]
            peak, max_dd = agent.initial_cash, ZERO
            for x in snaps:
                peak = max(peak, x.equity)
                max_dd = max(max_dd, (peak - x.equity) / peak * 100 if peak else ZERO)
            gross_win = sum((t.realised_pnl for t in wins), ZERO)
            gross_loss = -sum((t.realised_pnl for t in closing if t.realised_pnl < 0), ZERO)
            return {
                **{
                    k: summary[k]
                    for k in (
                        "agent_id",
                        "name",
                        "equity",
                        "initial_cash",
                        "total_pnl",
                        "total_return_pct",
                        "day_pnl",
                        "realised_pnl",
                        "unrealised_pnl",
                        "total_charges",
                    )
                },
                "trade_count": len(trades),
                "closing_trade_count": len(closing),
                "win_rate_pct": num((Decimal(len(wins)) / len(closing) * 100).quantize(PAISE))
                if closing
                else None,
                "profit_factor": num((gross_win / gross_loss).quantize(PAISE)) if gross_loss else None,
                "max_drawdown_pct": num(max_dd.quantize(PAISE)),
                "equity_curve": curve,
            }

    def leaderboard(self) -> list[dict[str, Any]]:
        with self._session() as s:
            rows = []
            for a in s.scalars(select(AgentRow)):
                p = self._portfolio_summary(s, a)
                rows.append(
                    {
                        k: p[k]
                        for k in (
                            "agent_id",
                            "name",
                            "status",
                            "equity",
                            "initial_cash",
                            "total_pnl",
                            "total_return_pct",
                            "day_pnl",
                            "trade_count",
                            "total_charges",
                        )
                    }
                )
            rows.sort(key=lambda r: r["total_return_pct"], reverse=True)
            for i, r in enumerate(rows, 1):
                r["rank"] = i
            return rows

    # =====================================================================================
    # events
    # =====================================================================================

    def events_since(
        self, agent_id: str | None, cursor: int, *, wait_seconds: float = 0, limit: int = 200
    ) -> dict[str, Any]:
        if wait_seconds > 0:
            self.bus.wait(cursor, min(wait_seconds, 60))
        evs = self.bus.since(cursor, agent_id=agent_id, limit=limit)
        return {
            "cursor": evs[-1].id if evs else max(cursor, self.bus.cursor if cursor == 0 else cursor),
            "events": [e.to_dict() for e in evs],
        }

    def audit_log(self, agent_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        from .serialize import event_row_to_dict

        with self._session() as s:
            q = (
                select(EventRow)
                .where(EventRow.agent_id == agent_id)
                .order_by(EventRow.id.desc())
                .limit(max(1, min(limit, 1000)))
            )
            return [event_row_to_dict(e) for e in s.scalars(q)]

    # =====================================================================================
    # background tick
    # =====================================================================================

    def tick(self) -> dict[str, Any]:
        """Advance market data one step and run every time-driven process. Called by the loop."""
        with self.lock:
            now = self._now()
            changed = self.market.step(now)
            phase = self.clock.phase(now)
            stats = {
                "ts": ts(now),
                "phase": phase.value,
                "quotes_changed": len(changed),
                "fills": 0,
                "expired": 0,
                "squared_off": 0,
                "halted": 0,
            }
            with self._session() as s:
                if phase == MarketPhase.NORMAL:
                    stats["fills"] = self._process_open_orders(s, now, phase)
                if self.clock.is_past_square_off(now) and self._squared_off_on != now.date():
                    stats["squared_off"] = self._square_off_mis(s, now, phase)
                    self._squared_off_on = now.date()
                stats["expired"] = self._expire_orders(s, now)
                stats["halted"] = self._risk_sweep(s, now)
                self._snapshot(s, now)
                s.commit()
            self.tick_count += 1
            if changed:
                self.bus.publish(
                    now, EventType.TICK.value, None, {"quotes_changed": len(changed), "phase": phase.value}
                )
            return stats

    def _process_open_orders(self, s: Session, now: datetime, phase: MarketPhase) -> int:
        fills = 0
        for order in s.scalars(
            select(OrderRow).where(OrderRow.status.in_(OPEN_STATUSES)).order_by(OrderRow.created_at)
        ).all():
            agent = s.get(AgentRow, order.agent_id)
            before = order.filled_quantity
            self._try_execute(s, agent, order, now, phase)
            if order.filled_quantity > before:
                fills += 1
        return fills

    def _expire_orders(self, s: Session, now: datetime) -> int:
        n = 0
        for order in s.scalars(
            select(OrderRow).where(OrderRow.status.in_(OPEN_STATUSES), OrderRow.expires_at <= now)
        ).all():
            agent = s.get(AgentRow, order.agent_id)
            self._release_block(s, agent, order, order.blocked_cash, "released on expiry")
            order.status = OrderStatus.EXPIRED.value
            order.status_reason = "DAY order expired at market close"
            order.updated_at = now
            self._emit(s, EventType.ORDER_EXPIRED, agent.id, {"order": order_to_dict(order)})
            n += 1
        return n

    def _square_off_mis(self, s: Session, now: datetime, phase: MarketPhase) -> int:
        n = 0
        for order in s.scalars(
            select(OrderRow).where(
                OrderRow.status.in_(OPEN_STATUSES), OrderRow.product == ProductType.MIS.value
            )
        ).all():
            self._cancel(
                s, s.get(AgentRow, order.agent_id), order, "open MIS orders are cancelled at 15:20 square-off"
            )
        for pos in s.scalars(
            select(PositionRow).where(PositionRow.product == ProductType.MIS.value, PositionRow.quantity != 0)
        ).all():
            agent = s.get(AgentRow, pos.agent_id)
            side = Side.SELL if pos.quantity > 0 else Side.BUY
            order = OrderRow(
                agent_id=agent.id,
                symbol=pos.symbol,
                exchange=pos.exchange,
                side=side.value,
                order_type=OrderType.MARKET.value,
                product=ProductType.MIS.value,
                validity=Validity.IOC.value,
                quantity=abs(pos.quantity),
                status=OrderStatus.OPEN.value,
                blocked_cash=ZERO,
                is_system=True,
                tag="AUTO_SQUARE_OFF",
                reasoning="system: MIS auto square-off at 15:20 IST",
                created_at=now,
                updated_at=now,
                expires_at=self._expiry_for(now),
            )
            s.add(order)
            s.flush()
            self._try_execute(s, agent, order, now, phase, force=True)
            self._emit(s, EventType.SQUARE_OFF, agent.id, {"order": order_to_dict(order)})
            n += 1
        return n

    def _risk_sweep(self, s: Session, now: datetime) -> int:
        if self._last_risk_check_at and (now - self._last_risk_check_at).total_seconds() < 5:
            return 0
        self._last_risk_check_at = now
        halted = 0
        for agent in s.scalars(select(AgentRow)).all():
            if self._check_daily_loss(s, agent, now, commit=False, raise_=False):
                halted += 1
        return halted

    def _snapshot(self, s: Session, now: datetime) -> None:
        if self._last_snapshot_at and (now - self._last_snapshot_at).total_seconds() < 60:
            return
        self._last_snapshot_at = now
        for agent in s.scalars(select(AgentRow)).all():
            summ = self._portfolio_summary(s, agent)
            s.add(
                EquitySnapshotRow(
                    agent_id=agent.id, ts=now, equity=Decimal(str(summ["equity"])), cash=agent.cash
                )
            )

    # =====================================================================================
    # execution internals
    # =====================================================================================

    def _try_execute(
        self,
        s: Session,
        agent: AgentRow,
        order: OrderRow,
        now: datetime,
        phase: MarketPhase,
        *,
        force: bool = False,
    ) -> None:
        if order.status not in OPEN_STATUSES:
            return
        if phase != MarketPhase.NORMAL and not force:
            return
        exchange = Exchange(order.exchange)
        quote = self.market.quote(order.symbol, exchange)
        if quote is None:
            return
        side = Side(order.side)
        otype = OrderType(order.order_type)
        info = self.market.instrument(order.symbol, exchange)
        tick = info.tick_size

        if otype in (OrderType.SL, OrderType.SL_M) and not order.triggered:
            hit = quote.ltp >= order.trigger_price if side == Side.BUY else quote.ltp <= order.trigger_price
            if not hit:
                return
            order.triggered = True
            order.updated_at = now
            self._emit(
                s,
                EventType.ORDER_TRIGGERED,
                agent.id,
                {"order_id": order.id, "ltp": num(quote.ltp), "trigger_price": num(order.trigger_price)},
            )

        if otype in (OrderType.MARKET, OrderType.SL_M):
            price = self._market_fill_price(quote, side, tick)
        else:  # LIMIT or triggered SL: fill only if marketable, at the touch
            if side == Side.BUY:
                if quote.ask > order.price:
                    return
                price = quote.ask
            else:
                if quote.bid < order.price:
                    return
                price = quote.bid

        remaining = order.remaining
        frac = self.settings.max_fill_fraction_per_tick
        qty = remaining if frac >= 1 else max(1, min(remaining, math.ceil(order.quantity * frac)))

        product = ProductType(order.product)
        if product == ProductType.CNC and side == Side.SELL:
            pos = self._get_position(s, agent.id, order.symbol, exchange, product)
            held = pos.quantity if pos else 0
            if held < qty:
                self._cancel(s, agent, order, f"insufficient holdings at execution: have {held}, need {qty}")
                return

        charges = compute_charges(
            side=side,
            product=product,
            exchange=exchange,
            quantity=qty,
            price=price,
            schedule=self.charge_schedule,
            apply_dp_charge=(
                product == ProductType.CNC
                and side == Side.SELL
                and not self._dp_charged_today(s, agent.id, order.symbol, exchange, now)
            ),
        )
        self._apply_fill(s, agent, order, qty, price, charges, now)

    def _market_fill_price(self, quote: Quote, side: Side, tick: Decimal) -> Decimal:
        slip = Decimal(self.settings.market_slippage_bps) / Decimal(10_000)
        raw = quote.ask * (1 + slip) if side == Side.BUY else quote.bid * (1 - slip)
        p = round_to_tick(raw, tick)
        return max(quote.lower_circuit, min(quote.upper_circuit, p))

    def _apply_fill(
        self, s: Session, agent: AgentRow, order: OrderRow, qty: int, price: Decimal, charges, now: datetime
    ) -> None:
        exchange = Exchange(order.exchange)
        side, product = Side(order.side), ProductType(order.product)
        pos = self._get_position(s, agent.id, order.symbol, exchange, product, create=True)
        remaining_before = order.remaining
        value = to_paise(price * qty)

        # release the order-level block proportional to this fill
        released = to_paise(order.blocked_cash * qty / remaining_before) if remaining_before else ZERO
        released = min(released, order.blocked_cash)
        self._release_block(s, agent, order, released, "released on fill")

        new_qty, new_avg, realised = apply_fill(pos.quantity, pos.average_price, side, qty, price)

        if product == ProductType.CNC:
            if side == Side.BUY:
                if agent.cash < value + charges.total:
                    # should not happen thanks to blocking; keep cash exact and cancel the remainder
                    self._cancel(s, agent, order, "insufficient funds at execution")
                    return
                agent.cash -= value
                self._ledger(
                    s, agent, LedgerKind.BUY, -value, order.id, f"BUY {qty} {order.symbol} @ {price}"
                )
            else:
                agent.cash += value
                self._ledger(
                    s, agent, LedgerKind.SELL, value, order.id, f"SELL {qty} {order.symbol} @ {price}"
                )
        else:
            new_margin = to_paise(abs(new_qty) * new_avg / self.mis_leverage)
            delta = new_margin - pos.margin_blocked
            if delta > 0:
                if agent.cash < delta + charges.total:
                    self._cancel(s, agent, order, "insufficient margin at execution")
                    return
                agent.cash -= delta
                agent.blocked_cash += delta
                self._ledger(
                    s, agent, LedgerKind.MARGIN_BLOCK, -delta, order.id, f"MIS margin for {order.symbol}"
                )
            elif delta < 0:
                agent.cash += -delta
                agent.blocked_cash -= -delta
                self._ledger(
                    s,
                    agent,
                    LedgerKind.MARGIN_RELEASE,
                    -delta,
                    order.id,
                    f"MIS margin released for {order.symbol}",
                )
            pos.margin_blocked = new_margin
            if realised:
                agent.cash += realised
                self._ledger(
                    s, agent, LedgerKind.REALISED_PNL, realised, order.id, f"realised P&L on {order.symbol}"
                )

        agent.cash -= charges.total
        self._ledger(s, agent, LedgerKind.CHARGES, -charges.total, order.id, "statutory + brokerage charges")

        pos.quantity, pos.average_price = new_qty, new_avg
        pos.realised_pnl += realised
        if side == Side.BUY:
            pos.buy_quantity += qty
        else:
            pos.sell_quantity += qty
        pos.updated_at = now

        trade = TradeRow(
            order_id=order.id,
            agent_id=agent.id,
            symbol=order.symbol,
            exchange=order.exchange,
            side=order.side,
            product=order.product,
            quantity=qty,
            price=price,
            charges=charges.total,
            realised_pnl=realised,
            executed_at=now,
        )
        s.add(trade)
        s.flush()

        prev_filled = order.filled_quantity
        prev_avg = order.average_price or ZERO
        order.filled_quantity += qty
        order.average_price = ((prev_avg * prev_filled + price * qty) / order.filled_quantity).quantize(
            Decimal("0.0001")
        )
        order.charges += charges.total
        bd = dict(order.charges_breakdown or {})
        for k, v in charges.as_dict().items():
            bd[k] = str(to_paise(Decimal(bd.get(k, "0")) + Decimal(v)))
        order.charges_breakdown = bd
        order.updated_at = now
        if order.remaining == 0:
            order.status = OrderStatus.FILLED.value
            order.executed_at = now
            order.status_reason = None
            if order.blocked_cash > 0:
                self._release_block(s, agent, order, order.blocked_cash, "released residual block")
            ev = EventType.ORDER_FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED.value
            ev = EventType.ORDER_PARTIALLY_FILLED
        self.market.record_trade(order.symbol, exchange, qty)
        self._emit(
            s,
            ev,
            agent.id,
            {
                "order": order_to_dict(order),
                "trade": trade_to_dict(trade),
                "position": position_to_dict(pos, price),
            },
        )

    def _cancel(self, s: Session, agent: AgentRow, order: OrderRow, reason: str) -> None:
        self._release_block(s, agent, order, order.blocked_cash, "released on cancel")
        order.status = OrderStatus.CANCELLED.value
        order.status_reason = reason
        order.updated_at = self._now()
        self._emit(s, EventType.ORDER_CANCELLED, agent.id, {"order": order_to_dict(order)})

    def _release_block(
        self, s: Session, agent: AgentRow, order: OrderRow, amount: Decimal, note: str
    ) -> None:
        amount = min(amount, order.blocked_cash)
        if amount <= 0:
            return
        order.blocked_cash -= amount
        agent.blocked_cash -= amount
        agent.cash += amount
        self._ledger(s, agent, LedgerKind.MARGIN_RELEASE, amount, order.id, note)

    # ---- risk helpers ---------------------------------------------------------------

    def _reference_price(
        self, otype: OrderType, side: Side, price: Decimal | None, trig: Decimal | None, quote: Quote
    ) -> Decimal:
        if otype == OrderType.LIMIT:
            return price
        if otype == OrderType.SL:
            return max(price, trig) if side == Side.BUY else price
        if otype == OrderType.SL_M:
            return trig
        slip = Decimal(self.settings.market_slippage_bps) / Decimal(10_000)
        return (quote.ask * (1 + slip)) if side == Side.BUY else quote.bid

    def _required_block(
        self,
        product: ProductType,
        side: Side,
        qty: int,
        ref_price: Decimal,
        cur_qty: int,
        est_charges: Decimal,
    ) -> Decimal:
        if product == ProductType.CNC:
            return to_paise(ref_price * qty + est_charges) if side == Side.BUY else ZERO
        signed = qty if side == Side.BUY else -qty
        increase = max(0, abs(cur_qty + signed) - abs(cur_qty))
        return to_paise(ref_price * increase / self.mis_leverage + est_charges)

    def _check_rate_limit(self, agent: AgentRow, now: datetime) -> None:
        times = self._order_times.setdefault(agent.id, deque(maxlen=1000))
        window = now - timedelta(seconds=60)
        while times and times[0] < window:
            times.popleft()
        if len(times) >= agent.max_orders_per_minute:
            raise RateLimited(
                f"order rate limit: {agent.max_orders_per_minute} orders/minute",
                hint="Slow down; batch decisions and avoid re-submitting unchanged orders.",
                details={"retry_after_seconds": max(1, int(60 - (now - times[0]).total_seconds()))},
            )

    def _check_daily_loss(
        self, s: Session, agent: AgentRow, now: datetime, *, commit: bool = True, raise_: bool = True
    ) -> bool:
        """Roll the day-start equity on a new day; halt the agent if today's loss breaches the limit."""
        today = now.date().isoformat()
        summ = self._portfolio_summary(s, agent)
        equity = Decimal(str(summ["equity"]))
        if agent.day_start_date != today:
            agent.day_start_date = today
            agent.day_start_equity = equity
            if agent.status == AgentStatus.HALTED.value and (agent.halt_reason or "").startswith(
                "daily loss limit"
            ):
                agent.status = AgentStatus.ACTIVE.value
                agent.halt_reason = None
                self._emit(s, EventType.AGENT_RESUMED, agent.id, {"reason": "new trading day"})
            return False
        if agent.status != AgentStatus.ACTIVE.value:
            return False
        loss = (agent.day_start_equity or equity) - equity
        if loss >= agent.max_daily_loss:
            self._halt(
                s,
                agent,
                f"daily loss limit breached: lost ₹{to_paise(loss)} today (limit ₹{agent.max_daily_loss})",
                cancel_orders=True,
            )
            if commit:
                s.commit()
            if raise_:
                raise AgentHalted(
                    f"agent halted: daily loss ₹{to_paise(loss)} breached the limit ₹{agent.max_daily_loss}",
                    hint="Trading resumes automatically on the next trading day, or an admin can resume the agent now.",
                    details={"day_loss": num(to_paise(loss)), "max_daily_loss": num(agent.max_daily_loss)},
                )
            return True
        return False

    # ---- query helpers --------------------------------------------------------------

    def _get_order(self, s: Session, agent: AgentRow, order_id: str) -> OrderRow:
        order = s.scalar(select(OrderRow).where(OrderRow.id == order_id, OrderRow.agent_id == agent.id))
        if order is None:
            order = s.scalar(
                select(OrderRow).where(OrderRow.client_order_id == order_id, OrderRow.agent_id == agent.id)
            )
        if order is None:
            raise NotFound(f"order {order_id} not found for this agent")
        return order

    def _open_orders(self, s: Session, agent_id: str) -> list[OrderRow]:
        return s.scalars(
            select(OrderRow).where(OrderRow.agent_id == agent_id, OrderRow.status.in_(OPEN_STATUSES))
        ).all()

    def _get_position(
        self,
        s: Session,
        agent_id: str,
        symbol: str,
        exchange: Exchange,
        product: ProductType,
        *,
        create: bool = False,
    ) -> PositionRow | None:
        pos = s.scalar(
            select(PositionRow).where(
                PositionRow.agent_id == agent_id,
                PositionRow.symbol == symbol,
                PositionRow.exchange == exchange.value,
                PositionRow.product == product.value,
            )
        )
        if pos is None and create:
            pos = PositionRow(
                agent_id=agent_id,
                symbol=symbol,
                exchange=exchange.value,
                product=product.value,
                quantity=0,
                average_price=ZERO,
                updated_at=self._now(),
            )
            s.add(pos)
            s.flush()
        return pos

    def _committed_sell_qty(self, s: Session, agent_id: str, symbol: str, exchange: Exchange) -> int:
        rows = s.scalars(
            select(OrderRow).where(
                OrderRow.agent_id == agent_id,
                OrderRow.symbol == symbol,
                OrderRow.exchange == exchange.value,
                OrderRow.product == ProductType.CNC.value,
                OrderRow.side == Side.SELL.value,
                OrderRow.status.in_(OPEN_STATUSES),
            )
        ).all()
        return sum(o.remaining for o in rows)

    def _dp_charged_today(
        self, s: Session, agent_id: str, symbol: str, exchange: Exchange, now: datetime
    ) -> bool:
        start = datetime.combine(now.date(), datetime.min.time(), tzinfo=IST)
        row = s.scalar(
            select(TradeRow.id)
            .where(
                TradeRow.agent_id == agent_id,
                TradeRow.symbol == symbol,
                TradeRow.exchange == exchange.value,
                TradeRow.product == ProductType.CNC.value,
                TradeRow.side == Side.SELL.value,
                TradeRow.executed_at >= start,
            )
            .limit(1)
        )
        return row is not None
