"""Row → plain-dict conversion shared by the REST API, MCP tools and SDK.

Money is emitted as JSON numbers (2–4 dp floats) because that is what LLM tool-use and most
agent frameworks handle most naturally; the engine itself is exact :class:`Decimal`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..marketdata.base import Candle, InstrumentInfo, Quote
from ..models import EventRow, LedgerRow, OrderRow, PositionRow, TradeRow


def num(x: Decimal | None) -> float | None:
    return None if x is None else float(x)


def ts(x: datetime | None) -> str | None:
    return None if x is None else x.isoformat()


def order_to_dict(o: OrderRow) -> dict[str, Any]:
    return {
        "order_id": o.id,
        "client_order_id": o.client_order_id,
        "symbol": o.symbol,
        "exchange": o.exchange,
        "side": o.side,
        "order_type": o.order_type,
        "product": o.product,
        "validity": o.validity,
        "quantity": o.quantity,
        "filled_quantity": o.filled_quantity,
        "pending_quantity": o.quantity - o.filled_quantity,
        "price": num(o.price),
        "trigger_price": num(o.trigger_price),
        "triggered": o.triggered,
        "status": o.status,
        "status_reason": o.status_reason,
        "average_price": num(o.average_price),
        "charges": num(o.charges),
        "charges_breakdown": o.charges_breakdown,
        "blocked_cash": num(o.blocked_cash),
        "reasoning": o.reasoning,
        "tag": o.tag,
        "is_system": o.is_system,
        "created_at": ts(o.created_at),
        "updated_at": ts(o.updated_at),
        "executed_at": ts(o.executed_at),
        "expires_at": ts(o.expires_at),
    }


def trade_to_dict(t: TradeRow) -> dict[str, Any]:
    return {
        "trade_id": t.id,
        "order_id": t.order_id,
        "symbol": t.symbol,
        "exchange": t.exchange,
        "side": t.side,
        "product": t.product,
        "quantity": t.quantity,
        "price": num(t.price),
        "value": num(t.price * t.quantity),
        "charges": num(t.charges),
        "realised_pnl": num(t.realised_pnl),
        "executed_at": ts(t.executed_at),
    }


def position_to_dict(p: PositionRow, ltp: Decimal | None) -> dict[str, Any]:
    ltp = ltp if ltp is not None else p.average_price
    unrealised = (ltp - p.average_price) * p.quantity
    return {
        "symbol": p.symbol,
        "exchange": p.exchange,
        "product": p.product,
        "quantity": p.quantity,
        "average_price": num(p.average_price),
        "last_price": num(ltp),
        "market_value": num(ltp * p.quantity),
        "unrealised_pnl": num(unrealised),
        "realised_pnl": num(p.realised_pnl),
        "margin_blocked": num(p.margin_blocked),
        "buy_quantity": p.buy_quantity,
        "sell_quantity": p.sell_quantity,
        "updated_at": ts(p.updated_at),
    }


def ledger_to_dict(l: LedgerRow) -> dict[str, Any]:
    return {
        "id": l.id,
        "ts": ts(l.ts),
        "kind": l.kind,
        "amount": num(l.amount),
        "cash_after": num(l.cash_after),
        "blocked_after": num(l.blocked_after),
        "ref_id": l.ref_id,
        "note": l.note,
    }


def event_row_to_dict(e: EventRow) -> dict[str, Any]:
    return {"id": e.id, "ts": ts(e.ts), "type": e.type, "agent_id": e.agent_id, **e.payload}


def quote_to_dict(q: Quote) -> dict[str, Any]:
    return {
        "symbol": q.symbol,
        "exchange": q.exchange.value,
        "ltp": num(q.ltp),
        "bid": num(q.bid),
        "ask": num(q.ask),
        "open": num(q.open),
        "high": num(q.high),
        "low": num(q.low),
        "prev_close": num(q.prev_close),
        "change": num(q.change),
        "change_pct": num(q.change_pct),
        "upper_circuit": num(q.upper_circuit),
        "lower_circuit": num(q.lower_circuit),
        "volume": q.volume,
        "ts": ts(q.ts),
    }


def instrument_to_dict(i: InstrumentInfo) -> dict[str, Any]:
    return {
        "symbol": i.symbol,
        "exchange": i.exchange.value,
        "name": i.name,
        "sector": i.sector,
        "tick_size": num(i.tick_size),
        "lot_size": i.lot_size,
        "band_pct": i.band_pct,
    }


def candle_to_dict(c: Candle) -> dict[str, Any]:
    return {"ts": ts(c.ts), "open": num(c.open), "high": num(c.high), "low": num(c.low), "close": num(c.close), "volume": c.volume}
