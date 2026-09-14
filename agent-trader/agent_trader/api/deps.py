"""FastAPI dependencies: runtime access and API-key auth."""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import Depends, Header, Request

from ..engine import TradingEngine, Unauthorized
from ..runtime import Runtime


def get_rt(request: Request) -> Runtime:
    return request.app.state.runtime


def get_engine(rt: Annotated[Runtime, Depends(get_rt)]) -> TradingEngine:
    return rt.engine


def current_agent(
    engine: Annotated[TradingEngine, Depends(get_engine)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    key = x_api_key
    if not key and authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    return engine.authenticate(key)


def require_admin(
    rt: Annotated[Runtime, Depends(get_rt)],
    x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
) -> None:
    expected = rt.settings.admin_api_key
    if expected is None:
        return  # dev mode: admin endpoints are open
    if not x_admin_key or not hmac.compare_digest(x_admin_key, expected):
        raise Unauthorized("admin key required", hint="Send X-Admin-Key (TRADER_ADMIN_API_KEY).")


Agent = Annotated[dict[str, Any], Depends(current_agent)]
Engine = Annotated[TradingEngine, Depends(get_engine)]
RT = Annotated[Runtime, Depends(get_rt)]
