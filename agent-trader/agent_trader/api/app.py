"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..engine import TradingError
from ..runtime import Runtime, get_runtime
from .routes import ALL_ROUTERS

DESCRIPTION = """
Paper-trading platform for **AI agents** on the Indian stock market (NSE/BSE, INR).

* Register → get an API key → send it as `X-API-Key`.
* Trade CNC (delivery) or MIS (intraday, leveraged, shorting) with MARKET / LIMIT / SL / SL-M orders.
* Real market rules: IST session 09:15–15:30, holidays, price bands, tick size, STT/GST/stamp duty.
* Guardrails per agent: order value, position value, daily loss (auto-halt), rate limit.
* Every order can carry `reasoning`; everything is in the audit log.
* Same capabilities via MCP (`/mcp`) and the Python SDK.
"""


def create_app(runtime: Runtime | None = None, *, start_loop: bool = True, mount_mcp: bool = True) -> FastAPI:
    rt = runtime or get_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_loop:
            rt.start()
        if mount_mcp:
            # streamable-http MCP session manager must be run inside the ASGI lifespan
            async with app.state.mcp_app.router.lifespan_context(app.state.mcp_app):
                yield
        else:
            yield
        rt.stop()

    app = FastAPI(title="agent-trader", version=__version__, description=DESCRIPTION, lifespan=lifespan)
    app.state.runtime = rt
    app.add_middleware(CORSMiddleware, allow_origins=rt.settings.cors_origins, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(TradingError)
    async def _trading_error(_: Request, exc: TradingError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        errs = [f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors()]
        return JSONResponse(
            status_code=400,
            content={"error": "INVALID_REQUEST", "message": "; ".join(errs), "hint": "See /openapi.json for the exact schema."},
        )

    for r in ALL_ROUTERS:
        app.include_router(r)

    if mount_mcp:
        from ..mcp_server import build_mcp_server

        mcp = build_mcp_server(rt)
        mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0")
        app.state.mcp_app = mcp_app
        app.mount("/", mcp_app)  # serves /mcp; mounted last so REST routes win

    return app
