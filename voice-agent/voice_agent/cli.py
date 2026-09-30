"""``voice-agent`` command line: estimate, simulate, compare, pricebook, serve."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .costs import PriceBook
from .estimate import Assumptions, estimate_call
from .profiles import PROFILES, get_profile


def _fmt_table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(header))
    out = [line, "  ".join("-" * w for w in widths)]
    for r in rows:
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))
    return "\n".join(out)


def cmd_estimate(args: argparse.Namespace) -> int:
    book = PriceBook.load(args.pricebook)
    a = Assumptions(
        call_minutes=args.minutes,
        vad_gating=not args.no_vad_gating,
        prompt_caching=not args.no_prompt_cache,
        tts_cache_hit_rate=args.tts_cache_hit_rate,
        fastpath_rate=args.fastpath_rate,
        system_prompt_tokens=args.system_tokens,
        capacity_utilization=args.utilization,
    )
    names = list(PROFILES) if args.profile == "all" else [args.profile]
    rows = []
    notes: list[str] = []
    for name in names:
        est = estimate_call(book, get_profile(name), a)
        b = est.meter.breakdown()
        rows.append(
            [
                name,
                f"${est.usd_per_minute:.4f}",
                f"${est.usd_per_call:.4f}",
                f"${est.usd_per_minute * args.monthly_minutes:,.0f}",
            ]
            + [
                f"${b.get(c, 0.0):.4f}"
                for c in ("telephony", "stt", "llm", "tts", "s2s", "platform", "compute")
            ]
        )
        notes.extend(est.notes)
    print(f"price book as of {book.as_of}; {args.minutes:g}-minute call, {args.monthly_minutes:,} min/month")
    print(
        _fmt_table(
            rows,
            [
                "profile",
                "$/min",
                "$/call",
                "$/month",
                "telephony",
                "stt",
                "llm",
                "tts",
                "s2s",
                "platform",
                "compute",
            ],
        )
    )
    for n in dict.fromkeys(notes):
        print(f"note: {n}")
    if args.json:
        print(
            json.dumps(
                {name: estimate_call(book, get_profile(name), a).meter.to_dict() for name in names}, indent=2
            )
        )
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from .simulate import Levers, compare_levers, run_call

    book = PriceBook.load(args.pricebook)
    if args.compare:
        results = asyncio.run(compare_levers(book, args.profile))
        rows = []
        for label, m in results.items():
            c = m.counters
            b = m.breakdown()
            rows.append(
                [
                    label,
                    f"{m.duration_s:.0f}s",
                    f"${m.total():.4f}",
                    f"${m.usd_per_minute():.4f}",
                    f"{c.get('stt_seconds_billed', 0):.0f}s",
                    f"{c.get('llm_calls', 0):g}",
                    f"{c.get('llm_input_tokens', 0):g}",
                    f"{c.get('llm_cache_read_tokens', 0):g}",
                    f"${b.get('llm', 0):.4f}",
                    f"{c.get('tts_chars_billed', 0):g}",
                    f"{c.get('response_latency_avg_s', 0):.2f}s",
                ]
            )
        print(f"profile {args.profile}; scripted 8-turn booking call; price book as of {book.as_of}")
        print(
            _fmt_table(
                rows,
                [
                    "levers",
                    "call",
                    "total",
                    "$/min",
                    "stt billed",
                    "llm calls",
                    "llm in tok",
                    "cached tok",
                    "llm $",
                    "tts chars",
                    "latency",
                ],
            )
        )
        return 0
    levers = Levers.all_off() if args.levers_off else Levers()
    result = asyncio.run(run_call(book, args.profile, levers=levers))
    print(result.meter.format())
    print("turns:")
    for t in result.session.turns:
        lat = f"{t.latency_s * 1000:.0f}ms" if t.latency_s is not None else "-"
        flag = " (interrupted)" if t.interrupted else ""
        print(f"  [{t.source:<16} {lat:>6}] user={t.user_text!r} -> agent={t.agent_text!r}{flag}")
    if args.json:
        print(json.dumps(result.meter.to_dict(), indent=2))
    return 0


def cmd_pricebook(args: argparse.Namespace) -> int:
    book = PriceBook.load(args.pricebook)
    print(f"as of {book.as_of}")
    rows = []
    for s in book.skus(args.component):
        rates = ", ".join(f"{k}={v:g}" for k, v in sorted(s.rates.items()))
        rows.append([s.component, s.name, rates, s.as_of, s.source])
    print(_fmt_table(rows, ["component", "sku", "rates (USD)", "as of", "source"]))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    asyncio.run(serve(args))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="voice-agent", description=__doc__)
    p.add_argument("--pricebook", default=None, help="path to a price book JSON (default: bundled)")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("estimate", help="price a call under each profile from average assumptions")
    e.add_argument("--profile", default="all", choices=["all", *PROFILES])
    e.add_argument("--minutes", type=float, default=3.0)
    e.add_argument("--monthly-minutes", type=int, default=100_000)
    e.add_argument("--system-tokens", type=int, default=1500)
    e.add_argument("--tts-cache-hit-rate", type=float, default=0.15)
    e.add_argument("--fastpath-rate", type=float, default=0.10)
    e.add_argument(
        "--utilization", type=float, default=0.35, help="average utilisation of self-hosted capacity"
    )
    e.add_argument("--no-vad-gating", action="store_true")
    e.add_argument("--no-prompt-cache", action="store_true")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_estimate)

    s = sub.add_parser("simulate", help="run a scripted call through the real pipeline with mock providers")
    s.add_argument("--profile", default="budget-hosted", choices=[n for n, pr in PROFILES.items() if pr.llm])
    s.add_argument("--compare", action="store_true", help="show the ledger as each lever is switched on")
    s.add_argument("--levers-off", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_simulate)

    b = sub.add_parser("pricebook", help="list the price book")
    b.add_argument("--component", default=None)
    b.set_defaults(func=cmd_pricebook)

    v = sub.add_parser("serve", help="WebSocket server for PCM16 clients and Twilio Media Streams")
    v.add_argument("--host", default="0.0.0.0")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--profile", default="budget-hosted")
    v.add_argument("--mock", action="store_true", help="use the offline mock STT/LLM/TTS (no API keys)")
    v.add_argument("--ledger-dir", default="ledgers")
    v.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
