"""Unit tests for agent_trader.engine.positions.apply_fill / unrealised_pnl."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from agent_trader.charges import Side
from agent_trader.engine.positions import apply_fill, unrealised_pnl

D = Decimal


def test_open_long_from_flat() -> None:
    qty, avg, pnl = apply_fill(0, D("0"), Side.BUY, 10, D("100.00"))
    assert (qty, avg, pnl) == (10, D("100.0000"), D("0.00"))
    assert avg.as_tuple().exponent == -4
    assert pnl.as_tuple().exponent == -2


def test_open_short_from_flat() -> None:
    qty, avg, pnl = apply_fill(0, D("0"), Side.SELL, 10, D("250.50"))
    assert (qty, avg, pnl) == (-10, D("250.5000"), D("0.00"))


def test_add_to_long_reweights_average_vwap() -> None:
    qty, avg, pnl = apply_fill(10, D("100"), Side.BUY, 30, D("120"))
    # (10×100 + 30×120) / 40 = 4600/40 = 115
    assert (qty, avg, pnl) == (40, D("115.0000"), D("0.00"))
    # non-terminating average is quantised half-up to 4 dp: (100 + 101 + 103)/3 = 101.3333...
    qty, avg, pnl = apply_fill(2, D("100.5"), Side.BUY, 1, D("103"))
    assert qty == 3 and avg == D("101.3333") and pnl == D("0.00")
    qty, avg, _ = apply_fill(2, D("100"), Side.BUY, 1, D("100.10"))  # 300.10/3 = 100.03333 → 100.0333
    assert avg == D("100.0333")


def test_add_to_short_reweights_average() -> None:
    qty, avg, pnl = apply_fill(-10, D("100"), Side.SELL, 10, D("110"))
    assert (qty, avg, pnl) == (-20, D("105.0000"), D("0.00"))


def test_partial_close_long_books_realised_pnl_keeps_avg() -> None:
    qty, avg, pnl = apply_fill(10, D("100"), Side.SELL, 4, D("110"))
    assert qty == 6
    assert avg == D("100")  # untouched on a reduce
    assert pnl == D("40.00")  # (110-100) × 4
    # a loss
    qty, avg, pnl = apply_fill(10, D("100"), Side.SELL, 4, D("95.50"))
    assert (qty, avg, pnl) == (6, D("100"), D("-18.00"))


def test_partial_close_short_books_realised_pnl() -> None:
    qty, avg, pnl = apply_fill(-10, D("100"), Side.BUY, 3, D("90"))
    assert (qty, avg, pnl) == (-7, D("100"), D("30.00"))  # (100-90) × 3
    qty, avg, pnl = apply_fill(-10, D("100"), Side.BUY, 3, D("104"))
    assert (qty, avg, pnl) == (-7, D("100"), D("-12.00"))


def test_full_close_resets_average_to_zero() -> None:
    qty, avg, pnl = apply_fill(10, D("100"), Side.SELL, 10, D("101.25"))
    assert (qty, avg, pnl) == (0, D("0"), D("12.50"))
    qty, avg, pnl = apply_fill(-5, D("200"), Side.BUY, 5, D("210"))
    assert (qty, avg, pnl) == (0, D("0"), D("-50.00"))


def test_flip_long_to_short_opens_remainder_at_fill_price() -> None:
    qty, avg, pnl = apply_fill(10, D("100"), Side.SELL, 15, D("110"))
    assert qty == -5
    assert avg == D("110.0000")  # remainder opened at the fill price
    assert pnl == D("100.00")  # only the 10 closed shares realise: (110-100)×10


def test_flip_short_to_long_opens_remainder_at_fill_price() -> None:
    qty, avg, pnl = apply_fill(-10, D("100"), Side.BUY, 25, D("90"))
    assert qty == 15
    assert avg == D("90.0000")
    assert pnl == D("100.00")  # (100-90)×10


def test_short_cover_full() -> None:
    qty, avg, pnl = apply_fill(-20, D("50"), Side.BUY, 20, D("45"))
    assert (qty, avg, pnl) == (0, D("0"), D("100.00"))


def test_realised_pnl_rounded_to_paise() -> None:
    # (100.0033 - 100) × 3 = 0.0099 → 0.01
    qty, avg, pnl = apply_fill(3, D("100"), Side.SELL, 3, D("100.0033"))
    assert pnl == D("0.01")
    assert pnl.as_tuple().exponent == -2


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_fill_qty_must_be_positive(bad: int) -> None:
    with pytest.raises(ValueError):
        apply_fill(10, D("100"), Side.BUY, bad, D("100"))
    with pytest.raises(ValueError):
        apply_fill(0, D("0"), Side.SELL, bad, D("100"))


def test_unrealised_pnl_long_and_short() -> None:
    assert unrealised_pnl(10, D("100"), D("105.5")) == D("55.00")
    assert unrealised_pnl(-10, D("100"), D("105.5")) == D("-55.00")
    assert unrealised_pnl(0, D("0"), D("105.5")) == D("0.00")
    assert unrealised_pnl(3, D("100.0033"), D("100")) == D("-0.01")  # -0.0099 → -0.01


# ---- randomised property test -----------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3, 42, 2026])
def test_random_fill_sequence_bookkeeping_and_cash_flow_identity(seed: int) -> None:
    """Quantity is the signed sum of fills, avg is 0 iff flat, and realised P&L reconciles with cash flows.

    Identity: sum(sell proceeds) - sum(buy cost) + final_qty × final_avg == sum(realised)
    (the position's residual is valued at cost, which is what avg_price represents). The
    average is quantised to 4 dp and each realised amount to paise, so the identity holds up
    to a small, bounded rounding tolerance.
    """
    rng = random.Random(seed)
    qty, avg = 0, D("0")
    realised_total = D("0")
    buy_cost = D("0")
    sell_proceeds = D("0")
    n_fills = 200
    prices_seen: list[Decimal] = []
    for _ in range(n_fills):
        side = rng.choice([Side.BUY, Side.SELL])
        fill_qty = rng.randint(1, 50)
        price = D(rng.randint(2000, 4000)) * D("0.05")  # ₹100 – ₹200 on a 0.05 tick
        prices_seen.append(price)
        signed = fill_qty if side == Side.BUY else -fill_qty

        new_qty, new_avg, pnl = apply_fill(qty, avg, side, fill_qty, price)

        assert new_qty == qty + signed
        assert (new_qty == 0) == (new_avg == 0)
        if new_qty != 0:
            assert new_avg > 0
            assert min(prices_seen) <= new_avg <= max(prices_seen)
            assert new_avg.as_tuple().exponent >= -4
        assert pnl.as_tuple().exponent >= -2
        if qty == 0 or (qty > 0) == (signed > 0):
            assert pnl == 0  # opening/adding never realises
        else:
            assert new_avg in (avg, D("0"), price)  # reduce keeps avg; close → 0; flip → fill price

        if side == Side.BUY:
            buy_cost += price * fill_qty
        else:
            sell_proceeds += price * fill_qty
        realised_total += pnl
        qty, avg = new_qty, new_avg

    identity = sell_proceeds - buy_cost + avg * qty
    # bound: each fill can introduce ≤ ₹0.005 realised rounding plus ≤ 0.00005/share × 2500 shares max
    tolerance = D("0.01") * n_fills + D("0.00005") * 2500
    assert abs(identity - realised_total) <= tolerance, (identity, realised_total)


def test_random_sequence_is_exact_when_no_rounding_needed() -> None:
    """With whole-rupee prices and fills that always fully close before re-opening, the identity is exact."""
    rng = random.Random(99)
    qty, avg = 0, D("0")
    realised_total = D("0")
    cash = D("0")
    for _ in range(100):
        # open a position of random sign and size at an integer price, then close it fully
        size = rng.randint(1, 100)
        open_side = rng.choice([Side.BUY, Side.SELL])
        p_open = D(rng.randint(100, 500))
        p_close = D(rng.randint(100, 500))
        qty, avg, pnl = apply_fill(qty, avg, open_side, size, p_open)
        assert pnl == 0
        cash += -p_open * size if open_side == Side.BUY else p_open * size
        close_side = Side.SELL if open_side == Side.BUY else Side.BUY
        qty, avg, pnl = apply_fill(qty, avg, close_side, size, p_close)
        assert qty == 0 and avg == 0
        cash += -p_close * size if close_side == Side.BUY else p_close * size
        expected = (p_close - p_open) * size if open_side == Side.BUY else (p_open - p_close) * size
        assert pnl == expected
        realised_total += pnl
    assert cash == realised_total
