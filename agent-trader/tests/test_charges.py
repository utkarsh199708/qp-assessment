"""Unit tests for agent_trader.charges: statutory + brokerage charges, computed by hand."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from agent_trader.charges import (
    ChargeBreakdown,
    ChargeSchedule,
    Exchange,
    ProductType,
    Side,
    compute_charges,
    to_paise,
)

D = Decimal
LAKH = D("100000.00")  # quantity 100 × ₹1000 = ₹1,00,000 turnover

# Hand-computed from the documented rates on ₹1,00,000 turnover:
#   STT delivery 0.1% = 100.00; STT intraday sell 0.025% = 25.00
#   NSE exch 0.00297% = 2.97; BSE exch 0.00375% = 3.75; SEBI ₹10/crore = 0.10
#   stamp delivery buy 0.015% = 15.00; stamp intraday buy 0.003% = 3.00
#   brokerage MIS = min(20, 0.03% = 30) = 20.00; CNC = 0
#   GST 18% × (brokerage + exch + sebi):
#       NSE CNC: 0.18 × 3.07  = 0.5526 → 0.55 ; NSE MIS: 0.18 × 23.07 = 4.1526 → 4.15
#       BSE CNC: 0.18 × 3.85  = 0.693  → 0.69 ; BSE MIS: 0.18 × 23.85 = 4.293  → 4.29
EXPECTED_LAKH = {
    (Exchange.NSE, ProductType.CNC, Side.BUY): dict(
        brokerage="0.00",
        stt="100.00",
        exchange_txn="2.97",
        sebi="0.10",
        stamp_duty="15.00",
        gst="0.55",
        total="118.62",
    ),
    (Exchange.NSE, ProductType.CNC, Side.SELL): dict(
        brokerage="0.00",
        stt="100.00",
        exchange_txn="2.97",
        sebi="0.10",
        stamp_duty="0.00",
        gst="0.55",
        total="103.62",
    ),
    (Exchange.NSE, ProductType.MIS, Side.BUY): dict(
        brokerage="20.00",
        stt="0.00",
        exchange_txn="2.97",
        sebi="0.10",
        stamp_duty="3.00",
        gst="4.15",
        total="30.22",
    ),
    (Exchange.NSE, ProductType.MIS, Side.SELL): dict(
        brokerage="20.00",
        stt="25.00",
        exchange_txn="2.97",
        sebi="0.10",
        stamp_duty="0.00",
        gst="4.15",
        total="52.22",
    ),
    (Exchange.BSE, ProductType.CNC, Side.BUY): dict(
        brokerage="0.00",
        stt="100.00",
        exchange_txn="3.75",
        sebi="0.10",
        stamp_duty="15.00",
        gst="0.69",
        total="119.54",
    ),
    (Exchange.BSE, ProductType.CNC, Side.SELL): dict(
        brokerage="0.00",
        stt="100.00",
        exchange_txn="3.75",
        sebi="0.10",
        stamp_duty="0.00",
        gst="0.69",
        total="104.54",
    ),
    (Exchange.BSE, ProductType.MIS, Side.BUY): dict(
        brokerage="20.00",
        stt="0.00",
        exchange_txn="3.75",
        sebi="0.10",
        stamp_duty="3.00",
        gst="4.29",
        total="31.14",
    ),
    (Exchange.BSE, ProductType.MIS, Side.SELL): dict(
        brokerage="20.00",
        stt="25.00",
        exchange_txn="3.75",
        sebi="0.10",
        stamp_duty="0.00",
        gst="4.29",
        total="53.14",
    ),
}


@pytest.mark.parametrize("key", sorted(EXPECTED_LAKH, key=lambda k: (k[0].value, k[1].value, k[2].value)))
def test_exact_paise_on_one_lakh_turnover(key) -> None:
    exchange, product, side = key
    exp = EXPECTED_LAKH[key]
    cb = compute_charges(side=side, product=product, exchange=exchange, quantity=100, price=D("1000"))
    assert cb.turnover == LAKH
    assert cb.brokerage == D(exp["brokerage"])
    assert cb.stt == D(exp["stt"])
    assert cb.exchange_txn == D(exp["exchange_txn"])
    assert cb.sebi == D(exp["sebi"])
    assert cb.stamp_duty == D(exp["stamp_duty"])
    assert cb.gst == D(exp["gst"])
    assert cb.dp_charge == D("0.00")
    assert cb.total == D(exp["total"])
    # as_dict mirrors the breakdown as strings
    d = cb.as_dict()
    assert d["total"] == exp["total"]
    assert d["turnover"] == "100000.00"
    assert set(d) == {
        "turnover",
        "brokerage",
        "stt",
        "exchange_txn",
        "sebi",
        "stamp_duty",
        "gst",
        "dp_charge",
        "total",
    }
    assert all(isinstance(v, str) for v in d.values())


def test_cnc_sell_with_dp_charge_on_one_lakh() -> None:
    cb = compute_charges(
        side=Side.SELL,
        product=ProductType.CNC,
        exchange=Exchange.NSE,
        quantity=100,
        price=D("1000"),
        apply_dp_charge=True,
    )
    assert cb.dp_charge == D("15.34")
    assert cb.total == D("103.62") + D("15.34") == D("118.96")


# ---- brokerage ---------------------------------------------------------------------------


def test_intraday_brokerage_is_pct_below_cap() -> None:
    # ₹1,000 turnover → 0.03% = ₹0.30 < ₹20
    cb = compute_charges(
        side=Side.BUY, product=ProductType.MIS, exchange=Exchange.NSE, quantity=10, price=D("100")
    )
    assert cb.turnover == D("1000.00")
    assert cb.brokerage == D("0.30")
    # ₹50,000 → ₹15.00, still below the cap
    cb = compute_charges(
        side=Side.SELL, product=ProductType.MIS, exchange=Exchange.NSE, quantity=100, price=D("500")
    )
    assert cb.brokerage == D("15.00")


def test_intraday_brokerage_capped_at_20() -> None:
    # ₹66,666.67 turnover is the break-even; anything above is capped at ₹20
    cb = compute_charges(
        side=Side.BUY, product=ProductType.MIS, exchange=Exchange.NSE, quantity=1, price=D("66700")
    )
    assert cb.brokerage == D("20.00")
    cb = compute_charges(
        side=Side.BUY, product=ProductType.MIS, exchange=Exchange.NSE, quantity=1000, price=D("10000")
    )
    assert cb.brokerage == D("20.00")
    # just under: 66,600 × 0.0003 = 19.98
    cb = compute_charges(
        side=Side.BUY, product=ProductType.MIS, exchange=Exchange.NSE, quantity=1, price=D("66600")
    )
    assert cb.brokerage == D("19.98")


def test_delivery_brokerage_is_zero_regardless_of_size() -> None:
    for qty, price in ((1, D("1")), (100, D("1000")), (10000, D("12500"))):
        for side in Side:
            cb = compute_charges(
                side=side, product=ProductType.CNC, exchange=Exchange.NSE, quantity=qty, price=price
            )
            assert cb.brokerage == D("0.00")


# ---- STT / stamp side rules ---------------------------------------------------------------


def test_stt_intraday_only_on_sell_and_delivery_both_sides() -> None:
    buy = compute_charges(
        side=Side.BUY, product=ProductType.MIS, exchange=Exchange.NSE, quantity=100, price=D("1000")
    )
    sell = compute_charges(
        side=Side.SELL, product=ProductType.MIS, exchange=Exchange.NSE, quantity=100, price=D("1000")
    )
    assert buy.stt == D("0.00") and sell.stt == D("25.00")
    buy = compute_charges(
        side=Side.BUY, product=ProductType.CNC, exchange=Exchange.NSE, quantity=100, price=D("1000")
    )
    sell = compute_charges(
        side=Side.SELL, product=ProductType.CNC, exchange=Exchange.NSE, quantity=100, price=D("1000")
    )
    assert buy.stt == sell.stt == D("100.00")


def test_stamp_duty_buy_side_only() -> None:
    for product in ProductType:
        for exchange in Exchange:
            sell = compute_charges(
                side=Side.SELL, product=product, exchange=exchange, quantity=100, price=D("1000")
            )
            assert sell.stamp_duty == D("0.00")
            buy = compute_charges(
                side=Side.BUY, product=product, exchange=exchange, quantity=100, price=D("1000")
            )
            assert buy.stamp_duty == (D("15.00") if product == ProductType.CNC else D("3.00"))


# ---- DP charge ----------------------------------------------------------------------------


@pytest.mark.parametrize("product", list(ProductType))
@pytest.mark.parametrize("side", list(Side))
@pytest.mark.parametrize("apply", [True, False])
def test_dp_charge_only_on_cnc_sell_when_requested(product: ProductType, side: Side, apply: bool) -> None:
    cb = compute_charges(
        side=side, product=product, exchange=Exchange.NSE, quantity=5, price=D("200"), apply_dp_charge=apply
    )
    expected = D("15.34") if (apply and product == ProductType.CNC and side == Side.SELL) else D("0.00")
    assert cb.dp_charge == expected
    assert (
        cb.total == cb.brokerage + cb.stt + cb.exchange_txn + cb.sebi + cb.stamp_duty + cb.gst + cb.dp_charge
    )


def test_dp_charge_is_flat_not_proportional() -> None:
    small = compute_charges(
        side=Side.SELL,
        product=ProductType.CNC,
        exchange=Exchange.BSE,
        quantity=1,
        price=D("10"),
        apply_dp_charge=True,
    )
    big = compute_charges(
        side=Side.SELL,
        product=ProductType.CNC,
        exchange=Exchange.BSE,
        quantity=1000,
        price=D("5000"),
        apply_dp_charge=True,
    )
    assert small.dp_charge == big.dp_charge == D("15.34")
    # DP charge is not part of the GST base
    assert small.gst == to_paise((small.brokerage + small.exchange_txn + small.sebi) * D("0.18"))


# ---- GST base -----------------------------------------------------------------------------


@pytest.mark.parametrize("exchange", list(Exchange))
@pytest.mark.parametrize("product", list(ProductType))
@pytest.mark.parametrize("side", list(Side))
def test_gst_base_excludes_stt_stamp_and_dp(exchange: Exchange, product: ProductType, side: Side) -> None:
    cb = compute_charges(
        side=side, product=product, exchange=exchange, quantity=100, price=D("1000"), apply_dp_charge=True
    )
    base = cb.brokerage + cb.exchange_txn + cb.sebi
    assert cb.gst == to_paise(base * D("0.18"))
    # and demonstrably NOT 18% of the base including STT/stamp/DP (which would be far larger)
    wrong = to_paise((base + cb.stt + cb.stamp_duty + cb.dp_charge) * D("0.18"))
    if cb.stt + cb.stamp_duty + cb.dp_charge > 0:
        assert cb.gst != wrong


def test_gst_on_delivery_is_only_on_exchange_and_sebi() -> None:
    cb = compute_charges(
        side=Side.BUY, product=ProductType.CNC, exchange=Exchange.NSE, quantity=100, price=D("1000")
    )
    # 18% of (2.97 + 0.10) = 0.5526 → 0.55, i.e. rounding is applied once at the end
    assert cb.gst == D("0.55")
    assert cb.brokerage == 0
    assert cb.gst == to_paise((cb.exchange_txn + cb.sebi) * D("0.18"))


# ---- totals and rounding -------------------------------------------------------------------


@pytest.mark.parametrize("exchange", list(Exchange))
@pytest.mark.parametrize("product", list(ProductType))
@pytest.mark.parametrize("side", list(Side))
@pytest.mark.parametrize(
    "qty, price", [(7, D("123.45")), (1, D("0.05")), (333, D("999.95")), (13, D("12500"))]
)
def test_total_is_sum_of_parts_and_everything_is_paise(
    exchange: Exchange, product: ProductType, side: Side, qty: int, price: Decimal
) -> None:
    cb = compute_charges(
        side=side, product=product, exchange=exchange, quantity=qty, price=price, apply_dp_charge=True
    )
    parts = [
        cb.turnover,
        cb.brokerage,
        cb.stt,
        cb.exchange_txn,
        cb.sebi,
        cb.stamp_duty,
        cb.gst,
        cb.dp_charge,
        cb.total,
    ]
    for p in parts:
        assert isinstance(p, Decimal)
        assert p == p.quantize(D("0.01")), f"{p} not rounded to paise"
        assert p >= 0
    assert cb.turnover == to_paise(price * qty)
    assert (
        cb.total == cb.brokerage + cb.stt + cb.exchange_txn + cb.sebi + cb.stamp_duty + cb.gst + cb.dp_charge
    )
    assert D(cb.as_dict()["total"]) == cb.total


def test_rounding_is_half_up_to_paise() -> None:
    assert to_paise(D("0.005")) == D("0.01")
    assert to_paise(D("0.004999")) == D("0.00")
    assert to_paise(D("2.345")) == D("2.35")
    assert to_paise(D("2.344")) == D("2.34")
    assert to_paise(D("100")) == D("100.00")
    # STT on ₹864.15 (7 × 123.45) delivery = 0.86415 → 0.86 ; exch NSE 0.02566 → 0.03
    cb = compute_charges(
        side=Side.BUY, product=ProductType.CNC, exchange=Exchange.NSE, quantity=7, price=D("123.45")
    )
    assert cb.turnover == D("864.15")
    assert cb.stt == D("0.86")
    assert cb.exchange_txn == D("0.03")
    assert cb.sebi == D("0.00")  # 0.000864 → 0.00
    assert cb.stamp_duty == D("0.13")  # 0.1296225 → 0.13
    assert cb.gst == D("0.01")  # 0.18 × 0.03 = 0.0054 → 0.01
    assert cb.total == D("1.03")


def test_tiny_order_has_near_zero_charges() -> None:
    cb = compute_charges(
        side=Side.BUY, product=ProductType.MIS, exchange=Exchange.NSE, quantity=1, price=D("0.05")
    )
    assert cb.turnover == D("0.05")
    assert cb.total == D("0.00")


def test_charges_scale_linearly_below_brokerage_cap() -> None:
    # Delivery charges are all proportional (no cap, no flat fee) so 10× quantity ≈ 10× charges
    # up to rounding on each component.
    one = compute_charges(
        side=Side.BUY, product=ProductType.CNC, exchange=Exchange.NSE, quantity=100, price=D("1000")
    )
    ten = compute_charges(
        side=Side.BUY, product=ProductType.CNC, exchange=Exchange.NSE, quantity=1000, price=D("1000")
    )
    assert ten.turnover == one.turnover * 10
    assert ten.stt == one.stt * 10
    assert ten.exchange_txn == D("29.70") and ten.sebi == D("1.00") and ten.stamp_duty == D("150.00")
    assert ten.gst == D("5.53")  # 0.18 × 30.70 = 5.526 → 5.53
    assert ten.total == D("1186.23")


# ---- schedule overrides ---------------------------------------------------------------------


def test_custom_schedule_is_honoured() -> None:
    sched = ChargeSchedule(
        brokerage_delivery_flat=D("10"),
        brokerage_intraday_cap=D("5"),
        brokerage_intraday_pct=D("0.001"),
        stt_delivery_pct=D("0"),
        stt_intraday_sell_pct=D("0"),
        exchange_txn_pct={Exchange.NSE: D("0"), Exchange.BSE: D("0.01")},
        sebi_pct=D("0"),
        stamp_delivery_buy_pct=D("0"),
        stamp_intraday_buy_pct=D("0"),
        gst_pct=D("0.5"),
        dp_charge_per_scrip_day=D("1"),
    )
    cnc = compute_charges(
        side=Side.SELL,
        product=ProductType.CNC,
        exchange=Exchange.NSE,
        quantity=100,
        price=D("100"),
        schedule=sched,
        apply_dp_charge=True,
    )
    assert cnc.brokerage == D("10.00") and cnc.gst == D("5.00") and cnc.dp_charge == D("1.00")
    assert cnc.stt == cnc.exchange_txn == cnc.sebi == cnc.stamp_duty == D("0.00")
    assert cnc.total == D("16.00")

    mis = compute_charges(
        side=Side.BUY,
        product=ProductType.MIS,
        exchange=Exchange.BSE,
        quantity=100,
        price=D("100"),
        schedule=sched,
    )
    assert mis.brokerage == D("5.00")  # 0.1% of 10,000 = 10 → capped at 5
    assert mis.exchange_txn == D("100.00")  # 1% of 10,000
    assert mis.gst == D("52.50")  # 50% × (5 + 100)
    assert mis.total == D("157.50")


def test_breakdown_is_immutable() -> None:
    cb = compute_charges(
        side=Side.BUY, product=ProductType.CNC, exchange=Exchange.NSE, quantity=1, price=D("100")
    )
    assert isinstance(cb, ChargeBreakdown)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cb.stt = D("0")  # type: ignore[misc]
