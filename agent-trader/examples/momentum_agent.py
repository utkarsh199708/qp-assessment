"""A rule-based momentum agent using the Python SDK — no LLM involved.

Strategy (deliberately simple): every step rank the universe by 20-candle (5-minute) return,
hold the top N names as CNC positions with equal weights, exit anything that drops out of the
top N or falls 2 % below its average price. Every order carries a `reasoning` string and an
idempotent `client_order_id`.

Run against a live server:   python examples/momentum_agent.py --url http://localhost:8000
Run the self-contained demo:  agent-trader demo   (frozen clock, deterministic market)
"""

from __future__ import annotations

import argparse
import time

from agent_trader.sdk import AgentTraderClient, AgentTraderError

TOP_N = 3
LOOKBACK = 20
STOP_PCT = 2.0
WEIGHT = 0.10  # 10 % of equity per position
UNIVERSE = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC", "LT", "TATASTEEL",
            "JSWSTEEL", "HINDALCO", "BEL", "TRENT", "MARUTI", "SUNPHARMA", "TITAN", "ADANIENT", "ONGC", "NTPC"]


def momentum_scores(client: AgentTraderClient) -> dict[str, float]:
    scores = {}
    for sym in UNIVERSE:
        candles = client.ohlc(sym, interval="5m", limit=LOOKBACK + 1)
        if len(candles) < LOOKBACK + 1:
            continue
        scores[sym] = candles[-1]["close"] / candles[0]["close"] - 1
    return scores


def step(client: AgentTraderClient, n: int) -> None:
    status = client.market_status()
    pf = client.portfolio()
    held = {p["symbol"]: p for p in pf["positions"] if p["product"] == "CNC" and p["quantity"] > 0}
    scores = momentum_scores(client)
    ranked = sorted(scores, key=scores.get, reverse=True)
    targets = ranked[:TOP_N]

    # exits
    for sym, pos in held.items():
        drawdown = (pos["last_price"] / pos["average_price"] - 1) * 100
        if sym not in targets or drawdown <= -STOP_PCT:
            why = f"stop-loss {drawdown:.2f}%" if drawdown <= -STOP_PCT else f"dropped out of top-{TOP_N} momentum"
            try:
                o = client.sell(sym, pos["quantity"], reasoning=why, client_order_id=f"exit-{sym}-{n}", tag="momentum")
                print(f"[{n}] SELL {sym} x{pos['quantity']} -> {o['status']} @ {o['average_price']} ({why})")
            except AgentTraderError as e:
                print(f"[{n}] SELL {sym} refused: {e}")

    # entries
    budget = pf["equity"] * WEIGHT
    for sym in targets:
        if sym in held or scores[sym] <= 0:
            continue
        ltp = client.quote(sym)["ltp"]
        qty = int(budget // ltp)
        if qty < 1:
            continue
        why = f"rank {targets.index(sym) + 1} momentum {scores[sym] * 100:.2f}% over {LOOKBACK}x5m"
        try:
            o = client.buy(sym, qty, reasoning=why, client_order_id=f"entry-{sym}-{n}", tag="momentum")
            print(f"[{n}] BUY  {sym} x{qty} -> {o['status']} @ {o['average_price']} charges ₹{o['charges']} ({why})")
        except AgentTraderError as e:
            print(f"[{n}] BUY {sym} refused: {e}")

    print(f"[{n}] {status['phase']} equity ₹{pf['equity']:,.2f} day P&L ₹{pf['day_pnl']:,.2f} positions {len(held)}")


def run(client: AgentTraderClient, steps: int, sleep: float = 60.0, advance: bool = False) -> None:
    for n in range(steps):
        step(client, n)
        if advance:  # frozen-clock demo: move time forward one minute and tick the engine
            client.admin_clock(advance_seconds=60, ticks=1)
        else:
            time.sleep(sleep)
    perf = client.performance()
    print("\n=== performance ===")
    for k in ("equity", "total_pnl", "total_return_pct", "realised_pnl", "unrealised_pnl", "total_charges", "trade_count", "win_rate_pct", "max_drawdown_pct"):
        print(f"{k:>18}: {perf[k]}")
    print("leaderboard:", [(r["rank"], r["name"], r["total_return_pct"]) for r in client.leaderboard()])


def run_demo(steps: int = 120) -> None:
    from agent_trader.testing import in_process_platform

    rt, server = in_process_platform()
    try:
        client = AgentTraderClient.register(server.base_url, name="momentum-demo", description="rule-based momentum example", admin_key="admin")
        print(f"registered {client.agent['agent_id']} with ₹{client.agent['cash']:,.0f} at {server.base_url}")
        run(client, steps, advance=True)
    finally:
        server.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=None, help="server URL; omit to run the self-contained demo")
    p.add_argument("--api-key", default=None)
    p.add_argument("--name", default="momentum-bot")
    p.add_argument("--steps", type=int, default=60)
    p.add_argument("--sleep", type=float, default=60.0)
    a = p.parse_args()
    if a.url is None:
        run_demo(a.steps)
    else:
        c = AgentTraderClient(a.url, api_key=a.api_key) if a.api_key else AgentTraderClient.register(a.url, name=a.name)
        run(c, a.steps, sleep=a.sleep)
