"""An LLM trading agent: Claude drives agent-trader through tool use.

Two ways to connect Claude to the platform:

* ``--mode sdk``  (default) — tools are thin ``@beta_tool`` wrappers around the Python SDK (REST).
* ``--mode mcp``            — Claude uses the platform's own MCP server (``agent-trader mcp`` over stdio)
                              through the Anthropic SDK's MCP tool-conversion helpers.

Requires ``pip install 'agent-trader[llm]'`` and Anthropic credentials (``ANTHROPIC_API_KEY`` or ``ant auth login``).
Server-side refusal fallbacks are enabled by default (``fallbacks="default"``) so a safety decline on the
primary model is retried on a fallback model inside the same call; drop the two lines if you don't want that.

    python examples/claude_agent.py --url http://localhost:8000 --objective "Build a 3-stock momentum book"
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os

import anthropic
from anthropic import beta_tool

from agent_trader.sdk import AgentTraderClient, AgentTraderError

MODEL = "claude-opus-5"

SYSTEM = """You are an autonomous trading agent on agent-trader, a paper-trading platform for the Indian stock market
(NSE/BSE, INR). No real money is involved, but the rules are real: session 09:15–15:30 IST, price bands, tick size ₹0.05,
Indian charges (STT, exchange fees, stamp duty, GST), MIS intraday positions squared off at 15:20 IST, no short selling in CNC.

How to work:
1. Check market_status first. If the market is closed you may still analyse and queue DAY orders (they execute at the next open).
2. Read get_portfolio to know cash, positions and risk limits. Never exceed the limits; a rejection is information, not an obstacle to retry.
3. Research with get_quotes / get_ohlc before trading. Prefer a few well-reasoned orders over many small ones; charges compound.
4. Always give a concise `reasoning` for every order and use a unique `client_order_id` (e.g. "buy-RELIANCE-1").
5. Protect positions with stop orders (SL-M) where sensible.
6. Finish with a short written summary: what you did, why, current equity and P&L, and what you would do next session.
Tool results are JSON; error results contain `error`, `message` and a `hint` — follow the hint."""


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def make_tools(client: AgentTraderClient):
    """Build the tool set for the Tool Runner from a connected SDK client."""

    def guard(fn):
        @functools.wraps(fn)  # keeps the signature so @beta_tool can derive the JSON schema
        def wrapped(*a, **kw):
            try:
                return _j(fn(*a, **kw))
            except AgentTraderError as e:
                return _j({"error": e.code, "message": e.message, "hint": e.hint, "details": e.details})

        return wrapped

    @beta_tool
    @guard
    def market_status() -> str:
        """Is the market open? Returns the session phase (PRE_OPEN/NORMAL/CLOSING/CLOSED/HOLIDAY), IST time and next open time."""
        return client.market_status()

    @beta_tool
    @guard
    def search_instruments(query: str = "", limit: int = 30) -> str:
        """Find tradeable NSE stocks by symbol, company name or sector (e.g. "bank", "pharma", "TCS"). Empty query lists all.

        Args:
            query: Substring matched against symbol, name and sector.
            limit: Maximum number of instruments to return.
        """
        return client.instruments(query, "NSE", limit)

    @beta_tool
    @guard
    def get_quotes(symbols: list[str]) -> str:
        """Live quotes (ltp, bid/ask, open/high/low, prev_close, change %, circuit limits, volume) for several NSE symbols.

        Args:
            symbols: Trading symbols, e.g. ["RELIANCE", "TCS"].
        """
        return client.quotes(symbols)

    @beta_tool
    @guard
    def get_ohlc(symbol: str, interval: str = "15m", limit: int = 50) -> str:
        """Historical candles for technical analysis, oldest first.

        Args:
            symbol: NSE trading symbol.
            interval: One of 1m, 3m, 5m, 15m, 30m, 1h, 1d.
            limit: Number of candles (max 1000).
        """
        return client.ohlc(symbol, interval, limit)

    @beta_tool
    @guard
    def get_portfolio() -> str:
        """Account snapshot: cash, blocked cash, equity, day/total P&L, open positions with live P&L, risk limits, halt status."""
        return client.portfolio()

    @beta_tool
    @guard
    def place_order(
        symbol: str,
        side: str,
        quantity: int,
        reasoning: str,
        client_order_id: str,
        order_type: str = "MARKET",
        product: str = "CNC",
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> str:
        """Place an order. Returns the order with its status (FILLED / OPEN / PARTIALLY_FILLED / CANCELLED), average price and charges.

        Args:
            symbol: NSE trading symbol, e.g. RELIANCE.
            side: BUY or SELL.
            quantity: Number of shares (integer > 0).
            reasoning: Why you are placing this order (kept in the audit trail).
            client_order_id: Unique id for idempotent retries, e.g. "buy-RELIANCE-1".
            order_type: MARKET, LIMIT (needs price), SL (needs price and trigger_price) or SL-M (needs trigger_price).
            product: CNC = delivery (no shorting, ₹0 brokerage). MIS = intraday, 5x leverage, shorting allowed, squared off 15:20 IST.
            price: Limit price, a multiple of ₹0.05 inside the day's circuit band.
            trigger_price: Stop trigger for SL / SL-M orders.
        """
        return client.place_order(
            symbol, side, quantity, order_type=order_type, product=product, price=price, trigger_price=trigger_price,
            client_order_id=client_order_id, reasoning=reasoning, tag="claude",
        )

    @beta_tool
    @guard
    def get_orders(status: str = "open") -> str:
        """List orders, newest first.

        Args:
            status: "open" for resting orders, "all", or FILLED / CANCELLED / REJECTED / EXPIRED.
        """
        return client.orders(None if status == "all" else status)

    @beta_tool
    @guard
    def cancel_order(order_id: str) -> str:
        """Cancel a resting order by order_id or client_order_id.

        Args:
            order_id: The order to cancel.
        """
        return client.cancel_order(order_id)

    @beta_tool
    @guard
    def get_performance() -> str:
        """Return %, win rate, profit factor, max drawdown and equity curve for this agent, plus the leaderboard rank of all agents."""
        return {"performance": client.performance(points=50), "leaderboard": client.leaderboard()}

    return [market_status, search_instruments, get_quotes, get_ohlc, get_portfolio, place_order, get_orders, cancel_order, get_performance]


def _print_message(message) -> None:
    for block in message.content:
        if block.type == "text" and block.text.strip():
            print(f"\n{block.text}")
        elif block.type == "tool_use":
            print(f"→ {block.name}({_j(block.input)})")
    if message.stop_reason == "refusal":
        print(f"[refusal] {message.stop_details}")


def run_sdk_mode(url: str, api_key: str | None, name: str, objective: str, model: str = MODEL) -> None:
    trader = AgentTraderClient(url, api_key=api_key) if api_key else AgentTraderClient.register(url, name=name, description=f"LLM agent ({model})")
    print(f"agent {trader.me()['agent_id']} connected to {url}")
    claude = anthropic.Anthropic()
    runner = claude.beta.messages.tool_runner(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        tools=make_tools(trader),
        messages=[{"role": "user", "content": objective}],
        max_iterations=40,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    for message in runner:
        _print_message(message)


async def run_mcp_mode(url: str, api_key: str | None, name: str, objective: str, model: str = MODEL) -> None:
    """Same agent, but Claude talks to the platform's MCP server (stdio) — the tools are defined by the server."""
    from anthropic import AsyncAnthropic
    from anthropic.lib.tools.mcp import async_mcp_tool
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    if api_key is None:
        api_key = AgentTraderClient.register(url, name=name, description=f"LLM agent via MCP ({model})").api_key
    env = {**os.environ, "TRADER_API_KEY": api_key}
    claude = AsyncAnthropic()
    async with stdio_client(StdioServerParameters(command="agent-trader", args=["mcp"], env=env)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            runner = claude.beta.messages.tool_runner(
                model=model,
                max_tokens=16000,
                system=SYSTEM,
                tools=[async_mcp_tool(t, session) for t in tools],
                messages=[{"role": "user", "content": objective}],
                max_iterations=40,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            async for message in runner:
                _print_message(message)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--api-key", default=os.environ.get("TRADER_API_KEY"))
    p.add_argument("--name", default="claude-trader")
    p.add_argument("--mode", choices=["sdk", "mcp"], default="sdk")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--objective", default="Review the market and my portfolio, then build a small diversified CNC book of 3 stocks "
                   "with clear reasoning, protect each with an SL-M order about 2% below entry, and summarise.")
    a = p.parse_args()
    if a.mode == "sdk":
        run_sdk_mode(a.url, a.api_key, a.name, a.objective, a.model)
    else:
        asyncio.run(run_mcp_mode(a.url, a.api_key, a.name, a.objective, a.model))
