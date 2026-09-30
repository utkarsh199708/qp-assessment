"""Price book, per-call cost meter and prompt-cache arithmetic.

The price book is data (``pricebook.json``), not code: every SKU carries its
list price, unit, billing increment, source URL and the date it was checked,
so the numbers can be refreshed without touching the pipeline.

Rate names encode their unit: ``<label>_per_<unit>`` with unit one of
``sec | min | hour | month | call | char | 1k_chars | 1m_chars | ktok | mtok``.
Quantities are always passed in *base* units (seconds, characters, tokens,
calls, months) and converted here, so callers never do unit maths.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

_UNIT_DIVISOR = {
    "sec": 1.0,
    "min": 60.0,
    "hour": 3600.0,
    "month": 1.0,
    "call": 1.0,
    "char": 1.0,
    "1k_chars": 1_000.0,
    "1m_chars": 1_000_000.0,
    "ktok": 1_000.0,
    "mtok": 1_000_000.0,
}

_UNIT_BASE = {
    "sec": "seconds",
    "min": "seconds",
    "hour": "seconds",
    "month": "months",
    "call": "calls",
    "char": "chars",
    "1k_chars": "chars",
    "1m_chars": "chars",
    "ktok": "tokens",
    "mtok": "tokens",
}


def rate_unit(rate: str) -> str:
    """``"input_per_mtok"`` -> ``"mtok"``, ``"per_min"`` -> ``"min"``. Raises on an unknown suffix."""
    if rate.startswith("per_"):
        unit = rate[len("per_") :]
    elif "_per_" in rate:
        unit = rate.rsplit("_per_", 1)[1]
    else:
        raise ValueError(f"rate name {rate!r} must look like '[<label>_]per_<unit>'")
    if unit not in _UNIT_DIVISOR:
        raise ValueError(f"unknown unit {unit!r} in rate {rate!r}")
    return unit


def is_rate(key: str) -> bool:
    try:
        rate_unit(key)
    except ValueError:
        return False
    return True


def base_unit(rate: str) -> str:
    return _UNIT_BASE[rate_unit(rate)]


@dataclass(frozen=True)
class Sku:
    component: str
    name: str
    rates: dict[str, float]
    increment_s: float = 0.0
    attrs: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    as_of: str = ""
    note: str = ""

    def price(self, rate: str) -> float:
        if rate not in self.rates:
            raise KeyError(f"{self.component}/{self.name} has no rate {rate!r}; has {sorted(self.rates)}")
        return self.rates[rate]

    def cost(self, rate: str, quantity: float) -> float:
        """Cost of ``quantity`` base units under ``rate`` (time rounded up to the increment)."""
        unit = rate_unit(rate)
        if _UNIT_BASE[unit] == "seconds" and self.increment_s > 0 and quantity > 0:
            # round before ceil so 100 x 0.02 s does not become 3 s at a 1 s increment
            quantity = math.ceil(round(quantity / self.increment_s, 6)) * self.increment_s
        return quantity / _UNIT_DIVISOR[unit] * self.price(rate)


_RESERVED = {"increment_s", "source", "as_of", "note"}


class PriceBook:
    COMPONENTS = ("telephony", "stt", "tts", "llm", "s2s", "compute", "platform")

    def __init__(self, data: dict[str, Any]):
        self.as_of: str = data.get("as_of", "")
        self.assumptions: dict[str, Any] = data.get("assumptions", {})
        self._skus: dict[tuple[str, str], Sku] = {}
        for component in self.COMPONENTS:
            for name, raw in data.get(component, {}).items():
                rates = {k: float(v) for k, v in raw.items() if is_rate(k)}
                attrs = {k: v for k, v in raw.items() if k not in rates and k not in _RESERVED}
                self._skus[(component, name)] = Sku(
                    component=component,
                    name=name,
                    rates=rates,
                    increment_s=float(raw.get("increment_s", 0.0)),
                    attrs=attrs,
                    source=str(raw.get("source", "")),
                    as_of=str(raw.get("as_of", self.as_of)),
                    note=str(raw.get("note", "")),
                )

    @classmethod
    def load(cls, path: str | Path | None = None) -> PriceBook:
        if path is None:
            text = resources.files("voice_agent").joinpath("pricebook.json").read_text()
        else:
            text = Path(path).read_text()
        return cls(json.loads(text))

    def get(self, component: str, name: str) -> Sku:
        try:
            return self._skus[(component, name)]
        except KeyError:
            known = sorted(n for c, n in self._skus if c == component)
            raise KeyError(f"no {component} SKU {name!r}; known: {known}") from None

    def skus(self, component: str | None = None) -> list[Sku]:
        return [s for (c, _), s in self._skus.items() if component is None or c == component]

    def cost(self, component: str, name: str, rate: str, quantity: float) -> float:
        return self.get(component, name).cost(rate, quantity)


# --- prompt-cache arithmetic ----------------------------------------------------


@dataclass(frozen=True)
class CacheSplit:
    """How one request's input tokens are billed."""

    uncached: int
    cache_write: int
    cache_read: int

    @property
    def total(self) -> int:
        return self.uncached + self.cache_write + self.cache_read


def split_prompt_cache(
    prefix_total: int, cached_prefix: int, *, enabled: bool, min_tokens: int
) -> CacheSplit:
    """Bill a request whose whole prompt is ``prefix_total`` tokens when the first
    ``cached_prefix`` tokens were cached by the previous request.

    Models refuse to cache prefixes shorter than ``min_tokens`` (4096 on Claude
    Haiku 4.5, 512 on Sonnet 5.5 / Opus 5.5): below that everything is billed at
    the full input price even with cache markers set.
    """
    if not enabled or prefix_total < min_tokens:
        return CacheSplit(uncached=prefix_total, cache_write=0, cache_read=0)
    cached_prefix = max(0, min(cached_prefix, prefix_total))
    return CacheSplit(uncached=0, cache_write=prefix_total - cached_prefix, cache_read=cached_prefix)


def estimate_tokens(text: str) -> int:
    """Cheap tokenizer stand-in (~4 chars/token for English) for mocks and estimates."""
    return max(1, math.ceil(len(text) / 4))


# --- per-call meter --------------------------------------------------------------


@dataclass
class LineItem:
    component: str
    sku: str
    rate: str
    quantity: float
    unit: str
    usd: float


@dataclass
class CostMeter:
    """Accumulates billable quantities for one call and prices them at report time.

    Time-based quantities are summed first and rounded to the SKU's billing
    increment once, which is how providers bill a call leg or a streaming session.
    """

    book: PriceBook
    call_id: str = "call"
    _quantities: dict[tuple[str, str, str], float] = field(default_factory=lambda: defaultdict(float))
    counters: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    duration_s: float = 0.0

    def add(self, component: str, sku: str, rate: str, quantity: float) -> None:
        if quantity <= 0:
            return
        self.book.get(component, sku).price(rate)  # validate early
        self._quantities[(component, sku, rate)] += quantity

    def add_llm(self, sku: str, split: CacheSplit, output_tokens: int) -> None:
        self.add("llm", sku, "input_per_mtok", split.uncached)
        self.add("llm", sku, "cache_write_per_mtok", split.cache_write)
        self.add("llm", sku, "cache_read_per_mtok", split.cache_read)
        self.add("llm", sku, "output_per_mtok", output_tokens)
        self.counters["llm_calls"] += 1
        self.counters["llm_input_tokens"] += split.total
        self.counters["llm_cache_read_tokens"] += split.cache_read
        self.counters["llm_output_tokens"] += output_tokens

    def bump(self, counter: str, by: float = 1) -> None:
        self.counters[counter] += by

    def lines(self) -> list[LineItem]:
        out: list[LineItem] = []
        for (component, sku, rate), qty in sorted(self._quantities.items()):
            s = self.book.get(component, sku)
            out.append(LineItem(component, sku, rate, qty, base_unit(rate), s.cost(rate, qty)))
        return out

    def breakdown(self) -> dict[str, float]:
        agg: dict[str, float] = defaultdict(float)
        for line in self.lines():
            agg[line.component] += line.usd
        return dict(agg)

    def total(self) -> float:
        return sum(line.usd for line in self.lines())

    def usd_per_minute(self) -> float:
        return self.total() / (self.duration_s / 60.0) if self.duration_s > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "duration_s": round(self.duration_s, 2),
            "total_usd": round(self.total(), 6),
            "usd_per_min": round(self.usd_per_minute(), 6),
            "breakdown_usd": {k: round(v, 6) for k, v in self.breakdown().items()},
            "lines": [
                {
                    "component": line.component,
                    "sku": line.sku,
                    "rate": line.rate,
                    "quantity": round(line.quantity, 3),
                    "unit": line.unit,
                    "usd": round(line.usd, 6),
                }
                for line in self.lines()
            ],
            "counters": {
                k: (round(v, 3) if isinstance(v, float) else v) for k, v in sorted(self.counters.items())
            },
            "pricebook_as_of": self.book.as_of,
        }

    def format(self) -> str:
        rows = [
            f"call {self.call_id}: {self.duration_s:.1f}s  total ${self.total():.4f}  (${self.usd_per_minute():.4f}/min)"
        ]
        for line in self.lines():
            rows.append(
                f"  {line.component:<10} {line.sku:<28} {line.rate:<22} {line.quantity:>12.2f} {line.unit:<8} ${line.usd:.5f}"
            )
        if self.counters:
            rows.append(
                "  counters: "
                + ", ".join(
                    f"{k}={v:g}" if isinstance(v, int | float) else f"{k}={v}"
                    for k, v in sorted(self.counters.items())
                )
            )
        return "\n".join(rows)
