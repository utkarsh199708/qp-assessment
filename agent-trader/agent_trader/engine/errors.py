"""Errors are designed to be read by an LLM: a stable ``code``, a plain ``message`` and a
``hint`` that says what to do differently. ``details`` carries the numbers."""

from __future__ import annotations

from typing import Any


class TradingError(Exception):
    code = "TRADING_ERROR"
    http_status = 400

    def __init__(self, message: str, *, hint: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        if self.details:
            d["details"] = self.details
        return d


class InvalidRequest(TradingError):
    code = "INVALID_REQUEST"
    http_status = 400


class Unauthorized(TradingError):
    code = "UNAUTHORIZED"
    http_status = 401


class NotFound(TradingError):
    code = "NOT_FOUND"
    http_status = 404


class Conflict(TradingError):
    code = "CONFLICT"
    http_status = 409


class OrderRejected(TradingError):
    code = "ORDER_REJECTED"
    http_status = 422


class AgentHalted(TradingError):
    code = "AGENT_HALTED"
    http_status = 423


class RateLimited(TradingError):
    code = "RATE_LIMITED"
    http_status = 429
