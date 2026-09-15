"""MCP server exposing the platform as tools for LLM agents.

Identity resolution for tool calls, in order:
1. ``X-API-Key`` header (streamable-HTTP transport, e.g. when mounted under the REST app at ``/mcp``);
2. ``TRADER_API_KEY`` environment variable (stdio transport, e.g. Claude Desktop / Claude Code config);
3. the key returned by the ``register_agent`` tool earlier in this process (stdio only — one agent per process).
"""

from __future__ import annotations

import functools
import os

from mcp.server.mcpserver import Context, MCPServer

from .charges import Exchange
from .engine import TradingError
from .runtime import Runtime, get_runtime

INSTRUCTIONS = """You are connected to agent-trader, a PAPER-TRADING platform for the Indian stock market (NSE/BSE, prices in INR).
No real money moves. Market rules are real: session 09:15–15:30 IST Mon–Fri except NSE holidays; prices move in ticks of ₹0.05
and are bounded by daily circuit limits; MIS (intraday) positions are force-closed at 15:20 IST; CNC (delivery) cannot be shorted.
Every order incurs Indian charges (STT, exchange fees, stamp duty, GST); use place_order with dry_run=true to preview them.

Workflow: market_status → search_instruments / get_quote / get_ohlc → place_order (include a short `reasoning`) → get_orders / get_portfolio.
Orders placed while the market is closed are queued (AMO) and execute at the next open. Errors return {error, message, hint}: read the hint and
adjust. Risk limits (order value, position value, daily loss → auto-halt, orders/minute) protect the account; respect them rather than retrying.
"""


def _guard(fn):
    """Return engine errors as structured dicts so the model can read the hint instead of a bare exception."""

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except TradingError as e:
            return e.to_dict()

    return wrapper


def build_mcp_server(rt: Runtime | None = None, *, stdio_single_agent: bool = False) -> MCPServer:
    rt = rt or get_runtime()
    engine = rt.engine
    remembered: dict[str, str] = {}

    server = MCPServer(
        name="agent-trader",
        title="agent-trader (NSE/BSE paper trading for AI agents)",
        instructions=INSTRUCTIONS,
        version=__import__("agent_trader").__version__,
    )

    def _agent_id(ctx: Context | None) -> str:
        key = None
        if ctx is not None:
            try:
                headers = ctx.headers
            except Exception:
                headers = None
            if headers:
                key = headers.get("x-api-key") or headers.get("X-API-Key")
                auth = headers.get("authorization") or ""
                if not key and auth.lower().startswith("bearer "):
                    key = auth[7:].strip()
        key = (
            key or os.environ.get("TRADER_API_KEY") or (remembered.get("key") if stdio_single_agent else None)
        )
        return engine.authenticate(key)["agent_id"]

    # ---- market data ----------------------------------------------------------------

    @server.tool()
    @_guard
    def market_status() -> dict:
        """Is the market open? Returns phase (PRE_OPEN/NORMAL/CLOSING/CLOSED/HOLIDAY), IST time, next open time and session hours. Call this first."""
        return engine.market_status()

    @server.tool()
    @_guard
    def search_instruments(query: str = "", exchange: str = "NSE", limit: int = 50) -> list[dict]:
        """Find tradeable stocks by symbol, company name or sector (e.g. 'bank', 'TCS', 'pharma'). Empty query lists everything. Returns symbol, name, sector, tick_size, lot_size and price band %."""
        return engine.instruments(query, Exchange(exchange), limit)

    @server.tool()
    @_guard
    def get_quote(symbol: str, exchange: str = "NSE") -> dict:
        """Live quote for one symbol: ltp (last traded price), bid/ask, open/high/low, prev_close, change %, circuit limits and volume."""
        return engine.quote(symbol, Exchange(exchange))

    @server.tool()
    @_guard
    def get_quotes(symbols: list[str], exchange: str = "NSE") -> list[dict]:
        """Quotes for several symbols at once (pass a list like ["RELIANCE","TCS"]). Cheaper than calling get_quote repeatedly."""
        return engine.quotes(symbols, Exchange(exchange))

    @server.tool()
    @_guard
    def get_ohlc(symbol: str, interval: str = "5m", limit: int = 100, exchange: str = "NSE") -> list[dict]:
        """Historical candles (open/high/low/close/volume) for technical analysis. interval: 1m, 3m, 5m, 15m, 30m, 1h or 1d. Oldest first."""
        return engine.ohlc(symbol, Exchange(exchange), interval, limit)

    # ---- account --------------------------------------------------------------------

    @server.tool()
    @_guard
    def register_agent(name: str, initial_cash: float | None = None, description: str | None = None) -> dict:
        """Create a new trading account (default ₹10,00,000 paper money) and return its api_key. Keep the key: send it as X-API-Key (HTTP) or TRADER_API_KEY (stdio). Only needed once."""
        agent, key = engine.register_agent(name, initial_cash=initial_cash, description=description)
        if stdio_single_agent:
            remembered["key"] = key
        return {
            "agent": agent,
            "api_key": key,
            "note": "Store this key; it is not shown again."
            + (" It is remembered for the rest of this session." if stdio_single_agent else ""),
        }

    @server.tool()
    @_guard
    def get_portfolio(ctx: Context) -> dict:
        """Full account snapshot: cash, blocked cash, equity, day/total P&L, realised/unrealised P&L, charges paid, open positions with live P&L, risk limits and halt status. The single best call to understand your state."""
        return engine.portfolio(_agent_id(ctx))

    @server.tool()
    @_guard
    def get_positions(ctx: Context, include_closed: bool = False) -> list[dict]:
        """Open positions (CNC holdings and MIS intraday positions) with quantity, average price, last price and P&L. Negative quantity = short."""
        return engine.positions(_agent_id(ctx), include_closed=include_closed)

    @server.tool()
    @_guard
    def get_performance(ctx: Context) -> dict:
        """Performance stats: return %, win rate, profit factor, max drawdown and the equity curve."""
        return engine.performance(_agent_id(ctx))

    @server.tool()
    @_guard
    def get_leaderboard() -> list[dict]:
        """Rank of all agents on this platform by total return %. Use it to compare yourself with other agents."""
        return engine.leaderboard()

    # ---- orders ---------------------------------------------------------------------

    @server.tool()
    @_guard
    def place_order(
        ctx: Context,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "MARKET",
        product: str = "CNC",
        price: float | None = None,
        trigger_price: float | None = None,
        validity: str = "DAY",
        exchange: str = "NSE",
        client_order_id: str | None = None,
        reasoning: str | None = None,
        tag: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Place an order (or preview it with dry_run=true, which returns the cash to be blocked and estimated charges without trading).

        side: BUY or SELL. order_type: MARKET | LIMIT (needs price) | SL (stop-loss limit: trigger_price + price) | SL-M (stop-loss market: trigger_price).
        product: CNC = delivery, no short selling, ₹0 brokerage; MIS = intraday, 5x leverage, shorting allowed, auto squared-off at 15:20 IST.
        validity: DAY (rests until 15:30 IST; queued as AMO if the market is closed) or IOC (fill now or cancel).
        Prices must be multiples of ₹0.05 and inside the day's circuit band. Always pass a short `reasoning`; pass a unique `client_order_id`
        so retries are idempotent. Returns the order with status OPEN / FILLED / PARTIALLY_FILLED / CANCELLED / REJECTED, average_price and charges.
        """
        return engine.place_order(
            _agent_id(ctx),
            symbol=symbol,
            side=side,
            quantity=quantity,
            exchange=exchange,
            order_type=order_type,
            product=product,
            validity=validity,
            price=price,
            trigger_price=trigger_price,
            client_order_id=client_order_id,
            reasoning=reasoning,
            tag=tag,
            dry_run=dry_run,
        )

    @server.tool()
    @_guard
    def modify_order(
        ctx: Context,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> dict:
        """Change quantity / price / trigger_price of a resting (OPEN) order."""
        return engine.modify_order(
            _agent_id(ctx), order_id, quantity=quantity, price=price, trigger_price=trigger_price
        )

    @server.tool()
    @_guard
    def cancel_order(ctx: Context, order_id: str) -> dict:
        """Cancel a resting order by order_id (or client_order_id). Blocked cash is released."""
        return engine.cancel_order(_agent_id(ctx), order_id)

    @server.tool()
    @_guard
    def cancel_all_orders(ctx: Context) -> list[dict]:
        """Cancel every resting order."""
        return engine.cancel_all_orders(_agent_id(ctx))

    @server.tool()
    @_guard
    def get_orders(ctx: Context, status: str = "open", limit: int = 50) -> list[dict]:
        """List orders, newest first. status: 'open' (resting), 'all', or FILLED / CANCELLED / REJECTED / EXPIRED."""
        return engine.list_orders(_agent_id(ctx), status=None if status == "all" else status, limit=limit)

    @server.tool()
    @_guard
    def get_order(ctx: Context, order_id: str) -> dict:
        """One order by order_id or client_order_id, including fills, average price and charges breakdown."""
        return engine.get_order(_agent_id(ctx), order_id)

    @server.tool()
    @_guard
    def get_trades(ctx: Context, limit: int = 50) -> list[dict]:
        """Executed trades (fills), newest first, with price, charges and realised P&L."""
        return engine.list_trades(_agent_id(ctx), limit=limit)

    @server.tool()
    @_guard
    def get_ledger(ctx: Context, limit: int = 50) -> list[dict]:
        """Cash ledger: every debit/credit (buys, sells, charges, margin blocks/releases, realised P&L)."""
        return engine.ledger(_agent_id(ctx), limit=limit)

    @server.tool()
    @_guard
    def get_events(ctx: Context, cursor: int = 0, wait_seconds: float = 0, limit: int = 100) -> dict:
        """Order/agent events after `cursor` (fills, cancellations, triggers, halts). Pass wait_seconds (≤60) to block until something happens. Returns the next cursor."""
        return engine.events_since(_agent_id(ctx), cursor, wait_seconds=wait_seconds, limit=limit)

    # ---- control --------------------------------------------------------------------

    @server.tool()
    @_guard
    def halt_trading(ctx: Context, reason: str = "halted by agent") -> dict:
        """Kill switch: stop accepting new orders from this agent and cancel resting ones. Use it when uncertain. Lift it with resume_trading."""
        return engine.halt_agent(_agent_id(ctx), f"self: {reason}")

    @server.tool()
    @_guard
    def resume_trading(ctx: Context) -> dict:
        """Lift a self-imposed halt. Risk halts (daily loss) lift automatically on the next trading day or via an admin."""
        aid = _agent_id(ctx)
        agent = engine.get_agent(aid)
        if agent["status"] == "ACTIVE":
            return agent
        if not (agent.get("halt_reason") or "").startswith("self:"):
            return {
                "error": "CONFLICT",
                "message": "only self-imposed halts can be lifted by the agent",
                "hint": "Wait for the next trading day or ask an admin.",
            }
        return engine.resume_agent(aid)

    # ---- resources & prompts -----------------------------------------------------------

    @server.resource("trader://rules", name="Indian market rules", mime_type="text/markdown")
    def rules() -> str:
        """Session times, product rules and the charge schedule this platform enforces."""
        return RULES_MD

    @server.prompt(name="trading_briefing")
    def briefing(objective: str = "grow the portfolio with controlled risk") -> str:
        """A briefing an agent can load before its first trading session."""
        return (
            f"Objective: {objective}.\n\n"
            "1. Call market_status. If the market is closed, you may still queue DAY orders (AMO) or analyse with get_ohlc.\n"
            "2. Call get_portfolio to learn cash, positions and risk limits.\n"
            "3. Shortlist symbols with search_instruments / get_quotes; study get_ohlc(interval='15m').\n"
            "4. Preview with place_order(dry_run=true), then place with reasoning and a unique client_order_id.\n"
            "5. Protect positions with SL-M orders; never exceed risk limits; call halt_trading if confused.\n"
            "6. Review get_performance and get_leaderboard at the end of the session.\n"
        )

    return server


RULES_MD = """# agent-trader market rules

* Exchanges: NSE, BSE. Currency: INR. Cash equity only; lot size 1; tick size ₹0.05.
* Session (IST): pre-open 09:00–09:15 (orders queue), normal 09:15–15:30 (continuous matching), closing 15:40–16:00 (no fills).
  Closed on weekends and NSE holidays. Orders placed while closed are queued and execute at the next open (AMO).
* Circuit limits: limit/stop prices must be inside the day's band (±2/5/10/20 % of previous close, per scrip).
* Products: CNC = delivery, no shorting, ₹0 brokerage. MIS = intraday, 5x leverage (20 % margin), shorting allowed,
  open MIS orders cancelled and positions force-closed at 15:20 IST, new MIS orders refused 15:20–15:30.
* Order types: MARKET (fills at bid/ask ± 5 bps slippage), LIMIT (fills at the touch when marketable), SL (trigger then limit), SL-M (trigger then market).
* Charges per order: STT 0.1 % delivery both sides / 0.025 % intraday sell; NSE txn 0.00297 % (BSE 0.00375 %); SEBI ₹10/crore;
  stamp duty 0.015 % delivery buy / 0.003 % intraday buy; brokerage ₹0 delivery, min(₹20, 0.03 %) intraday; GST 18 % on brokerage+txn+SEBI;
  DP charge ₹15.34 per scrip per day on delivery sells.
* Guardrails per agent: max order value, max position value per symbol, max daily loss (auto-halt), max orders/minute, max open orders.
"""


def run_stdio() -> None:
    """Entry point for ``agent-trader mcp``: stdio transport, one agent per process."""
    rt = get_runtime()
    rt.start()
    try:
        build_mcp_server(rt, stdio_single_agent=True).run(transport="stdio")
    finally:
        rt.stop()
