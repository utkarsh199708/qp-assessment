"""Unit tests for agent_trader.clock: phases, holidays, next_open, square-off and clock modes."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

import pytest

from agent_trader.clock import (
    DEFAULT_HOLIDAYS,
    IST,
    NSE_HOLIDAYS_2025,
    NSE_HOLIDAYS_2026,
    MarketClock,
    MarketPhase,
    to_ist,
)

WED = date(2026, 9, 16)  # a regular trading Wednesday


def at(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=IST)


# ---- phases at boundaries ---------------------------------------------------------------


@pytest.mark.parametrize(
    "t, expected",
    [
        (time(8, 59), MarketPhase.CLOSED),
        (time(9, 0), MarketPhase.PRE_OPEN),
        (time(9, 14, 59), MarketPhase.PRE_OPEN),
        (time(9, 15), MarketPhase.NORMAL),
        (time(15, 19, 59), MarketPhase.NORMAL),
        (time(15, 20), MarketPhase.NORMAL),
        (time(15, 29, 59), MarketPhase.NORMAL),
        (time(15, 30), MarketPhase.CLOSED),
        (time(15, 39, 59), MarketPhase.CLOSED),
        (time(15, 40), MarketPhase.CLOSING),
        (time(15, 59, 59), MarketPhase.CLOSING),
        (time(16, 0), MarketPhase.CLOSED),
        (time(0, 0), MarketPhase.CLOSED),
        (time(23, 59, 59), MarketPhase.CLOSED),
    ],
)
def test_phase_boundaries_on_trading_day(clock: MarketClock, t: time, expected: MarketPhase) -> None:
    assert MarketClock.phase_for_time(t) == expected
    when = datetime.combine(WED, t, tzinfo=IST)
    assert clock.phase(when) == expected
    # the same answer when the frozen clock itself is moved there
    clock.set(when)
    assert clock.phase() == expected
    assert clock.is_open() == (expected == MarketPhase.NORMAL)
    assert clock.accepts_orders() == (
        expected in (MarketPhase.PRE_OPEN, MarketPhase.NORMAL, MarketPhase.CLOSING)
    )


def test_is_open_only_in_normal_session(clock: MarketClock) -> None:
    assert clock.is_open(at(WED, 9, 10)) is False
    assert clock.is_open(at(WED, 9, 15)) is True
    assert clock.is_open(at(WED, 15, 45)) is False  # CLOSING accepts orders but is not "open"
    assert clock.accepts_orders(at(WED, 15, 45)) is True
    assert clock.accepts_orders(at(WED, 15, 35)) is False


# ---- holidays and weekends --------------------------------------------------------------


@pytest.mark.parametrize(
    "d",
    [
        date(2026, 9, 14),  # Ganesh Chaturthi (Monday)
        date(2026, 10, 2),  # Mahatma Gandhi Jayanti (Friday)
        date(2026, 12, 25),  # Christmas (Friday)
        date(2026, 9, 19),  # Saturday
        date(2026, 9, 20),  # Sunday
    ],
)
def test_holidays_and_weekends_are_holiday_phase_all_day(clock: MarketClock, d: date) -> None:
    assert clock.is_trading_day(d) is False
    for t in (time(8, 59), time(9, 0), time(9, 15), time(12, 0), time(15, 20), time(15, 45)):
        when = datetime.combine(d, t, tzinfo=IST)
        assert clock.phase(when) == MarketPhase.HOLIDAY
        assert clock.is_open(when) is False
        assert clock.accepts_orders(when) is False
        assert clock.is_past_square_off(when) is False


def test_holiday_tables_are_weekdays_only_and_merged() -> None:
    assert date(2026, 9, 14) in NSE_HOLIDAYS_2026
    assert all(d.weekday() < 5 for d in NSE_HOLIDAYS_2026)
    assert all(d.weekday() < 5 for d in NSE_HOLIDAYS_2025)
    assert DEFAULT_HOLIDAYS == NSE_HOLIDAYS_2025 | NSE_HOLIDAYS_2026
    assert MarketClock(mode="frozen").holidays == DEFAULT_HOLIDAYS


def test_regular_weekdays_are_trading_days(clock: MarketClock) -> None:
    assert clock.is_trading_day(WED) is True
    assert clock.is_trading_day(date(2026, 9, 15)) is True  # Tuesday after the holiday
    assert clock.is_trading_day() is True  # defaults to today (frozen at 2026-09-16)


def test_custom_holiday_set_overrides_default() -> None:
    c = MarketClock(mode="frozen", frozen_at=at(WED, 10, 0), holidays=frozenset({WED}))
    assert c.is_trading_day(WED) is False
    assert c.phase() == MarketPhase.HOLIDAY
    assert c.is_trading_day(date(2026, 9, 14)) is True  # no longer a holiday under the custom set


# ---- next_open / next_trading_day --------------------------------------------------------


def test_next_open_same_day_before_open(clock: MarketClock) -> None:
    assert clock.next_open(at(WED, 8, 0)) == at(WED, 9, 15)
    assert clock.next_open(at(WED, 9, 14, 59)) == at(WED, 9, 15)


def test_next_open_after_open_is_next_trading_day(clock: MarketClock) -> None:
    assert clock.next_open(at(WED, 10, 0)) == at(date(2026, 9, 17), 9, 15)
    assert clock.next_open(at(WED, 16, 30)) == at(date(2026, 9, 17), 9, 15)


def test_next_open_across_weekend(clock: MarketClock) -> None:
    fri = date(2026, 9, 18)
    mon = date(2026, 9, 21)
    assert clock.next_open(at(fri, 16, 0)) == at(mon, 9, 15)
    assert clock.next_open(at(date(2026, 9, 19), 10, 0)) == at(mon, 9, 15)  # Saturday
    assert clock.next_open(at(date(2026, 9, 20), 3, 0)) == at(mon, 9, 15)  # Sunday, before 09:15
    assert clock.next_trading_day(fri) == mon


def test_next_open_across_holiday(clock: MarketClock) -> None:
    # Fri 11 Sep → (Sat, Sun, Mon 14 Sep holiday) → Tue 15 Sep
    assert clock.next_open(at(date(2026, 9, 11), 15, 45)) == at(date(2026, 9, 15), 9, 15)
    assert clock.next_open(at(date(2026, 9, 14), 8, 0)) == at(date(2026, 9, 15), 9, 15)
    assert clock.next_trading_day(date(2026, 9, 11)) == date(2026, 9, 15)
    # Thu 1 Oct → Fri 2 Oct holiday + weekend → Mon 5 Oct
    assert clock.next_open(at(date(2026, 10, 1), 16, 0)) == at(date(2026, 10, 5), 9, 15)
    # Christmas Friday → Mon 28 Dec
    assert clock.next_open(at(date(2026, 12, 25), 10, 0)) == at(date(2026, 12, 28), 9, 15)


def test_next_open_defaults_to_now_and_is_aware_ist(clock: MarketClock) -> None:
    nxt = clock.next_open()
    assert nxt.tzinfo is not None and nxt.utcoffset() == timedelta(hours=5, minutes=30)
    assert nxt == at(date(2026, 9, 17), 9, 15)


def test_next_open_accepts_naive_and_utc_input(clock: MarketClock) -> None:
    assert clock.next_open(datetime(2026, 9, 16, 8, 0)) == at(WED, 9, 15)
    # 02:00 UTC == 07:30 IST on the same day
    assert clock.next_open(datetime(2026, 9, 16, 2, 0, tzinfo=UTC)) == at(WED, 9, 15)


def test_session_close(clock: MarketClock) -> None:
    assert clock.session_close() == at(WED, 15, 30)
    assert clock.session_close(date(2026, 9, 18)) == at(date(2026, 9, 18), 15, 30)


# ---- MIS square-off window ---------------------------------------------------------------


@pytest.mark.parametrize(
    "t, expected",
    [
        (time(15, 19, 59), False),
        (time(15, 20), True),
        (time(15, 25), True),
        (time(15, 29, 59), True),
        (time(15, 30), False),
        (time(15, 45), False),
        (time(10, 0), False),
    ],
)
def test_is_past_square_off_window(clock: MarketClock, t: time, expected: bool) -> None:
    when = datetime.combine(WED, t, tzinfo=IST)
    assert clock.is_past_square_off(when) is expected
    clock.set(when)
    assert clock.is_past_square_off() is expected


def test_is_past_square_off_false_on_holiday(clock: MarketClock) -> None:
    assert clock.is_past_square_off(at(date(2026, 9, 14), 15, 25)) is False
    assert clock.is_past_square_off(at(date(2026, 9, 19), 15, 25)) is False


# ---- frozen mode -------------------------------------------------------------------------


def test_frozen_now_set_and_advance(clock: MarketClock) -> None:
    assert clock.mode == "frozen"
    assert clock.now() == at(WED, 10, 0)
    assert clock.today() == WED

    clock.advance(timedelta(hours=5, minutes=20))
    assert clock.now() == at(WED, 15, 20)
    assert clock.phase() == MarketPhase.NORMAL
    assert clock.is_past_square_off() is True

    clock.set(datetime(2026, 9, 18, 9, 5))  # naive → interpreted as IST
    assert clock.now() == at(date(2026, 9, 18), 9, 5)
    assert clock.now().tzinfo is not None
    assert clock.phase() == MarketPhase.PRE_OPEN

    clock.set(datetime(2026, 9, 18, 4, 0, tzinfo=UTC))  # 09:30 IST
    assert clock.now() == at(date(2026, 9, 18), 9, 30)
    assert clock.now().utcoffset() == timedelta(hours=5, minutes=30)

    clock.advance(timedelta(days=-2))
    assert clock.today() == WED


def test_frozen_default_frozen_at_is_trading_wednesday() -> None:
    c = MarketClock(mode="frozen")
    assert c.frozen_at == at(WED, 10, 0)
    assert c.phase() == MarketPhase.NORMAL


def test_frozen_at_naive_is_normalised_to_ist() -> None:
    c = MarketClock(mode="frozen", frozen_at=datetime(2026, 9, 16, 11, 0))
    assert c.now().tzinfo is not None
    assert c.now() == at(WED, 11, 0)


def test_frozen_at_utc_is_converted_to_ist() -> None:
    c = MarketClock(mode="frozen", frozen_at=datetime(2026, 9, 16, 10, 0, tzinfo=UTC))
    assert c.now() == at(WED, 15, 30)
    assert c.phase() == MarketPhase.CLOSED


def test_set_and_advance_rejected_outside_frozen_mode() -> None:
    for mode in ("real", "always_open"):
        c = MarketClock(mode=mode)
        with pytest.raises(RuntimeError):
            c.set(at(WED, 10, 0))
        with pytest.raises(RuntimeError):
            c.advance(timedelta(minutes=1))


def test_real_mode_now_is_aware_ist() -> None:
    c = MarketClock(mode="real")
    now = c.now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=5, minutes=30)
    assert abs((now - datetime.now(tz=UTC)).total_seconds()) < 5


# ---- always_open mode --------------------------------------------------------------------


def test_always_open_ignores_calendar_and_session() -> None:
    c = MarketClock(mode="always_open")
    for when in (
        at(date(2026, 9, 14), 3, 0),  # holiday, middle of the night
        at(date(2026, 9, 19), 12, 0),  # Saturday
        at(WED, 8, 59),
        at(WED, 15, 45),
        at(WED, 23, 59),
    ):
        assert c.phase(when) == MarketPhase.NORMAL
        assert c.is_open(when) is True
        assert c.accepts_orders(when) is True
        assert c.is_past_square_off(when) is False
        assert c.is_trading_day(when.date()) is True
        assert c.next_open(when) == when  # "next open" is right now
    assert c.phase() == MarketPhase.NORMAL
    assert c.is_past_square_off() is False
    st = c.status()
    assert st["phase"] == "NORMAL" and st["is_open"] is True and st["trading_day"] is True
    assert st["clock_mode"] == "always_open"


# ---- status() ---------------------------------------------------------------------------


def test_status_snapshot_keys_and_values(clock: MarketClock) -> None:
    st = clock.status()
    assert set(st) == {
        "phase",
        "is_open",
        "accepts_orders",
        "ist_now",
        "trading_day",
        "next_open_ist",
        "session",
        "clock_mode",
    }
    assert st["phase"] == "NORMAL"
    assert st["is_open"] is True
    assert st["accepts_orders"] is True
    assert st["trading_day"] is True
    assert st["clock_mode"] == "frozen"
    assert st["ist_now"] == "2026-09-16T10:00:00+05:30"
    assert st["next_open_ist"] == "2026-09-17T09:15:00+05:30"
    assert st["session"] == {
        "pre_open": "09:00-09:15",
        "normal": "09:15-15:30",
        "mis_square_off": "15:20",
        "closing": "15:40-16:00",
    }


def test_status_on_holiday(clock: MarketClock) -> None:
    clock.set(at(date(2026, 9, 14), 10, 0))
    st = clock.status()
    assert st["phase"] == "HOLIDAY"
    assert st["is_open"] is False
    assert st["accepts_orders"] is False
    assert st["trading_day"] is False
    assert st["next_open_ist"] == "2026-09-15T09:15:00+05:30"


def test_status_in_closing_session(clock: MarketClock) -> None:
    clock.set(at(WED, 15, 45))
    st = clock.status()
    assert st["phase"] == "CLOSING"
    assert st["is_open"] is False
    assert st["accepts_orders"] is True


# ---- to_ist -----------------------------------------------------------------------------


def test_to_ist_naive_assumed_ist() -> None:
    out = to_ist(datetime(2026, 9, 16, 10, 0))
    assert out.tzinfo is not None
    assert out.utcoffset() == timedelta(hours=5, minutes=30)
    assert out.replace(tzinfo=None) == datetime(2026, 9, 16, 10, 0)
    assert out == at(WED, 10, 0)


def test_to_ist_utc_is_shifted() -> None:
    out = to_ist(datetime(2026, 9, 16, 4, 30, tzinfo=UTC))
    assert out == at(WED, 10, 0)
    assert out.hour == 10 and out.minute == 0
    # crossing midnight
    out = to_ist(datetime(2026, 9, 15, 20, 0, tzinfo=UTC))
    assert out.date() == WED and out.hour == 1 and out.minute == 30


def test_to_ist_aware_ist_unchanged() -> None:
    src = at(WED, 10, 0)
    out = to_ist(src)
    assert out == src and out.tzinfo is IST


def test_to_ist_other_offset() -> None:
    plus_two = timezone(timedelta(hours=2))
    out = to_ist(datetime(2026, 9, 16, 6, 30, tzinfo=plus_two))  # 04:30 UTC → 10:00 IST
    assert out == at(WED, 10, 0)
    assert out.utcoffset() == timedelta(hours=5, minutes=30)
