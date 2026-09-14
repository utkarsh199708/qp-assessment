"""IST market clock: sessions, phases, holidays and a controllable clock for tests.

All times are Asia/Kolkata (UTC+05:30, no DST). The clock can run in three modes:

* ``real``        – wall-clock time; the market is open only during real NSE hours.
* ``frozen``      – time is fixed at ``frozen_at`` and only moves via :meth:`MarketClock.advance`
                    or :meth:`MarketClock.set`; used by tests and deterministic replays.
* ``always_open`` – wall-clock time, but every instant is treated as the NORMAL session and
                    holidays/weekends are ignored; used for local development so agents can
                    trade at 3 a.m.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Literal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

ClockMode = Literal["real", "frozen", "always_open"]


class MarketPhase(str, Enum):
    """Trading session phases of the NSE/BSE cash segment."""

    CLOSED = "CLOSED"
    PRE_OPEN = "PRE_OPEN"  # 09:00–09:15: orders accepted and queued, no continuous matching
    NORMAL = "NORMAL"  # 09:15–15:30: continuous trading
    CLOSING = "CLOSING"  # 15:40–16:00: post-close session, trades at the closing price
    HOLIDAY = "HOLIDAY"  # trading holiday or weekend


PRE_OPEN_START = time(9, 0)
NORMAL_START = time(9, 15)
MIS_SQUARE_OFF = time(15, 20)
NORMAL_END = time(15, 30)
CLOSING_START = time(15, 40)
CLOSING_END = time(16, 0)

# NSE trading holidays 2026 (cash segment), weekdays only. Source: NSE holiday circular for
# CY2026. Weekend holidays (e.g. 15 Aug 2026 is a Saturday) are omitted because weekends are
# always closed. Override/extend via ``TRADER_EXTRA_HOLIDAYS``.
NSE_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 26),  # Republic Day
        date(2026, 3, 3),  # Holi
        date(2026, 3, 26),  # Shri Ram Navami
        date(2026, 3, 31),  # Shri Mahavir Jayanti
        date(2026, 4, 3),  # Good Friday
        date(2026, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
        date(2026, 5, 1),  # Maharashtra Day
        date(2026, 5, 28),  # Bakri Id
        date(2026, 6, 26),  # Muharram
        date(2026, 9, 14),  # Ganesh Chaturthi
        date(2026, 10, 2),  # Mahatma Gandhi Jayanti
        date(2026, 10, 20),  # Dussehra
        date(2026, 11, 9),  # Diwali Balipratipada
        date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
        date(2026, 12, 25),  # Christmas
    }
)

# NSE trading holidays 2025 (kept so replays of 2025 data behave correctly).
NSE_HOLIDAYS_2025: frozenset[date] = frozenset(
    {
        date(2025, 2, 26),  # Mahashivratri
        date(2025, 3, 14),  # Holi
        date(2025, 3, 31),  # Id-Ul-Fitr
        date(2025, 4, 10),  # Shri Mahavir Jayanti
        date(2025, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
        date(2025, 4, 18),  # Good Friday
        date(2025, 5, 1),  # Maharashtra Day
        date(2025, 8, 15),  # Independence Day
        date(2025, 8, 27),  # Ganesh Chaturthi
        date(2025, 10, 2),  # Mahatma Gandhi Jayanti / Dussehra
        date(2025, 10, 21),  # Diwali Laxmi Pujan
        date(2025, 10, 22),  # Diwali Balipratipada
        date(2025, 11, 5),  # Prakash Gurpurb Sri Guru Nanak Dev
        date(2025, 12, 25),  # Christmas
    }
)

DEFAULT_HOLIDAYS: frozenset[date] = NSE_HOLIDAYS_2025 | NSE_HOLIDAYS_2026


def to_ist(dt: datetime) -> datetime:
    """Return ``dt`` as an aware datetime in IST (naive input is assumed to be IST)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


@dataclass
class MarketClock:
    mode: ClockMode = "real"
    frozen_at: datetime | None = None
    holidays: frozenset[date] = field(default_factory=lambda: DEFAULT_HOLIDAYS)

    def __post_init__(self) -> None:
        if self.mode == "frozen":
            if self.frozen_at is None:
                # A sensible default: a regular trading Wednesday, mid-session.
                self.frozen_at = datetime(2026, 9, 16, 10, 0, tzinfo=IST)
            self.frozen_at = to_ist(self.frozen_at)

    # ---- time -------------------------------------------------------------------------

    def now(self) -> datetime:
        if self.mode == "frozen":
            assert self.frozen_at is not None
            return self.frozen_at
        return datetime.now(tz=timezone.utc).astimezone(IST)

    def today(self) -> date:
        return self.now().date()

    def set(self, at: datetime) -> None:
        """Move a frozen clock to ``at`` (IST if naive)."""
        if self.mode != "frozen":
            raise RuntimeError("clock.set() is only valid in frozen mode")
        self.frozen_at = to_ist(at)

    def advance(self, delta: timedelta) -> None:
        if self.mode != "frozen":
            raise RuntimeError("clock.advance() is only valid in frozen mode")
        assert self.frozen_at is not None
        self.frozen_at = self.frozen_at + delta

    # ---- calendar ---------------------------------------------------------------------

    def is_trading_day(self, d: date | None = None) -> bool:
        if self.mode == "always_open":
            return True
        d = d or self.today()
        return d.weekday() < 5 and d not in self.holidays

    def next_trading_day(self, after: date | None = None) -> date:
        d = (after or self.today()) + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    # ---- phases -----------------------------------------------------------------------

    @staticmethod
    def phase_for_time(t: time) -> MarketPhase:
        if PRE_OPEN_START <= t < NORMAL_START:
            return MarketPhase.PRE_OPEN
        if NORMAL_START <= t < NORMAL_END:
            return MarketPhase.NORMAL
        if CLOSING_START <= t < CLOSING_END:
            return MarketPhase.CLOSING
        return MarketPhase.CLOSED

    def phase(self, at: datetime | None = None) -> MarketPhase:
        if self.mode == "always_open":
            return MarketPhase.NORMAL
        at = to_ist(at) if at else self.now()
        if not self.is_trading_day(at.date()):
            return MarketPhase.HOLIDAY
        return self.phase_for_time(at.time())

    def is_open(self, at: datetime | None = None) -> bool:
        """True during continuous trading (NORMAL session)."""
        return self.phase(at) == MarketPhase.NORMAL

    def accepts_orders(self, at: datetime | None = None) -> bool:
        """True when the exchange accepts new orders (pre-open, normal, closing)."""
        return self.phase(at) in (MarketPhase.PRE_OPEN, MarketPhase.NORMAL, MarketPhase.CLOSING)

    def is_past_square_off(self, at: datetime | None = None) -> bool:
        """True from 15:20 IST on a trading day: MIS positions get force-closed."""
        if self.mode == "always_open":
            return False
        at = to_ist(at) if at else self.now()
        return self.is_trading_day(at.date()) and MIS_SQUARE_OFF <= at.time() < NORMAL_END

    def next_open(self, at: datetime | None = None) -> datetime:
        """Next instant at which the NORMAL session starts (>= ``at``)."""
        at = to_ist(at) if at else self.now()
        if self.mode == "always_open":
            return at
        d = at.date()
        if self.is_trading_day(d) and at.time() < NORMAL_START:
            return datetime.combine(d, NORMAL_START, tzinfo=IST)
        d = self.next_trading_day(d)
        return datetime.combine(d, NORMAL_START, tzinfo=IST)

    def session_close(self, d: date | None = None) -> datetime:
        return datetime.combine(d or self.today(), NORMAL_END, tzinfo=IST)

    def status(self) -> dict:
        """Machine-readable snapshot for the ``market_status`` tool."""
        now = self.now()
        ph = self.phase(now)
        return {
            "phase": ph.value,
            "is_open": ph == MarketPhase.NORMAL,
            "accepts_orders": self.accepts_orders(now),
            "ist_now": now.isoformat(),
            "trading_day": self.is_trading_day(now.date()),
            "next_open_ist": self.next_open(now).isoformat(),
            "session": {
                "pre_open": f"{PRE_OPEN_START:%H:%M}-{NORMAL_START:%H:%M}",
                "normal": f"{NORMAL_START:%H:%M}-{NORMAL_END:%H:%M}",
                "mis_square_off": f"{MIS_SQUARE_OFF:%H:%M}",
                "closing": f"{CLOSING_START:%H:%M}-{CLOSING_END:%H:%M}",
            },
            "clock_mode": self.mode,
        }
