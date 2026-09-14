"""Helpers for running the platform in-process (demos, tests, notebooks)."""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime

import httpx
import uvicorn

from .api import create_app
from .clock import IST
from .config import Settings
from .runtime import Runtime, build_runtime


def frozen_settings(**overrides) -> Settings:
    """Settings for a deterministic in-memory platform frozen at Wed 2026-09-16 09:16 IST."""
    base = dict(
        database_url="sqlite:///:memory:", clock_mode="frozen", frozen_at=datetime(2026, 9, 16, 9, 16, tzinfo=IST),
        sim_seed=42, sim_warmup_candles=120, admin_api_key="admin", _env_file=None,
    )
    base.update(overrides)
    return Settings(**base)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    """Serve a FastAPI app with uvicorn on a background thread. ``base_url`` is ready after start()."""

    def __init__(self, app, host: str = "127.0.0.1", port: int | None = None):
        self.port = port or free_port()
        self.host = host
        self.base_url = f"http://{host}:{self.port}"
        cfg = uvicorn.Config(app, host=host, port=self.port, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="agent-trader-uvicorn")

    def start(self, timeout: float = 10.0) -> "ServerThread":
        self.thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.base_url}/health", timeout=1.0).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


def in_process_platform(settings: Settings | None = None, *, start_loop: bool | None = None) -> tuple[Runtime, ServerThread]:
    """Build a runtime + app and serve it on a local port. Returns (runtime, server) — call server.stop() when done."""
    settings = settings or frozen_settings()
    rt = build_runtime(settings)
    if start_loop is None:
        start_loop = settings.clock_mode != "frozen"
    app = create_app(rt, start_loop=start_loop, mount_mcp=True)
    return rt, ServerThread(app).start()
