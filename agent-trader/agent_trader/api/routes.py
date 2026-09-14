"""REST routes. Every handler is a thin wrapper over :class:`TradingEngine`."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from sse_starlette.sse import EventSourceResponse

from ..charges import Exchange
from ..clock import to_ist
from ..engine import Conflict, InvalidRequest
from ..engine.serialize import quote_to_dict
from ..models import LedgerKind
from . import schemas
from .deps import RT, Agent, Engine, require_admin

agents = APIRouter(prefix="/v1/agents", tags=["agents"])
orders = APIRouter(prefix="/v1/orders", tags=["orders"])
market = APIRouter(prefix="/v1/market", tags=["market"])
events = APIRouter(prefix="/v1", tags=["events"])
admin = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])
meta = APIRouter(tags=["meta"])


def _risk_dict(r: schemas.RiskLimits | None) -> dict[str, Any]:
    return {k: v for k, v in (r.model_dump() if r else {}).items() if v is not None}


# ---- agents -------------------------------------------------------------------------------


@agents.post("/register", response_model=schemas.RegisterAgentResponse, status_code=201)
def register(req: schemas.RegisterAgentRequest, engine: Engine, rt: RT, x_admin_key: str | None = None):
    if not rt.settings.open_registration:
        require_admin(rt, x_admin_key)
    agent, key = engine.register_agent(
        req.name, initial_cash=req.initial_cash, description=req.description, metadata=req.metadata, risk=_risk_dict(req.risk_limits)
    )
    return {"agent": agent, "api_key": key}


@agents.get("/me")
def me(agent: Agent):
    return agent


@agents.get("/me/portfolio")
def portfolio(agent: Agent, engine: Engine):
    return engine.portfolio(agent["agent_id"])


@agents.get("/me/positions")
def positions(agent: Agent, engine: Engine, include_closed: bool = False):
    return engine.positions(agent["agent_id"], include_closed=include_closed)


@agents.get("/me/trades")
def trades(agent: Agent, engine: Engine, limit: int = Query(100, ge=1, le=500)):
    return engine.list_trades(agent["agent_id"], limit=limit)


@agents.get("/me/ledger")
def ledger(agent: Agent, engine: Engine, limit: int = Query(100, ge=1, le=1000)):
    return engine.ledger(agent["agent_id"], limit=limit)


@agents.get("/me/performance")
def performance(agent: Agent, engine: Engine, points: int = Query(200, ge=1, le=2000)):
    return engine.performance(agent["agent_id"], points=points)


@agents.get("/me/audit")
def audit(agent: Agent, engine: Engine, limit: int = Query(200, ge=1, le=1000)):
    return engine.audit_log(agent["agent_id"], limit=limit)


@agents.post("/me/halt")
def halt_self(req: schemas.HaltRequest, agent: Agent, engine: Engine):
    return engine.halt_agent(agent["agent_id"], f"self: {req.reason}")


@agents.post("/me/resume")
def resume_self(agent: Agent, engine: Engine):
    if agent["status"] == "ACTIVE":
        return agent
    if not (agent.get("halt_reason") or "").startswith("self:"):
        raise Conflict(
            "only self-imposed halts can be lifted by the agent",
            hint="Risk halts (daily loss) lift automatically next trading day or via an admin.",
        )
    return engine.resume_agent(agent["agent_id"])


@agents.patch("/me/risk-limits")
def tighten_risk(req: schemas.RiskLimits, agent: Agent, engine: Engine):
    """Agents may only *tighten* their own limits; raising them needs an admin."""
    new = _risk_dict(req)
    cur = agent["risk_limits"]
    for k, v in new.items():
        if Decimal(str(v)) > Decimal(str(cur[k])):
            raise InvalidRequest(f"{k} can only be lowered by the agent (current {cur[k]})", hint="Ask an admin to raise limits.")
    return engine.update_risk_limits(agent["agent_id"], new)


@agents.get("/leaderboard")
def leaderboard(engine: Engine):
    return engine.leaderboard()


# ---- orders -------------------------------------------------------------------------------


@orders.post("", status_code=201)
def place(req: schemas.PlaceOrderRequest, agent: Agent, engine: Engine):
    return engine.place_order(agent["agent_id"], **req.model_dump())


@orders.get("")
def list_orders(agent: Agent, engine: Engine, status: str | None = Query(None, description="open | OPEN | FILLED | CANCELLED | REJECTED | EXPIRED"), limit: int = Query(100, ge=1, le=500)):
    return engine.list_orders(agent["agent_id"], status=status, limit=limit)


@orders.delete("")
def cancel_all(agent: Agent, engine: Engine):
    return engine.cancel_all_orders(agent["agent_id"])


@orders.get("/{order_id}")
def get_order(order_id: str, agent: Agent, engine: Engine):
    return engine.get_order(agent["agent_id"], order_id)


@orders.delete("/{order_id}")
def cancel(order_id: str, agent: Agent, engine: Engine):
    return engine.cancel_order(agent["agent_id"], order_id)


@orders.patch("/{order_id}")
def modify(order_id: str, req: schemas.ModifyOrderRequest, agent: Agent, engine: Engine):
    return engine.modify_order(agent["agent_id"], order_id, **req.model_dump())


# ---- market -------------------------------------------------------------------------------


@market.get("/status")
def status(engine: Engine):
    return engine.market_status()


@market.get("/instruments")
def instruments(engine: Engine, q: str | None = None, exchange: schemas.ExchangeStr | None = None, limit: int = Query(200, ge=1, le=1000)):
    return engine.instruments(q, Exchange(exchange) if exchange else None, limit)


@market.get("/quotes")
def quotes(engine: Engine, symbols: str | None = Query(None, description="comma separated; omit for all"), exchange: schemas.ExchangeStr = "NSE"):
    syms = [s for s in symbols.split(",") if s.strip()] if symbols else None
    return engine.quotes(syms, Exchange(exchange))


@market.get("/quote/{symbol}")
def quote(symbol: str, engine: Engine, exchange: schemas.ExchangeStr = "NSE"):
    return engine.quote(symbol, Exchange(exchange))


@market.get("/ohlc/{symbol}")
def ohlc(symbol: str, engine: Engine, exchange: schemas.ExchangeStr = "NSE", interval: str = "1m", limit: int = Query(100, ge=1, le=1000)):
    return engine.ohlc(symbol, Exchange(exchange), interval, limit)


# ---- events / streaming -------------------------------------------------------------------


@events.get("/events")
def poll_events(agent: Agent, engine: Engine, cursor: int = Query(0, ge=0), wait: float = Query(0, ge=0, le=60), limit: int = Query(200, ge=1, le=1000)):
    """Long-poll for this agent's events after ``cursor``. Pass ``wait`` seconds to block until something arrives."""
    return engine.events_since(agent["agent_id"], cursor, wait_seconds=wait, limit=limit)


@events.get("/stream/events")
async def stream_events(agent: Agent, engine: Engine, cursor: int = Query(0, ge=0)):
    """Server-sent events: one ``event`` per order/agent event for this agent (plus TICK heartbeats)."""

    async def gen():
        cur = cursor
        while True:
            res = await asyncio.to_thread(engine.events_since, agent["agent_id"], cur, wait_seconds=15, limit=200)
            for ev in res["events"]:
                cur = ev["id"]
                yield {"id": str(ev["id"]), "event": ev["type"], "data": json.dumps(ev)}
            if not res["events"]:
                yield {"event": "heartbeat", "data": json.dumps({"cursor": cur})}

    return EventSourceResponse(gen())


@events.get("/stream/quotes")
async def stream_quotes(agent: Agent, engine: Engine, symbols: str = Query(..., description="comma separated"), exchange: schemas.ExchangeStr = "NSE", interval: float = Query(1.0, ge=0.2, le=60)):
    """Server-sent events: a ``quotes`` event every ``interval`` seconds for the given symbols."""
    syms = [s.strip() for s in symbols.split(",") if s.strip()]

    async def gen():
        while True:
            qs = engine.quotes(syms, Exchange(exchange))
            yield {"event": "quotes", "data": json.dumps(qs)}
            await asyncio.sleep(interval)

    return EventSourceResponse(gen())


# ---- admin --------------------------------------------------------------------------------


@admin.get("/agents")
def admin_agents(engine: Engine):
    return engine.list_agents()


@admin.post("/agents/{agent_id}/halt")
def admin_halt(agent_id: str, req: schemas.HaltRequest, engine: Engine):
    return engine.halt_agent(agent_id, f"admin: {req.reason}")


@admin.post("/agents/{agent_id}/resume")
def admin_resume(agent_id: str, engine: Engine):
    return engine.resume_agent(agent_id)


@admin.patch("/agents/{agent_id}/risk-limits")
def admin_risk(agent_id: str, req: schemas.RiskLimits, engine: Engine):
    return engine.update_risk_limits(agent_id, _risk_dict(req))


@admin.post("/agents/{agent_id}/deposit")
def admin_deposit(agent_id: str, req: schemas.DepositRequest, engine: Engine):
    return engine.deposit(agent_id, req.amount, req.note)


@admin.post("/market/set-price")
def admin_set_price(req: schemas.SetPriceRequest, rt: RT):
    if not hasattr(rt.market, "set_price"):
        raise InvalidRequest("the active market-data provider does not support set_price")
    q = rt.market.set_price(req.symbol.upper(), Exchange(req.exchange), req.price)
    return quote_to_dict(q)


@admin.post("/clock")
def admin_clock(req: schemas.ClockRequest, rt: RT):
    """Control the frozen clock (tests / deterministic replays) and run ticks."""
    if rt.clock.mode != "frozen" and (req.set or req.advance_seconds):
        raise InvalidRequest("clock can only be moved in frozen mode (TRADER_CLOCK_MODE=frozen)")
    if req.set:
        rt.clock.set(to_ist(datetime.fromisoformat(req.set)))
    if req.advance_seconds:
        from datetime import timedelta

        rt.clock.advance(timedelta(seconds=req.advance_seconds))
    stats = None
    for _ in range(req.ticks):
        stats = rt.engine.tick()
    return {"clock": rt.clock.status(), "last_tick": stats}


@admin.post("/tick")
def admin_tick(rt: RT, n: int = Query(1, ge=1, le=10_000)):
    stats = None
    for _ in range(n):
        stats = rt.engine.tick()
    return stats


# ---- meta ---------------------------------------------------------------------------------


@meta.get("/health")
def health(rt: RT):
    return {"status": "ok", "clock": rt.clock.status()["phase"], "ticks": rt.engine.tick_count}


@meta.get("/")
@meta.get("/v1/capabilities")
def capabilities(rt: RT):
    """Machine-readable description of this platform, for agents that discover it via HTTP."""
    s = rt.settings
    return {
        "name": "agent-trader",
        "description": "Paper-trading platform for AI agents on the Indian stock market (NSE/BSE, INR). Simulated execution, real market rules.",
        "auth": {"header": "X-API-Key", "obtain": "POST /v1/agents/register"},
        "openapi": "/openapi.json",
        "mcp": {"transport": "streamable-http", "path": "/mcp", "stdio": "agent-trader mcp"},
        "market": {"exchanges": ["NSE", "BSE"], "currency": "INR", "session_ist": "09:15-15:30", "mis_square_off_ist": "15:20"},
        "products": {"CNC": "delivery, no shorting", "MIS": f"intraday, {s.mis_leverage}x leverage, shorting allowed"},
        "order_types": ["MARKET", "LIMIT", "SL", "SL-M"],
        "defaults": {"initial_cash": float(s.default_initial_cash), "risk_limits": {
            "max_order_value": float(s.risk_max_order_value), "max_position_value_per_symbol": float(s.risk_max_position_value_per_symbol),
            "max_daily_loss": float(s.risk_max_daily_loss), "max_orders_per_minute": s.risk_max_orders_per_minute, "max_open_orders": s.risk_max_open_orders,
        }},
        "clock_mode": s.clock_mode,
        "market_data_provider": s.market_data_provider,
        "endpoints": {
            "register": "POST /v1/agents/register",
            "me": "GET /v1/agents/me",
            "portfolio": "GET /v1/agents/me/portfolio",
            "positions": "GET /v1/agents/me/positions",
            "orders": "POST/GET /v1/orders, GET/PATCH/DELETE /v1/orders/{order_id}, DELETE /v1/orders",
            "trades": "GET /v1/agents/me/trades",
            "ledger": "GET /v1/agents/me/ledger",
            "performance": "GET /v1/agents/me/performance",
            "leaderboard": "GET /v1/agents/leaderboard",
            "market": "GET /v1/market/status | /instruments | /quotes | /quote/{symbol} | /ohlc/{symbol}",
            "events": "GET /v1/events?cursor=&wait= (long-poll), GET /v1/stream/events (SSE), GET /v1/stream/quotes?symbols= (SSE)",
        },
    }


ALL_ROUTERS = [meta, agents, orders, market, events, admin]
__all__ = ["ALL_ROUTERS", "LedgerKind"]
