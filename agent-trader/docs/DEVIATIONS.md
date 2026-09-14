# v1 vs the design panel

`docs/DESIGN-PANEL.md` is the synthesized output of three independent designs (agent ergonomics,
market realism, safety/ops). v1 implements the load-bearing parts and deliberately trims the rest.
This file records what differs and why, so the gaps are explicit rather than accidental.

| Panel decision | v1 | Rationale |
|---|---|---|
| D1 single tool registry generating MCP/REST/SDK | Hand-written but 1:1 (engine methods → routes → MCP tools → SDK methods); tests assert parity | A code generator is overhead at 21 tools; parity is enforced by tests instead |
| D2 money as integer paise in DB | Exact `Decimal` stored as text (`db.Money`) | Same exactness, no unit conversion bugs; SQLite has no DECIMAL either way |
| D3 `NSE:INFY` identity | `symbol` + `exchange` fields, and `NSE:INFY` accepted in `symbol` | Both forms work; separate fields keep OpenAPI/MCP schemas simple |
| D4 rejected orders stored | Rejections raise structured errors **and** are written to the audit log as `ORDER_REJECTED` events; `client_order_id` is only consumed by accepted orders | Same audit value without creating order rows for typos |
| D5 market order on locked circuit → rest as LIMIT at band | Market orders fill at the band price (never beyond) | Simpler; the simulator rarely pins at the band |
| D6 partial MARKET remainder → LIMIT | Partial fills only via `max_fill_fraction_per_tick`; remainder stays as the same order | Adequate for a simulator without depth |
| D7 MIS leftovers → convert to CNC | MIS positions are always force-closed at 15:20 with a system MARKET order | Deterministic, and the fill always succeeds in the simulator |
| D8 80 % sale proceeds T+0 | 100 % usable immediately | Config-level realism deferred |
| D10 double-entry journals | Single-entry cash ledger with a tested invariant `sum(amount) == cash` and `blocked_cash == Σ order blocks + Σ MIS margin` | Same guarantees for one cash account |
| D11 arenas | One server = one world; `leaderboard` ranks all agents | As the panel recommended for v1 |
| D14 31 tools + core subset | 22 tools (incl. `get_rules`), all exposed | Fewer, denser tools are easier for LLMs to use correctly |
| D15 flat rejection enum with layer | Flat codes (`ORDER_REJECTED`, `INVALID_REQUEST`, …) + message + hint + details | Layer is implied by the message; kept the payload small |
| D16 daily loss = min(5 %, ₹50k), halt-and-flatten | ₹50k absolute (configurable per agent); halt cancels open orders and flattens MIS positions, CNC holdings are kept | CNC is delivery; liquidating it on a halt is not what a broker does |
| D17 `reason` required | `reasoning` optional but requested in every tool description | Do not block non-LLM bots; audit still captures it when given |
| D18 stdio = proxy to REST | stdio runs the engine in-process (one agent per process); HTTP MCP is mounted on the REST server | Zero-config local use; multi-agent deployments use HTTP MCP |
| alembic, structlog, prometheus, typer | Not included | Out of scope for v1; `create_all` on startup, stdlib logging, argparse |
