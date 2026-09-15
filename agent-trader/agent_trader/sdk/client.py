"""Python SDK for algorithmic agents. Mirrors the REST API / MCP tools one-to-one.

from agent_trader.sdk import AgentTraderClient
client = AgentTraderClient.register("http://localhost:8000", name="momentum-bot")
client.place_order("RELIANCE", "BUY", 10, reasoning="breakout above 20-day high")
print(client.portfolio()["equity"])
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx


class AgentTraderError(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        self.status = status
        self.code = body.get("error", "ERROR")
        self.message = body.get("message", "")
        self.hint = body.get("hint")
        self.details = body.get("details") or {}
        super().__init__(
            f"[{status} {self.code}] {self.message}" + (f" — hint: {self.hint}" if self.hint else "")
        )


class AgentTraderClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        *,
        admin_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.admin_key = admin_key
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    # ---- plumbing -------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        if self.admin_key:
            h["X-Admin-Key"] = self.admin_key
        return h

    def _req(
        self, method: str, path: str, *, json_body: Any = None, params: dict[str, Any] | None = None
    ) -> Any:
        r = self._http.request(
            method,
            path,
            json=json_body,
            params={k: v for k, v in (params or {}).items() if v is not None},
            headers=self._headers(),
        )
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {"error": "HTTP_ERROR", "message": r.text}
            raise AgentTraderError(r.status_code, body)
        return r.json() if r.content else None

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ---- account --------------------------------------------------------------------

    @classmethod
    def register(
        cls,
        base_url: str,
        name: str,
        *,
        initial_cash: float | None = None,
        description: str | None = None,
        metadata: dict | None = None,
        risk_limits: dict | None = None,
        **kw,
    ) -> AgentTraderClient:
        """Create an account and return a client already authenticated with its new key (``client.api_key``)."""
        c = cls(base_url, **kw)
        res = c._req(
            "POST",
            "/v1/agents/register",
            json_body={
                "name": name,
                "initial_cash": initial_cash,
                "description": description,
                "metadata": metadata,
                "risk_limits": risk_limits,
            },
        )
        c.api_key = res["api_key"]
        c.agent = res["agent"]
        return c

    def me(self) -> dict:
        return self._req("GET", "/v1/agents/me")

    def portfolio(self) -> dict:
        return self._req("GET", "/v1/agents/me/portfolio")

    def positions(self, include_closed: bool = False) -> list[dict]:
        return self._req("GET", "/v1/agents/me/positions", params={"include_closed": include_closed})

    def trades(self, limit: int = 100) -> list[dict]:
        return self._req("GET", "/v1/agents/me/trades", params={"limit": limit})

    def ledger(self, limit: int = 100) -> list[dict]:
        return self._req("GET", "/v1/agents/me/ledger", params={"limit": limit})

    def performance(self, points: int = 200) -> dict:
        return self._req("GET", "/v1/agents/me/performance", params={"points": points})

    def audit(self, limit: int = 200) -> list[dict]:
        return self._req("GET", "/v1/agents/me/audit", params={"limit": limit})

    def leaderboard(self) -> list[dict]:
        return self._req("GET", "/v1/agents/leaderboard")

    def halt(self, reason: str = "halted by agent") -> dict:
        return self._req("POST", "/v1/agents/me/halt", json_body={"reason": reason})

    def resume(self) -> dict:
        return self._req("POST", "/v1/agents/me/resume")

    def tighten_risk_limits(self, **limits: Any) -> dict:
        return self._req("PATCH", "/v1/agents/me/risk-limits", json_body=limits)

    # ---- market ---------------------------------------------------------------------

    def market_status(self) -> dict:
        return self._req("GET", "/v1/market/status")

    def instruments(
        self, query: str | None = None, exchange: str | None = None, limit: int = 200
    ) -> list[dict]:
        return self._req(
            "GET", "/v1/market/instruments", params={"q": query, "exchange": exchange, "limit": limit}
        )

    def quote(self, symbol: str, exchange: str = "NSE") -> dict:
        return self._req("GET", f"/v1/market/quote/{symbol}", params={"exchange": exchange})

    def quotes(self, symbols: list[str] | None = None, exchange: str = "NSE") -> list[dict]:
        return self._req(
            "GET",
            "/v1/market/quotes",
            params={"symbols": ",".join(symbols) if symbols else None, "exchange": exchange},
        )

    def ohlc(self, symbol: str, interval: str = "5m", limit: int = 100, exchange: str = "NSE") -> list[dict]:
        return self._req(
            "GET",
            f"/v1/market/ohlc/{symbol}",
            params={"interval": interval, "limit": limit, "exchange": exchange},
        )

    # ---- orders ---------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        *,
        order_type: str = "MARKET",
        product: str = "CNC",
        price: float | None = None,
        trigger_price: float | None = None,
        validity: str = "DAY",
        exchange: str = "NSE",
        client_order_id: str | None = None,
        reasoning: str | None = None,
        tag: str | None = None,
    ) -> dict:
        return self._req(
            "POST",
            "/v1/orders",
            json_body={
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "order_type": order_type,
                "product": product,
                "price": price,
                "trigger_price": trigger_price,
                "validity": validity,
                "exchange": exchange,
                "client_order_id": client_order_id,
                "reasoning": reasoning,
                "tag": tag,
            },
        )

    def buy(self, symbol: str, quantity: int, **kw) -> dict:
        return self.place_order(symbol, "BUY", quantity, **kw)

    def sell(self, symbol: str, quantity: int, **kw) -> dict:
        return self.place_order(symbol, "SELL", quantity, **kw)

    def orders(self, status: str | None = "open", limit: int = 100) -> list[dict]:
        return self._req("GET", "/v1/orders", params={"status": status, "limit": limit})

    def order(self, order_id: str) -> dict:
        return self._req("GET", f"/v1/orders/{order_id}")

    def modify_order(
        self,
        order_id: str,
        *,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> dict:
        return self._req(
            "PATCH",
            f"/v1/orders/{order_id}",
            json_body={"quantity": quantity, "price": price, "trigger_price": trigger_price},
        )

    def cancel_order(self, order_id: str) -> dict:
        return self._req("DELETE", f"/v1/orders/{order_id}")

    def cancel_all(self) -> list[dict]:
        return self._req("DELETE", "/v1/orders")

    def wait_for_fill(self, order_id: str, timeout: float = 30.0, poll: float = 0.5) -> dict:
        """Poll until the order reaches a terminal state (FILLED/CANCELLED/REJECTED/EXPIRED) or ``timeout``."""
        deadline = time.monotonic() + timeout
        while True:
            o = self.order(order_id)
            if o["status"] in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
                return o
            if time.monotonic() >= deadline:
                return o
            time.sleep(poll)

    # ---- events ---------------------------------------------------------------------

    def events(self, cursor: int = 0, wait: float = 0, limit: int = 200) -> dict:
        return self._req("GET", "/v1/events", params={"cursor": cursor, "wait": wait, "limit": limit})

    def stream_events(self, cursor: int = 0) -> Iterator[dict]:
        """Yield events from the SSE endpoint forever (reconnects are the caller's job)."""
        with self._http.stream(
            "GET", "/v1/stream/events", params={"cursor": cursor}, headers=self._headers(), timeout=None
        ) as r:
            yield from _iter_sse(r)

    def stream_quotes(
        self, symbols: list[str], interval: float = 1.0, exchange: str = "NSE"
    ) -> Iterator[list[dict]]:
        with self._http.stream(
            "GET",
            "/v1/stream/quotes",
            params={"symbols": ",".join(symbols), "interval": interval, "exchange": exchange},
            headers=self._headers(),
            timeout=None,
        ) as r:
            yield from _iter_sse(r)

    # ---- admin ----------------------------------------------------------------------

    def admin_agents(self) -> list[dict]:
        return self._req("GET", "/v1/admin/agents")

    def admin_halt(self, agent_id: str, reason: str) -> dict:
        return self._req("POST", f"/v1/admin/agents/{agent_id}/halt", json_body={"reason": reason})

    def admin_resume(self, agent_id: str) -> dict:
        return self._req("POST", f"/v1/admin/agents/{agent_id}/resume")

    def admin_set_risk_limits(self, agent_id: str, **limits: Any) -> dict:
        return self._req("PATCH", f"/v1/admin/agents/{agent_id}/risk-limits", json_body=limits)

    def admin_deposit(self, agent_id: str, amount: float, note: str | None = None) -> dict:
        return self._req(
            "POST", f"/v1/admin/agents/{agent_id}/deposit", json_body={"amount": amount, "note": note}
        )

    def admin_set_price(self, symbol: str, price: float, exchange: str = "NSE") -> dict:
        return self._req(
            "POST",
            "/v1/admin/market/set-price",
            json_body={"symbol": symbol, "price": price, "exchange": exchange},
        )

    def admin_clock(
        self, *, set: str | None = None, advance_seconds: float | None = None, ticks: int = 0
    ) -> dict:
        return self._req(
            "POST",
            "/v1/admin/clock",
            json_body={"set": set, "advance_seconds": advance_seconds, "ticks": ticks},
        )

    def admin_tick(self, n: int = 1) -> dict:
        return self._req("POST", "/v1/admin/tick", params={"n": n})


def _iter_sse(response: httpx.Response) -> Iterator[Any]:
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data_lines:
                yield json.loads("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
