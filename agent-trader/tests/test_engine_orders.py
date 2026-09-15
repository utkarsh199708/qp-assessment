"""Order lifecycle tests for :mod:`agent_trader.engine.core` (engine-orders area).

Everything runs against the shared in-memory engine with a frozen IST clock (Wed 2026-09-16
10:00, NORMAL session) and the seeded simulator, so every price and charge is reproducible.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from agent_trader.charges import Exchange, ProductType, Side, compute_charges
from agent_trader.clock import IST, MarketClock
from agent_trader.config import Settings
from agent_trader.db import Base, make_engine, make_session_factory
from agent_trader.engine import TradingEngine
from agent_trader.engine.errors import Conflict, InvalidRequest, NotFound, OrderRejected
from agent_trader.engine.positions import apply_fill
from agent_trader.marketdata import SimulatedMarketData
from agent_trader.marketdata.simulator import round_to_tick

from .conftest import FROZEN_AT, ltp

TICK = Decimal("0.05")
SLIP = Decimal("0.0005")  # settings.market_slippage_bps = 5
SYMBOL = "RELIANCE"
INITIAL = Decimal("1000000")


# =========================================================================================
# helpers
# =========================================================================================


def D(x) -> Decimal:
    """Exact Decimal from an API float (money is always emitted with <= 4 dp)."""
    return Decimal(str(x))


def quote(engine, symbol=SYMBOL, exchange=Exchange.NSE):
    return engine.quote(symbol, exchange)


def expected_market_buy_price(engine, symbol=SYMBOL, exchange=Exchange.NSE) -> Decimal:
    q = quote(engine, symbol, exchange)
    return round_to_tick(D(q["ask"]) * (1 + SLIP), TICK)


def expected_market_sell_price(engine, symbol=SYMBOL, exchange=Exchange.NSE) -> Decimal:
    q = quote(engine, symbol, exchange)
    return round_to_tick(D(q["bid"]) * (1 - SLIP), TICK)


def charges_for(side, qty, price, product=ProductType.CNC, exchange=Exchange.NSE, dp=False) -> Decimal:
    return compute_charges(
        side=side, product=product, exchange=exchange, quantity=qty, price=price, apply_dp_charge=dp
    ).total


def cash(engine, agent) -> Decimal:
    return D(engine.get_agent(agent)["cash"])


def blocked(engine, agent) -> Decimal:
    return D(engine.get_agent(agent)["blocked_cash"])


def assert_ledger_balances(engine, agent):
    """Invariant: the sum of signed ledger amounts equals the agent's free cash."""
    rows = engine.ledger(agent, limit=1000)
    total = sum((D(r["amount"]) for r in rows), Decimal("0"))
    assert total == cash(engine, agent)
    # the running balance recorded on the newest row also matches
    assert D(rows[0]["cash_after"]) == cash(engine, agent)
    assert D(rows[0]["blocked_after"]) == blocked(engine, agent)


def buy_market(engine, agent, qty=10, symbol=SYMBOL, **kw):
    return engine.place_order(agent, symbol=symbol, side="BUY", quantity=qty, order_type="MARKET", **kw)


def make_engine_with(**overrides):
    """A fresh (settings, clock, market, engine) tuple with settings overrides."""
    settings = Settings(
        database_url="sqlite:///:memory:",
        clock_mode="frozen",
        frozen_at=FROZEN_AT,
        admin_api_key="admin-secret",
        sim_seed=7,
        sim_warmup_candles=30,
        _env_file=None,
        **overrides,
    )
    clock = MarketClock(mode="frozen", frozen_at=settings.frozen_at)
    market = SimulatedMarketData(clock, seed=settings.sim_seed, warmup_candles=settings.sim_warmup_candles)
    db = make_engine(settings.database_url)
    Base.metadata.create_all(db)
    engine = TradingEngine(settings, clock, market, make_session_factory(db))
    return settings, clock, market, engine


# =========================================================================================
# MARKET orders
# =========================================================================================


class TestMarketOrders:
    def test_market_buy_fills_immediately_at_ask_plus_slippage(self, engine, agent):
        exp_price = expected_market_buy_price(engine)
        qty = 10
        o = buy_market(engine, agent, qty, reasoning="momentum", tag="t1")

        assert o["status"] == "FILLED"
        assert o["filled_quantity"] == qty and o["pending_quantity"] == 0
        assert D(o["average_price"]) == exp_price
        assert o["executed_at"] == FROZEN_AT.isoformat()
        assert o["blocked_cash"] == 0
        assert o["reasoning"] == "momentum" and o["tag"] == "t1"
        assert o["status_reason"] is None

        exp_charges = charges_for(Side.BUY, qty, exp_price)
        assert D(o["charges"]) == exp_charges
        assert o["charges_breakdown"]["total"] == str(exp_charges)
        assert D(o["charges_breakdown"]["stt"]) == (exp_price * qty * Decimal("0.001")).quantize(
            Decimal("0.01")
        )

        assert cash(engine, agent) == INITIAL - exp_price * qty - exp_charges
        assert blocked(engine, agent) == 0
        assert_ledger_balances(engine, agent)

        pos = engine.positions(agent)
        assert len(pos) == 1
        p = pos[0]
        assert (p["symbol"], p["exchange"], p["product"], p["quantity"]) == (SYMBOL, "NSE", "CNC", qty)
        assert D(p["average_price"]) == exp_price
        assert p["buy_quantity"] == qty and p["sell_quantity"] == 0

        trades = engine.list_trades(agent)
        assert len(trades) == 1
        assert trades[0]["order_id"] == o["order_id"]
        assert D(trades[0]["price"]) == exp_price and trades[0]["quantity"] == qty
        assert D(trades[0]["charges"]) == exp_charges

    def test_market_buy_ledger_rows_in_order(self, engine, agent):
        exp_price = expected_market_buy_price(engine)
        o = buy_market(engine, agent, 5)
        rows = engine.ledger(agent)  # newest first
        kinds = [r["kind"] for r in reversed(rows)]
        assert kinds == ["DEPOSIT", "MARGIN_BLOCK", "MARGIN_RELEASE", "BUY", "CHARGES"]
        by_kind = {r["kind"]: r for r in rows}
        assert D(by_kind["BUY"]["amount"]) == -(exp_price * 5)
        assert D(by_kind["MARGIN_BLOCK"]["amount"]) == -D(by_kind["MARGIN_RELEASE"]["amount"])
        assert by_kind["BUY"]["ref_id"] == o["order_id"]

    def test_market_sell_of_cnc_holdings_books_realised_pnl_and_dp_charge(self, engine, agent, market):
        qty = 10
        b = buy_market(engine, agent, qty)
        avg = D(b["average_price"])
        cash_after_buy = cash(engine, agent)

        market.set_price(SYMBOL, Exchange.NSE, avg + Decimal("20"))
        exp_price = expected_market_sell_price(engine)
        s = engine.place_order(agent, symbol=SYMBOL, side="SELL", quantity=qty, order_type="MARKET")

        assert s["status"] == "FILLED"
        assert D(s["average_price"]) == exp_price
        exp_charges = charges_for(Side.SELL, qty, exp_price, dp=True)  # first CNC sell of the scrip today
        assert D(s["charges"]) == exp_charges
        assert s["charges_breakdown"]["dp_charge"] == "15.34"
        assert cash(engine, agent) == cash_after_buy + exp_price * qty - exp_charges
        assert blocked(engine, agent) == 0
        assert_ledger_balances(engine, agent)

        _, _, exp_realised = apply_fill(qty, avg, Side.SELL, qty, exp_price)
        trade = engine.list_trades(agent)[0]
        assert trade["side"] == "SELL" and D(trade["realised_pnl"]) == exp_realised
        assert engine.positions(agent) == []
        closed = engine.positions(agent, include_closed=True)
        assert len(closed) == 1 and closed[0]["quantity"] == 0
        assert D(closed[0]["realised_pnl"]) == exp_realised
        assert closed[0]["sell_quantity"] == qty

    def test_second_cnc_sell_same_day_has_no_dp_charge(self, engine, agent):
        buy_market(engine, agent, 10)
        s1 = engine.place_order(agent, symbol=SYMBOL, side="SELL", quantity=4, order_type="MARKET")
        s2 = engine.place_order(agent, symbol=SYMBOL, side="SELL", quantity=4, order_type="MARKET")
        assert s1["charges_breakdown"]["dp_charge"] == "15.34"
        assert s2["charges_breakdown"]["dp_charge"] == "0.00"

    def test_cnc_sell_more_than_held_is_rejected(self, engine, agent):
        buy_market(engine, agent, 3)
        with pytest.raises(OrderRejected) as ei:
            engine.place_order(agent, symbol=SYMBOL, side="SELL", quantity=5, order_type="MARKET")
        assert ei.value.details == {"holdings": 3, "committed": 0, "available": 3}
        assert "MIS" in ei.value.hint

    def test_bse_market_buy_uses_bse_quote_and_charges(self, engine, agent):
        nse_price = expected_market_buy_price(engine, exchange=Exchange.NSE)
        bse_price = expected_market_buy_price(engine, exchange=Exchange.BSE)
        assert nse_price != bse_price  # BSE listing carries a basis

        o = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=7, exchange="BSE", order_type="MARKET"
        )
        assert o["status"] == "FILLED" and o["exchange"] == "BSE"
        assert D(o["average_price"]) == bse_price
        exp_charges = charges_for(Side.BUY, 7, bse_price, exchange=Exchange.BSE)
        assert D(o["charges"]) == exp_charges
        # BSE exchange txn rate 0.00375% (vs NSE 0.00297%)
        assert D(o["charges_breakdown"]["exchange_txn"]) == (bse_price * 7 * Decimal("0.0000375")).quantize(
            Decimal("0.01")
        )
        pos = engine.positions(agent)
        assert len(pos) == 1 and pos[0]["exchange"] == "BSE"
        assert cash(engine, agent) == INITIAL - bse_price * 7 - exp_charges
        assert_ledger_balances(engine, agent)

    def test_symbol_is_normalised(self, engine, agent):
        o = engine.place_order(agent, symbol=" reliance ", side="BUY", quantity=1, order_type="MARKET")
        assert o["symbol"] == "RELIANCE" and o["status"] == "FILLED"


# =========================================================================================
# LIMIT orders
# =========================================================================================


class TestLimitOrders:
    def test_limit_buy_below_market_rests_then_fills_at_ask(self, engine, agent, market):
        q = quote(engine)
        limit_px = round_to_tick(D(q["ltp"]) - Decimal("10"), TICK)
        qty = 10
        o = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=qty, order_type="LIMIT", price=limit_px
        )

        assert o["status"] == "OPEN" and o["filled_quantity"] == 0
        assert o["status_reason"] is None
        assert o["expires_at"] == datetime(2026, 9, 16, 15, 30, tzinfo=IST).isoformat()
        est = charges_for(Side.BUY, qty, limit_px)
        exp_block = limit_px * qty + est
        assert D(o["blocked_cash"]) == exp_block
        assert blocked(engine, agent) == exp_block
        assert cash(engine, agent) == INITIAL - exp_block
        assert engine.list_orders(agent, status="open")[0]["order_id"] == o["order_id"]
        assert_ledger_balances(engine, agent)

        # price drops so the ask is at/below the limit → fills on the next tick at the ask
        market.set_price(SYMBOL, Exchange.NSE, limit_px - Decimal("1"))
        ask = D(quote(engine)["ask"])
        assert ask <= limit_px
        stats = engine.tick()
        assert stats["fills"] == 1

        f = engine.get_order(agent, o["order_id"])
        assert f["status"] == "FILLED"
        assert D(f["average_price"]) == ask
        assert f["blocked_cash"] == 0
        assert blocked(engine, agent) == 0
        exp_charges = charges_for(Side.BUY, qty, ask)
        assert cash(engine, agent) == INITIAL - ask * qty - exp_charges
        assert_ledger_balances(engine, agent)
        assert engine.list_orders(agent, status="open") == []

    def test_marketable_limit_buy_fills_immediately_at_ask_not_limit(self, engine, agent):
        q = quote(engine)
        ask = D(q["ask"])
        limit_px = round_to_tick(ask + Decimal("5"), TICK)
        o = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=4, order_type="LIMIT", price=limit_px
        )
        assert o["status"] == "FILLED"
        assert D(o["average_price"]) == ask
        assert D(o["price"]) == limit_px
        # the block was based on the limit price; after the fill only the actual cost is gone
        assert blocked(engine, agent) == 0
        assert cash(engine, agent) == INITIAL - ask * 4 - charges_for(Side.BUY, 4, ask)
        assert_ledger_balances(engine, agent)

    def test_limit_sell_above_market_rests_then_fills_at_bid(self, engine, agent, market):
        buy_market(engine, agent, 10)
        cash_after_buy = cash(engine, agent)
        q = quote(engine)
        limit_px = round_to_tick(D(q["ltp"]) + Decimal("10"), TICK)
        o = engine.place_order(
            agent, symbol=SYMBOL, side="SELL", quantity=10, order_type="LIMIT", price=limit_px
        )
        assert o["status"] == "OPEN"
        assert o["blocked_cash"] == 0  # CNC sells block nothing
        assert blocked(engine, agent) == 0
        assert cash(engine, agent) == cash_after_buy

        market.set_price(SYMBOL, Exchange.NSE, limit_px + Decimal("1"))
        bid = D(quote(engine)["bid"])
        assert bid >= limit_px
        engine.tick()
        f = engine.get_order(agent, o["order_id"])
        assert f["status"] == "FILLED" and D(f["average_price"]) == bid
        exp_charges = charges_for(Side.SELL, 10, bid, dp=True)
        assert cash(engine, agent) == cash_after_buy + bid * 10 - exp_charges
        assert engine.positions(agent) == []
        assert_ledger_balances(engine, agent)

    def test_resting_limit_does_not_fill_while_not_marketable(self, engine, agent, market):
        q = quote(engine)
        limit_px = round_to_tick(D(q["ltp"]) - Decimal("10"), TICK)
        o = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="LIMIT", price=limit_px
        )
        market.set_price(SYMBOL, Exchange.NSE, limit_px)  # ask = limit + 0.05 → still not marketable
        engine.tick()
        assert engine.get_order(agent, o["order_id"])["status"] == "OPEN"

    def test_committed_sell_quantity_blocks_oversell(self, engine, agent):
        buy_market(engine, agent, 10)
        q = quote(engine)
        limit_px = round_to_tick(D(q["ltp"]) + Decimal("10"), TICK)
        engine.place_order(agent, symbol=SYMBOL, side="SELL", quantity=6, order_type="LIMIT", price=limit_px)
        with pytest.raises(OrderRejected) as ei:
            engine.place_order(
                agent, symbol=SYMBOL, side="SELL", quantity=5, order_type="LIMIT", price=limit_px
            )
        assert ei.value.details == {"holdings": 10, "committed": 6, "available": 4}
        # exactly the available quantity is fine
        ok = engine.place_order(
            agent, symbol=SYMBOL, side="SELL", quantity=4, order_type="LIMIT", price=limit_px
        )
        assert ok["status"] == "OPEN"


# =========================================================================================
# Stop orders
# =========================================================================================


class TestStopOrders:
    def test_sl_sell_triggers_and_fills_at_bid(self, engine, agent, market):
        buy_market(engine, agent, 10)
        cash_after_buy = cash(engine, agent)
        px = ltp(engine, SYMBOL)
        trigger = round_to_tick(px - Decimal("10"), TICK)
        price = trigger - Decimal("2")
        o = engine.place_order(
            agent,
            symbol=SYMBOL,
            side="SELL",
            quantity=10,
            order_type="SL",
            price=price,
            trigger_price=trigger,
        )
        assert o["status"] == "OPEN" and o["triggered"] is False
        assert D(o["trigger_price"]) == trigger and D(o["price"]) == price

        # above the trigger: nothing happens
        market.set_price(SYMBOL, Exchange.NSE, trigger + TICK)
        engine.tick()
        assert engine.get_order(agent, o["order_id"])["triggered"] is False

        cursor = engine.bus.cursor
        market.set_price(SYMBOL, Exchange.NSE, trigger)
        bid = D(quote(engine)["bid"])
        engine.tick()
        f = engine.get_order(agent, o["order_id"])
        assert f["triggered"] is True
        assert f["status"] == "FILLED" and D(f["average_price"]) == bid
        types = [e["type"] for e in engine.events_since(agent, cursor)["events"]]
        assert types.index("ORDER_TRIGGERED") < types.index("ORDER_FILLED")
        trig_ev = next(
            e for e in engine.events_since(agent, cursor)["events"] if e["type"] == "ORDER_TRIGGERED"
        )
        assert trig_ev["order_id"] == o["order_id"] and D(trig_ev["trigger_price"]) == trigger
        assert cash(engine, agent) == cash_after_buy + bid * 10 - charges_for(Side.SELL, 10, bid, dp=True)
        assert_ledger_balances(engine, agent)

    def test_sl_m_buy_triggers_and_fills_at_market(self, engine, agent, market):
        px = ltp(engine, SYMBOL)
        trigger = round_to_tick(px + Decimal("10"), TICK)
        o = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=5, order_type="SL-M", trigger_price=trigger
        )
        assert o["status"] == "OPEN" and o["price"] is None
        # SL-M blocks at the trigger price
        assert D(o["blocked_cash"]) == trigger * 5 + charges_for(Side.BUY, 5, trigger)

        market.set_price(SYMBOL, Exchange.NSE, trigger + Decimal("3"))
        exp_price = expected_market_buy_price(engine)
        engine.tick()
        f = engine.get_order(agent, o["order_id"])
        assert f["status"] == "FILLED" and f["triggered"] is True
        assert D(f["average_price"]) == exp_price
        assert f["blocked_cash"] == 0 and blocked(engine, agent) == 0
        assert cash(engine, agent) == INITIAL - exp_price * 5 - charges_for(Side.BUY, 5, exp_price)
        assert engine.positions(agent)[0]["quantity"] == 5
        assert_ledger_balances(engine, agent)

    def test_sl_buy_trigger_must_be_above_ltp(self, engine, agent):
        px = ltp(engine, SYMBOL)
        with pytest.raises(InvalidRequest) as ei:
            engine.place_order(
                agent,
                symbol=SYMBOL,
                side="BUY",
                quantity=1,
                order_type="SL-M",
                trigger_price=px - Decimal("5"),
            )
        assert "must be above the last price" in ei.value.message
        assert ei.value.hint and "MARKET/LIMIT" in ei.value.hint
        with pytest.raises(InvalidRequest):  # equal to ltp is also invalid
            engine.place_order(
                agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="SL-M", trigger_price=px
            )

    def test_sl_sell_trigger_must_be_below_ltp(self, engine, agent):
        px = ltp(engine, SYMBOL)
        with pytest.raises(InvalidRequest) as ei:
            engine.place_order(
                agent,
                symbol=SYMBOL,
                side="SELL",
                quantity=1,
                order_type="SL",
                price=px + Decimal("5"),
                trigger_price=px + Decimal("5"),
            )
        assert "must be below the last price" in ei.value.message

    def test_sl_buy_price_must_be_at_least_trigger(self, engine, agent):
        px = ltp(engine, SYMBOL)
        trigger = px + Decimal("10")
        with pytest.raises(InvalidRequest) as ei:
            engine.place_order(
                agent,
                symbol=SYMBOL,
                side="BUY",
                quantity=1,
                order_type="SL",
                price=trigger - Decimal("1"),
                trigger_price=trigger,
            )
        assert "price must be >= trigger_price" in ei.value.message

    def test_sl_sell_price_must_be_at_most_trigger(self, engine, agent):
        buy_market(engine, agent, 1)
        px = ltp(engine, SYMBOL)
        trigger = px - Decimal("10")
        with pytest.raises(InvalidRequest) as ei:
            engine.place_order(
                agent,
                symbol=SYMBOL,
                side="SELL",
                quantity=1,
                order_type="SL",
                price=trigger + Decimal("1"),
                trigger_price=trigger,
            )
        assert "price must be <= trigger_price" in ei.value.message

    def test_stop_orders_require_trigger_and_limit_forbids_trigger(self, engine, agent):
        with pytest.raises(InvalidRequest, match="require trigger_price"):
            engine.place_order(agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="SL-M")
        with pytest.raises(InvalidRequest, match="require price"):
            engine.place_order(
                agent,
                symbol=SYMBOL,
                side="BUY",
                quantity=1,
                order_type="SL",
                trigger_price=ltp(engine, SYMBOL) + 5,
            )
        with pytest.raises(InvalidRequest) as ei:
            engine.place_order(
                agent,
                symbol=SYMBOL,
                side="BUY",
                quantity=1,
                order_type="LIMIT",
                price=ltp(engine, SYMBOL),
                trigger_price=ltp(engine, SYMBOL),
            )
        assert "must not carry trigger_price" in ei.value.message and "SL-M" in ei.value.hint


# =========================================================================================
# Validity, sessions and expiry
# =========================================================================================


class TestValidityAndSessions:
    def test_ioc_limit_not_fillable_is_cancelled(self, engine, agent):
        q = quote(engine)
        limit_px = round_to_tick(D(q["ltp"]) - Decimal("10"), TICK)
        cursor = engine.bus.cursor
        o = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=3, order_type="LIMIT", price=limit_px, validity="IOC"
        )
        assert o["status"] == "CANCELLED"
        assert "IOC" in o["status_reason"]
        assert o["blocked_cash"] == 0
        assert blocked(engine, agent) == 0 and cash(engine, agent) == INITIAL
        types = [e["type"] for e in engine.events_since(agent, cursor)["events"]]
        assert types == ["ORDER_PLACED", "ORDER_CANCELLED"]
        assert_ledger_balances(engine, agent)

    def test_ioc_marketable_limit_fills(self, engine, agent):
        ask = D(quote(engine)["ask"])
        o = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=3, order_type="LIMIT", price=ask, validity="IOC"
        )
        assert o["status"] == "FILLED" and o["validity"] == "IOC"

    def test_ioc_rejected_when_market_closed(self, engine, agent, clock):
        clock.set(datetime(2026, 9, 16, 16, 30, tzinfo=IST))
        with pytest.raises(OrderRejected) as ei:
            engine.place_order(
                agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="MARKET", validity="IOC"
            )
        assert "IOC orders need an open market" in ei.value.message
        assert "CLOSED" in ei.value.hint and "2026-09-17T09:15:00+05:30" in ei.value.hint
        assert engine.list_orders(agent) == []

    def test_after_close_order_queues_and_fills_at_next_open(self, engine, agent, clock):
        clock.set(datetime(2026, 9, 16, 16, 30, tzinfo=IST))
        o = buy_market(engine, agent, 5)
        assert o["status"] == "OPEN"
        assert "queued" in o["status_reason"]
        assert "2026-09-17T09:15:00+05:30" in o["status_reason"]
        assert o["expires_at"] == datetime(2026, 9, 17, 15, 30, tzinfo=IST).isoformat()
        assert D(o["blocked_cash"]) > 0
        assert blocked(engine, agent) == D(o["blocked_cash"])

        # still closed later that evening and in the pre-open of the next day
        clock.set(datetime(2026, 9, 17, 9, 5, tzinfo=IST))
        engine.tick()
        assert engine.get_order(agent, o["order_id"])["status"] == "OPEN"

        clock.set(datetime(2026, 9, 17, 9, 15, tzinfo=IST))
        stats = engine.tick()
        assert stats["phase"] == "NORMAL" and stats["fills"] == 1
        f = engine.get_order(agent, o["order_id"])
        assert f["status"] == "FILLED" and f["status_reason"] is None
        # (prefix match: rows re-read from SQLite lose their tzinfo, see test_timestamps_keep_ist_offset_after_db_round_trip)
        assert f["executed_at"].startswith("2026-09-17T09:15:00")
        # filled at the open's market price (quote after the tick's step is the fill quote)
        assert D(f["average_price"]) == expected_market_buy_price(engine)
        assert blocked(engine, agent) == 0
        assert_ledger_balances(engine, agent)

    def test_holiday_order_queues_for_next_trading_day(self, engine, agent, clock):
        clock.set(datetime(2026, 9, 14, 12, 0, tzinfo=IST))  # Ganesh Chaturthi (Monday)
        assert engine.market_status()["phase"] == "HOLIDAY"
        o = buy_market(engine, agent, 2)
        assert o["status"] == "OPEN"
        assert "HOLIDAY" in o["status_reason"] and "2026-09-15T09:15:00+05:30" in o["status_reason"]
        assert o["expires_at"] == datetime(2026, 9, 15, 15, 30, tzinfo=IST).isoformat()

        clock.set(datetime(2026, 9, 15, 9, 15, tzinfo=IST))
        engine.tick()
        assert engine.get_order(agent, o["order_id"])["status"] == "FILLED"

    def test_weekend_order_queues_for_monday(self, engine, agent, clock):
        clock.set(datetime(2026, 9, 19, 11, 0, tzinfo=IST))  # Saturday
        o = buy_market(engine, agent, 2)
        assert o["status"] == "OPEN"
        assert o["expires_at"] == datetime(2026, 9, 21, 15, 30, tzinfo=IST).isoformat()

    def test_pre_open_order_queues_then_fills_at_open(self, engine, agent, clock):
        clock.set(datetime(2026, 9, 16, 9, 5, tzinfo=IST))
        o = buy_market(engine, agent, 2)
        assert o["status"] == "OPEN" and "PRE_OPEN" in o["status_reason"]
        assert o["expires_at"] == datetime(2026, 9, 16, 15, 30, tzinfo=IST).isoformat()
        engine.tick()  # still pre-open: nothing happens
        assert engine.get_order(agent, o["order_id"])["status"] == "OPEN"

        clock.set(datetime(2026, 9, 16, 9, 15, tzinfo=IST))
        engine.tick()
        f = engine.get_order(agent, o["order_id"])
        assert f["status"] == "FILLED" and f["status_reason"] is None
        assert D(f["average_price"]) == expected_market_buy_price(engine)

    def test_day_order_expires_at_close_and_releases_block(self, engine, agent, clock):
        q = quote(engine)
        limit_px = round_to_tick(D(q["ltp"]) - Decimal("10"), TICK)
        o = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=10, order_type="LIMIT", price=limit_px
        )
        block = D(o["blocked_cash"])
        assert block > 0

        clock.set(datetime(2026, 9, 16, 15, 29, tzinfo=IST))
        engine.tick()
        assert engine.get_order(agent, o["order_id"])["status"] == "OPEN"

        cursor = engine.bus.cursor
        clock.set(datetime(2026, 9, 16, 15, 30, tzinfo=IST))
        stats = engine.tick()
        assert stats["expired"] == 1
        f = engine.get_order(agent, o["order_id"])
        assert f["status"] == "EXPIRED"
        assert "expired" in f["status_reason"]
        assert f["blocked_cash"] == 0
        assert blocked(engine, agent) == 0 and cash(engine, agent) == INITIAL
        evs = engine.events_since(agent, cursor)["events"]
        assert any(e["type"] == "ORDER_EXPIRED" and e["order"]["order_id"] == o["order_id"] for e in evs)
        assert engine.list_orders(agent, status="EXPIRED")[0]["order_id"] == o["order_id"]
        assert engine.list_orders(agent, status="open") == []
        assert_ledger_balances(engine, agent)

    def test_mis_order_rejected_after_square_off(self, engine, agent, clock):
        clock.set(datetime(2026, 9, 16, 15, 21, tzinfo=IST))
        with pytest.raises(OrderRejected) as ei:
            engine.place_order(
                agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="MARKET", product="MIS"
            )
        assert "15:20" in ei.value.message


# =========================================================================================
# cancel / modify / get
# =========================================================================================


class TestCancelModifyGet:
    def _resting_buy(self, engine, agent, qty=10, offset=Decimal("10")):
        q = quote(engine)
        limit_px = round_to_tick(D(q["ltp"]) - offset, TICK)
        return limit_px, engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=qty, order_type="LIMIT", price=limit_px
        )

    def test_cancel_releases_block_and_emits_event(self, engine, agent):
        _, o = self._resting_buy(engine, agent)
        assert blocked(engine, agent) == D(o["blocked_cash"]) > 0
        cursor = engine.bus.cursor
        c = engine.cancel_order(agent, o["order_id"])
        assert c["status"] == "CANCELLED" and c["status_reason"] == "cancelled by agent"
        assert c["blocked_cash"] == 0
        assert blocked(engine, agent) == 0 and cash(engine, agent) == INITIAL
        evs = engine.events_since(agent, cursor)["events"]
        assert [e["type"] for e in evs] == ["ORDER_CANCELLED"]
        assert evs[0]["order"]["order_id"] == o["order_id"] and evs[0]["order"]["status"] == "CANCELLED"
        assert engine.ledger(agent)[0]["kind"] == "MARGIN_RELEASE"
        assert engine.ledger(agent)[0]["note"] == "released on cancel"
        assert_ledger_balances(engine, agent)

    def test_cancel_filled_order_is_conflict(self, engine, agent):
        o = buy_market(engine, agent, 1)
        with pytest.raises(Conflict) as ei:
            engine.cancel_order(agent, o["order_id"])
        assert ei.value.code == "CONFLICT" and "FILLED" in ei.value.message
        assert ei.value.to_dict()["error"] == "CONFLICT"

    def test_cancel_twice_is_conflict_and_unknown_is_not_found(self, engine, agent):
        _, o = self._resting_buy(engine, agent)
        engine.cancel_order(agent, o["order_id"])
        with pytest.raises(Conflict):
            engine.cancel_order(agent, o["order_id"])
        with pytest.raises(NotFound):
            engine.cancel_order(agent, "ord_doesnotexist")

    def test_cancel_all_orders(self, engine, agent):
        _, a = self._resting_buy(engine, agent, qty=1)
        _, b = self._resting_buy(engine, agent, qty=2)
        buy_market(engine, agent, 1)  # filled; untouched
        out = engine.cancel_all_orders(agent)
        assert {o["order_id"] for o in out} == {a["order_id"], b["order_id"]}
        assert all(o["status"] == "CANCELLED" and "cancel_all" in o["status_reason"] for o in out)
        assert engine.list_orders(agent, status="open") == []
        assert blocked(engine, agent) == 0
        assert_ledger_balances(engine, agent)

    def test_other_agent_cannot_see_or_cancel_order(self, engine, agent):
        other, _ = engine.register_agent("other")
        _, o = self._resting_buy(engine, agent)
        with pytest.raises(NotFound):
            engine.get_order(other["agent_id"], o["order_id"])
        with pytest.raises(NotFound):
            engine.cancel_order(other["agent_id"], o["order_id"])
        assert engine.get_order(agent, o["order_id"])["status"] == "OPEN"

    def test_modify_quantity_and_price_reblocks(self, engine, agent):
        limit_px, o = self._resting_buy(engine, agent, qty=10)
        new_px = limit_px - Decimal("5")
        cursor = engine.bus.cursor
        m = engine.modify_order(agent, o["order_id"], quantity=20, price=new_px)
        assert m["order_id"] == o["order_id"]
        assert m["status"] == "OPEN" and m["quantity"] == 20 and D(m["price"]) == new_px
        exp_block = new_px * 20 + charges_for(Side.BUY, 20, new_px)
        assert D(m["blocked_cash"]) == exp_block
        assert blocked(engine, agent) == exp_block
        assert cash(engine, agent) == INITIAL - exp_block
        evs = engine.events_since(agent, cursor)["events"]
        assert [e["type"] for e in evs] == ["ORDER_MODIFIED"]
        assert evs[0]["order"]["quantity"] == 20
        assert_ledger_balances(engine, agent)

    def test_modify_reduce_quantity_releases_cash(self, engine, agent):
        limit_px, o = self._resting_buy(engine, agent, qty=10)
        before = D(o["blocked_cash"])
        m = engine.modify_order(agent, o["order_id"], quantity=4)
        exp_block = limit_px * 4 + charges_for(Side.BUY, 4, limit_px)
        assert D(m["blocked_cash"]) == exp_block < before
        assert blocked(engine, agent) == exp_block
        assert_ledger_balances(engine, agent)

    def test_modify_makes_resting_order_marketable_and_fills(self, engine, agent):
        _, o = self._resting_buy(engine, agent, qty=5)
        ask = D(quote(engine)["ask"])
        m = engine.modify_order(agent, o["order_id"], price=ask)
        assert m["status"] == "FILLED" and D(m["average_price"]) == ask
        assert m["blocked_cash"] == 0 and blocked(engine, agent) == 0
        assert cash(engine, agent) == INITIAL - ask * 5 - charges_for(Side.BUY, 5, ask)
        assert_ledger_balances(engine, agent)

    def test_modify_market_order_with_price_is_invalid(self, engine, agent, clock):
        clock.set(datetime(2026, 9, 16, 16, 30, tzinfo=IST))  # AMO stays OPEN so it is modifiable
        o = buy_market(engine, agent, 2)
        assert o["status"] == "OPEN"
        with pytest.raises(InvalidRequest) as ei:
            engine.modify_order(agent, o["order_id"], price=ltp(engine, SYMBOL))
        assert "MARKET orders have no price" in ei.value.message
        with pytest.raises(InvalidRequest):
            engine.modify_order(agent, o["order_id"], trigger_price=ltp(engine, SYMBOL))
        # the failed modification changed nothing
        after = engine.get_order(agent, o["order_id"])
        assert after["quantity"] == 2 and D(after["blocked_cash"]) == D(o["blocked_cash"])
        assert blocked(engine, agent) == D(o["blocked_cash"])

    def test_modify_validation(self, engine, agent):
        limit_px, o = self._resting_buy(engine, agent, qty=10)
        with pytest.raises(InvalidRequest):
            engine.modify_order(agent, o["order_id"], quantity=0)
        with pytest.raises(InvalidRequest, match="tick size"):
            engine.modify_order(agent, o["order_id"], price=limit_px + Decimal("0.02"))
        with pytest.raises(OrderRejected, match="price band"):
            engine.modify_order(agent, o["order_id"], price=D(quote(engine)["upper_circuit"]) + TICK)
        filled = buy_market(engine, agent, 1)
        with pytest.raises(Conflict):
            engine.modify_order(agent, filled["order_id"], quantity=2)
        # failed modifications leave the block intact
        assert D(engine.get_order(agent, o["order_id"])["blocked_cash"]) == D(o["blocked_cash"])
        assert_ledger_balances(engine, agent)

    def test_modify_insufficient_funds_cancels_order(self, engine, agent):
        limit_px, o = self._resting_buy(engine, agent, qty=10)
        engine.update_risk_limits(
            agent, {"max_order_value": "100000000", "max_position_value_per_symbol": "100000000"}
        )
        with pytest.raises(OrderRejected) as ei:
            engine.modify_order(agent, o["order_id"], quantity=5000)  # ~₹72 lakh > ₹10 lakh
        assert "cancelled" in ei.value.message
        assert engine.get_order(agent, o["order_id"])["status"] == "CANCELLED"
        assert blocked(engine, agent) == 0 and cash(engine, agent) == INITIAL
        assert_ledger_balances(engine, agent)

    def test_get_order_by_client_order_id(self, engine, agent):
        o = buy_market(engine, agent, 1, client_order_id="my-cid-1")
        assert o["client_order_id"] == "my-cid-1"
        assert engine.get_order(agent, "my-cid-1")["order_id"] == o["order_id"]
        assert engine.get_order(agent, o["order_id"])["client_order_id"] == "my-cid-1"
        with pytest.raises(NotFound) as ei:
            engine.get_order(agent, "nope")
        assert ei.value.code == "NOT_FOUND"

    def test_cancel_by_client_order_id(self, engine, agent):
        q = quote(engine)
        limit_px = round_to_tick(D(q["ltp"]) - Decimal("10"), TICK)
        engine.place_order(
            agent,
            symbol=SYMBOL,
            side="BUY",
            quantity=1,
            order_type="LIMIT",
            price=limit_px,
            client_order_id="c-2",
        )
        assert engine.cancel_order(agent, "c-2")["status"] == "CANCELLED"


# =========================================================================================
# idempotency
# =========================================================================================


class TestIdempotency:
    def test_same_client_order_id_same_params_replays_without_double_fill(self, engine, agent):
        o1 = buy_market(engine, agent, 3, client_order_id="idem-1")
        cash_after = cash(engine, agent)
        o2 = buy_market(engine, agent, 3, client_order_id="idem-1")
        assert o2["idempotent_replay"] is True
        assert "idempotent_replay" not in o1
        assert o2["order_id"] == o1["order_id"]
        assert o2["status"] == "FILLED" and o2["filled_quantity"] == 3
        assert cash(engine, agent) == cash_after
        assert len(engine.list_orders(agent)) == 1
        assert len(engine.list_trades(agent)) == 1
        assert engine.positions(agent)[0]["quantity"] == 3

    def test_same_client_order_id_different_params_is_conflict(self, engine, agent):
        o1 = buy_market(engine, agent, 3, client_order_id="idem-2")
        with pytest.raises(Conflict) as ei:
            buy_market(engine, agent, 4, client_order_id="idem-2")
        assert ei.value.details == {"existing_order_id": o1["order_id"]}
        assert "fresh client_order_id" in ei.value.hint
        with pytest.raises(Conflict):
            engine.place_order(
                agent, symbol=SYMBOL, side="SELL", quantity=3, order_type="MARKET", client_order_id="idem-2"
            )
        assert len(engine.list_orders(agent)) == 1

    def test_limit_replay_compares_price(self, engine, agent):
        q = quote(engine)
        px = round_to_tick(D(q["ltp"]) - Decimal("10"), TICK)
        o1 = engine.place_order(
            agent,
            symbol=SYMBOL,
            side="BUY",
            quantity=1,
            order_type="LIMIT",
            price=px,
            client_order_id="idem-3",
        )
        o2 = engine.place_order(
            agent,
            symbol=SYMBOL,
            side="BUY",
            quantity=1,
            order_type="LIMIT",
            price=px,
            client_order_id="idem-3",
        )
        assert o2["order_id"] == o1["order_id"] and o2["idempotent_replay"] is True
        assert blocked(engine, agent) == D(o1["blocked_cash"])  # blocked only once
        with pytest.raises(Conflict):
            engine.place_order(
                agent,
                symbol=SYMBOL,
                side="BUY",
                quantity=1,
                order_type="LIMIT",
                price=px - TICK,
                client_order_id="idem-3",
            )

    def test_client_order_ids_are_scoped_per_agent(self, engine, agent):
        other, _ = engine.register_agent("other")
        a = buy_market(engine, agent, 1, client_order_id="shared")
        b = buy_market(engine, other["agent_id"], 1, client_order_id="shared")
        assert a["order_id"] != b["order_id"] and "idempotent_replay" not in b


# =========================================================================================
# validation errors
# =========================================================================================


class TestValidation:
    def test_price_not_on_tick(self, engine, agent):
        with pytest.raises(InvalidRequest) as ei:
            engine.place_order(
                agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="LIMIT", price=Decimal("1400.02")
            )
        e = ei.value
        assert e.code == "INVALID_REQUEST"
        assert "not a multiple of tick size 0.05" in e.message
        assert e.hint == "Round to 0.05: e.g. 1400.00"
        assert e.details == {"tick_size": 0.05, "suggested": 1400.0}
        d = e.to_dict()
        assert d["error"] == "INVALID_REQUEST" and d["details"]["suggested"] == 1400.0
        assert engine.list_orders(agent) == [] and cash(engine, agent) == INITIAL

    def test_trigger_price_not_on_tick(self, engine, agent):
        px = ltp(engine, SYMBOL)
        with pytest.raises(InvalidRequest, match="trigger_price .* tick size"):
            engine.place_order(
                agent,
                symbol=SYMBOL,
                side="BUY",
                quantity=1,
                order_type="SL-M",
                trigger_price=px + Decimal("5.03"),
            )

    def test_price_outside_band(self, engine, agent):
        q = quote(engine)
        upper, lower = D(q["upper_circuit"]), D(q["lower_circuit"])
        with pytest.raises(OrderRejected) as ei:
            engine.place_order(
                agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="LIMIT", price=upper + TICK
            )
        assert ei.value.code == "ORDER_REJECTED" and ei.value.http_status == 422
        assert "outside today's price band" in ei.value.message
        assert ei.value.details == {"lower_circuit": float(lower), "upper_circuit": float(upper)}
        assert ei.value.hint
        with pytest.raises(OrderRejected):
            engine.place_order(
                agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="LIMIT", price=lower - TICK
            )
        # exactly at the band edge is accepted
        o = engine.place_order(agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="LIMIT", price=lower)
        assert o["status"] == "OPEN"

    def test_market_with_price_is_invalid(self, engine, agent):
        with pytest.raises(InvalidRequest) as ei:
            engine.place_order(
                agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="MARKET", price=Decimal("1400")
            )
        assert "must not carry price" in ei.value.message
        assert "LIMIT" in ei.value.hint

    def test_limit_without_price_is_invalid(self, engine, agent):
        with pytest.raises(InvalidRequest) as ei:
            engine.place_order(agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="LIMIT")
        assert ei.value.message == "LIMIT orders require price"

    def test_non_positive_price(self, engine, agent):
        with pytest.raises(InvalidRequest, match="price must be positive"):
            engine.place_order(
                agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="LIMIT", price=Decimal("0")
            )

    def test_unknown_symbol_is_not_found(self, engine, agent):
        with pytest.raises(NotFound) as ei:
            engine.place_order(agent, symbol="NOPE", side="BUY", quantity=1, order_type="MARKET")
        assert ei.value.code == "NOT_FOUND" and ei.value.http_status == 404
        assert "unknown instrument NOPE on NSE" in ei.value.message
        assert "search_instruments" in ei.value.hint

    def test_quantity_must_be_positive(self, engine, agent):
        for q in (0, -5):
            with pytest.raises(InvalidRequest, match="quantity must be a positive integer"):
                engine.place_order(agent, symbol=SYMBOL, side="BUY", quantity=q, order_type="MARKET")
        assert engine.list_orders(agent) == []

    def test_unknown_agent_is_not_found(self, engine):
        with pytest.raises(NotFound):
            engine.place_order("agt_nope", symbol=SYMBOL, side="BUY", quantity=1)

    def test_invalid_enum_values_raise_invalid_request(self, engine, agent):
        with pytest.raises(InvalidRequest, match="not a valid Side"):
            engine.place_order(agent, symbol=SYMBOL, side="LONG", quantity=1)
        with pytest.raises(InvalidRequest, match="not a valid OrderType"):
            engine.place_order(agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="STOP")
        assert engine.list_orders(agent) == []

    def test_insufficient_funds(self, engine, agent):
        engine.update_risk_limits(
            agent, {"max_order_value": "100000000", "max_position_value_per_symbol": "100000000"}
        )
        with pytest.raises(OrderRejected) as ei:
            buy_market(engine, agent, 1000)  # ≈ ₹14.6 lakh > ₹10 lakh
        assert "insufficient funds" in ei.value.message
        assert ei.value.details["available_cash"] == float(INITIAL)
        assert ei.value.details["required"] > float(INITIAL)
        assert "20% margin" in ei.value.hint
        assert engine.list_orders(agent) == []

    def test_max_order_value_limit(self, engine, agent):
        with pytest.raises(OrderRejected) as ei:
            buy_market(engine, agent, 400)  # ≈ ₹5.84 lakh > default ₹5 lakh
        assert "max_order_value" in ei.value.message
        assert ei.value.details["max_order_value"] == 500000.0

    def test_validation_failure_leaves_only_an_audit_event(self, engine, agent):
        cursor = engine.bus.cursor
        with pytest.raises(InvalidRequest):
            engine.place_order(agent, symbol=SYMBOL, side="BUY", quantity=0)
        events = engine.events_since(agent, cursor)["events"]
        assert [e["type"] for e in events] == ["ORDER_REJECTED"]
        assert engine.list_orders(agent) == []
        assert len(engine.ledger(agent)) == 1  # only the initial deposit
        assert_ledger_balances(engine, agent)


# =========================================================================================
# partial fills
# =========================================================================================


class TestPartialFills:
    def test_limit_fills_across_ticks_with_proportional_block_release(self):
        settings, clock, market, engine = make_engine_with(max_fill_fraction_per_tick=0.5)
        assert settings.max_fill_fraction_per_tick == 0.5
        a, _ = engine.register_agent("partial")
        agent = a["agent_id"]

        q = engine.quote(SYMBOL)
        limit_px = round_to_tick(D(q["ltp"]) - Decimal("10"), TICK)
        qty = 10
        o = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=qty, order_type="LIMIT", price=limit_px
        )
        assert o["status"] == "OPEN"
        block0 = D(o["blocked_cash"])

        market.set_price(SYMBOL, Exchange.NSE, limit_px - Decimal("1"))
        ask = D(engine.quote(SYMBOL)["ask"])
        cursor = engine.bus.cursor
        engine.tick()
        p = engine.get_order(agent, o["order_id"])
        assert p["status"] == "PARTIALLY_FILLED"
        assert p["filled_quantity"] == 5 and p["pending_quantity"] == 5
        assert D(p["average_price"]) == ask
        assert D(p["blocked_cash"]) == (block0 / 2).quantize(Decimal("0.01"))
        assert blocked(engine, agent) == D(p["blocked_cash"])
        c5 = charges_for(Side.BUY, 5, ask)
        assert D(p["charges"]) == c5
        assert cash(engine, agent) == INITIAL - D(p["blocked_cash"]) - ask * 5 - c5
        evs = engine.events_since(agent, cursor)["events"]
        assert "ORDER_PARTIALLY_FILLED" in [e["type"] for e in evs]
        assert engine.list_orders(agent, status="open")[0]["order_id"] == o["order_id"]
        assert engine.positions(agent)[0]["quantity"] == 5
        assert_ledger_balances(engine, agent)

        # second tick (price unchanged, clock unchanged) completes the order
        engine.tick()
        f = engine.get_order(agent, o["order_id"])
        assert f["status"] == "FILLED" and f["filled_quantity"] == qty
        assert D(f["average_price"]) == ask
        assert f["blocked_cash"] == 0 and blocked(engine, agent) == 0
        assert D(f["charges"]) == c5 * 2
        assert cash(engine, agent) == INITIAL - ask * qty - c5 * 2
        trades = engine.list_trades(agent)
        assert [t["quantity"] for t in trades] == [5, 5]
        assert engine.positions(agent)[0]["quantity"] == qty
        assert engine.list_orders(agent, status="open") == []
        assert_ledger_balances(engine, agent)

    def test_market_order_partial_then_complete(self):
        _, clock, market, engine = make_engine_with(max_fill_fraction_per_tick=0.4)
        a, _ = engine.register_agent("partial")
        agent = a["agent_id"]
        o = buy_market(engine, agent, 10)
        assert o["status"] == "PARTIALLY_FILLED" and o["filled_quantity"] == 4
        engine.tick()
        assert engine.get_order(agent, o["order_id"])["filled_quantity"] == 8
        engine.tick()
        f = engine.get_order(agent, o["order_id"])
        assert f["status"] == "FILLED" and f["filled_quantity"] == 10
        assert [t["quantity"] for t in engine.list_trades(agent)] == [2, 4, 4]
        assert blocked(engine, agent) == 0
        assert_ledger_balances(engine, agent)

    def test_partially_filled_order_can_be_cancelled_and_releases_remaining_block(self):
        _, clock, market, engine = make_engine_with(max_fill_fraction_per_tick=0.5)
        a, _ = engine.register_agent("partial")
        agent = a["agent_id"]
        o = buy_market(engine, agent, 10)
        assert o["status"] == "PARTIALLY_FILLED"
        c = engine.cancel_order(agent, o["order_id"])
        assert c["status"] == "CANCELLED" and c["filled_quantity"] == 5
        assert c["blocked_cash"] == 0 and blocked(engine, agent) == 0
        assert engine.positions(agent)[0]["quantity"] == 5
        assert_ledger_balances(engine, agent)


# =========================================================================================
# dry run, events, audit, listings
# =========================================================================================


class TestDryRunEventsAndListings:
    def test_dry_run_returns_estimates_without_creating_an_order(self, engine, agent):
        cursor = engine.bus.cursor
        q = quote(engine)
        r = buy_market(engine, agent, 10, dry_run=True)
        assert r["dry_run"] is True and r["would_be_accepted"] is True
        assert r["would_execute_now"] is True
        assert r["market_phase"] == "NORMAL"
        assert (r["symbol"], r["side"], r["quantity"], r["order_type"], r["product"]) == (
            SYMBOL,
            "BUY",
            10,
            "MARKET",
            "CNC",
        )
        ref = (D(q["ask"]) * (1 + SLIP)).quantize(Decimal("0.01"))
        assert D(r["reference_price"]) == ref
        assert D(r["order_value"]) == (D(q["ask"]) * (1 + SLIP) * 10).quantize(Decimal("0.01"))
        est = charges_for(Side.BUY, 10, D(q["ask"]) * (1 + SLIP))
        assert D(r["estimated_charges"]) == est
        assert D(r["cash_to_block"]) == D(r["order_value"]) + est
        assert D(r["free_cash_after_block"]) == INITIAL - D(r["cash_to_block"])
        assert D(r["ltp"]) == D(q["ltp"]) and D(r["ask"]) == D(q["ask"])
        assert "order_id" not in r

        assert engine.list_orders(agent) == []
        assert cash(engine, agent) == INITIAL and blocked(engine, agent) == 0
        assert engine.events_since(agent, cursor)["events"] == []
        assert engine.positions(agent) == []

    def test_dry_run_limit_would_not_execute_when_not_marketable(self, engine, agent):
        q = quote(engine)
        px = round_to_tick(D(q["ltp"]) - Decimal("10"), TICK)
        r = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=2, order_type="LIMIT", price=px, dry_run=True
        )
        assert r["would_execute_now"] is False
        assert D(r["reference_price"]) == px
        assert engine.list_orders(agent) == []

    def test_dry_run_still_validates(self, engine, agent):
        with pytest.raises(InvalidRequest):
            engine.place_order(agent, symbol=SYMBOL, side="BUY", quantity=0, dry_run=True)

    def test_events_since_carries_order_and_trade_payloads(self, engine, agent):
        cursor = engine.bus.cursor
        o = buy_market(engine, agent, 2)
        res = engine.events_since(agent, cursor)
        evs = res["events"]
        assert [e["type"] for e in evs] == ["ORDER_PLACED", "ORDER_FILLED"]
        placed, filled = evs
        assert placed["agent_id"] == agent and placed["order"]["order_id"] == o["order_id"]
        assert placed["order"]["status"] == "OPEN" and placed["order"]["filled_quantity"] == 0
        assert filled["order"]["status"] == "FILLED"
        assert filled["trade"]["order_id"] == o["order_id"] and filled["trade"]["quantity"] == 2
        assert filled["position"]["quantity"] == 2 and filled["position"]["symbol"] == SYMBOL
        assert res["cursor"] == filled["id"] > placed["id"] > cursor
        assert placed["ts"] == FROZEN_AT.isoformat()
        # the cursor is strictly increasing; nothing new after it
        assert engine.events_since(agent, res["cursor"])["events"] == []

    def test_events_are_filtered_per_agent(self, engine, agent):
        other, _ = engine.register_agent("other")
        cursor = engine.bus.cursor
        buy_market(engine, other["agent_id"], 1)
        assert engine.events_since(agent, cursor)["events"] == []
        assert [e["type"] for e in engine.events_since(other["agent_id"], cursor)["events"]] == [
            "ORDER_PLACED",
            "ORDER_FILLED",
        ]

    def test_audit_log_is_persisted(self, engine, agent):
        o = buy_market(engine, agent, 2, reasoning="because")
        q = quote(engine)
        px = round_to_tick(D(q["ltp"]) - Decimal("10"), TICK)
        r = engine.place_order(agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="LIMIT", price=px)
        engine.cancel_order(agent, r["order_id"])

        log = engine.audit_log(agent)  # newest first
        types = [e["type"] for e in log]
        assert types == [
            "ORDER_CANCELLED",
            "ORDER_PLACED",
            "ORDER_FILLED",
            "ORDER_PLACED",
            "AGENT_REGISTERED",
        ]
        assert all(e["agent_id"] == agent for e in log)
        filled = next(e for e in log if e["type"] == "ORDER_FILLED")
        assert filled["order"]["order_id"] == o["order_id"] and filled["order"]["reasoning"] == "because"
        assert filled["trade"]["quantity"] == 2
        assert log[0]["order"]["order_id"] == r["order_id"]
        assert [e["id"] for e in log] == sorted((e["id"] for e in log), reverse=True)
        assert engine.audit_log(agent, limit=2) == log[:2]

    def test_list_orders_status_filters(self, engine, agent, clock):
        q = quote(engine)
        px = round_to_tick(D(q["ltp"]) - Decimal("10"), TICK)
        filled = buy_market(engine, agent, 1)
        clock.advance(timedelta(seconds=1))
        resting = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="LIMIT", price=px
        )
        clock.advance(timedelta(seconds=1))
        cancelled = engine.place_order(
            agent, symbol=SYMBOL, side="BUY", quantity=1, order_type="LIMIT", price=px, validity="IOC"
        )

        ids = lambda rows: [o["order_id"] for o in rows]  # noqa: E731
        assert ids(engine.list_orders(agent)) == [
            cancelled["order_id"],
            resting["order_id"],
            filled["order_id"],
        ]  # newest first
        assert ids(engine.list_orders(agent, status="open")) == [resting["order_id"]]
        assert ids(engine.list_orders(agent, status="FILLED")) == [filled["order_id"]]
        assert ids(engine.list_orders(agent, status="cancelled")) == [cancelled["order_id"]]
        assert engine.list_orders(agent, status="EXPIRED") == []
        assert ids(engine.list_orders(agent, limit=1)) == [cancelled["order_id"]]
        other, _ = engine.register_agent("other")
        assert engine.list_orders(other["agent_id"]) == []

    def test_list_trades_newest_first(self, engine, agent, clock):
        t1 = buy_market(engine, agent, 1)
        clock.advance(timedelta(minutes=1))
        t2 = buy_market(engine, agent, 2)
        clock.advance(timedelta(minutes=1))
        t3 = engine.place_order(agent, symbol=SYMBOL, side="SELL", quantity=3, order_type="MARKET")
        trades = engine.list_trades(agent)
        assert [t["order_id"] for t in trades] == [t3["order_id"], t2["order_id"], t1["order_id"]]
        assert [t["executed_at"] for t in trades] == sorted((t["executed_at"] for t in trades), reverse=True)
        assert [t["side"] for t in trades] == ["SELL", "BUY", "BUY"]
        assert D(trades[0]["value"]) == D(trades[0]["price"]) * 3
        assert engine.list_trades(agent, limit=2) == trades[:2]
        assert_ledger_balances(engine, agent)

    @pytest.mark.xfail(
        reason="suspected product bug: serialize.ts() emits '+05:30' for freshly created rows but naive ISO strings for "
        "rows re-read from SQLite (DateTime(timezone=True) is a no-op there), so the same field changes shape between "
        "place_order and get_order/list_trades/ledger",
        strict=True,
    )
    def test_timestamps_keep_ist_offset_after_db_round_trip(self, engine, agent):
        o = buy_market(engine, agent, 1)
        assert o["executed_at"] == FROZEN_AT.isoformat()  # fresh row: tz-aware
        g = engine.get_order(agent, o["order_id"])
        assert g["executed_at"] == o["executed_at"]
        assert g["created_at"] == o["created_at"]
        assert engine.list_trades(agent)[0]["executed_at"] == FROZEN_AT.isoformat()
        assert engine.ledger(agent)[0]["ts"] == FROZEN_AT.isoformat()

    def test_portfolio_reflects_fill(self, engine, agent):
        o = buy_market(engine, agent, 10)
        p = engine.portfolio(agent)
        assert p["trade_count"] == 1 and p["open_orders"] == 0
        assert D(p["cash"]) == cash(engine, agent)
        assert D(p["total_charges"]) == D(o["charges"])
        px = ltp(engine, SYMBOL)
        assert D(p["holdings_value"]) == px * 10
        assert D(p["equity"]) == D(p["cash"]) + px * 10
        assert D(p["total_pnl"]) == D(p["equity"]) - INITIAL
