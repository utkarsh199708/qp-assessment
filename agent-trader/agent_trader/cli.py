"""Command-line entry points: ``agent-trader serve | mcp | register | demo``."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="agent-trader", description="Paper-trading platform for AI agents on NSE/BSE."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the REST + MCP (streamable-http at /mcp) server")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--reload", action="store_true")

    sub.add_parser(
        "mcp", help="run the MCP server over stdio (for Claude Desktop / Claude Code / any MCP client)"
    )

    r = sub.add_parser("register", help="create an agent against a running server and print its API key")
    r.add_argument("name")
    r.add_argument("--url", default="http://localhost:8000")
    r.add_argument("--cash", type=float, default=None)

    d = sub.add_parser(
        "demo", help="run the bundled example agent in-process against a frozen simulated market"
    )
    d.add_argument("--steps", type=int, default=120)

    args = p.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        from .config import get_settings

        st = get_settings()
        uvicorn.run(
            "agent_trader.api.app:create_app",
            factory=True,
            host=args.host or st.host,
            port=args.port or st.port,
            reload=args.reload,
            log_level=st.log_level.lower(),
        )
        return 0

    if args.cmd == "mcp":
        from .mcp_server import run_stdio

        run_stdio()
        return 0

    if args.cmd == "register":
        from .sdk import AgentTraderClient

        c = AgentTraderClient.register(args.url, name=args.name, initial_cash=args.cash)
        print(json.dumps({"agent": c.agent, "api_key": c.api_key}, indent=2))
        return 0

    if args.cmd == "demo":
        from examples.momentum_agent import run_demo  # type: ignore

        run_demo(steps=args.steps)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
