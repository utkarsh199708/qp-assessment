"""Statutory and brokerage charges for Indian cash-equity trades.

Rates mirror a discount broker (Zerodha-style) as of 2026. Every rate is configurable via
:class:`ChargeSchedule`; the defaults are:

============================  =====================  =====================
Charge                        Delivery (CNC)         Intraday (MIS)
============================  =====================  =====================
Brokerage                     ₹0                     min(₹20, 0.03% turnover) per order
STT                           0.1% buy & sell        0.025% sell only
Exchange txn charges (NSE)    0.00297% of turnover   0.00297% of turnover
Exchange txn charges (BSE)    0.00375% of turnover   0.00375% of turnover
SEBI turnover fee             ₹10 / crore            ₹10 / crore
Stamp duty                    0.015% buy only        0.003% buy only
GST                           18% × (brokerage + exchange txn + SEBI)
DP charge (CDSL, per scrip    ₹15.34 on sell         –
per day, delivery sell only)
============================  =====================  =====================

All money is :class:`~decimal.Decimal` rounded to paise (2 dp, half-up), per order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

PAISE = Decimal("0.01")


def to_paise(x: Decimal) -> Decimal:
    return x.quantize(PAISE, rounding=ROUND_HALF_UP)


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"


class ProductType(StrEnum):
    CNC = "CNC"  # Cash & Carry – delivery, held overnight, no short selling
    MIS = "MIS"  # Margin Intraday Square-off – auto squared-off at 15:20 IST, shorting allowed


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class ChargeSchedule:
    # brokerage
    brokerage_delivery_flat: Decimal = Decimal("0")
    brokerage_intraday_cap: Decimal = Decimal("20")
    brokerage_intraday_pct: Decimal = Decimal("0.0003")  # 0.03%
    # STT
    stt_delivery_pct: Decimal = Decimal("0.001")  # 0.1% both sides
    stt_intraday_sell_pct: Decimal = Decimal("0.00025")  # 0.025% sell side
    # exchange transaction charges
    exchange_txn_pct: dict[Exchange, Decimal] = field(
        default_factory=lambda: {Exchange.NSE: Decimal("0.0000297"), Exchange.BSE: Decimal("0.0000375")}
    )
    # SEBI turnover fee ₹10 per crore
    sebi_pct: Decimal = Decimal("0.000001")
    # stamp duty (buy side only)
    stamp_delivery_buy_pct: Decimal = Decimal("0.00015")  # 0.015%
    stamp_intraday_buy_pct: Decimal = Decimal("0.00003")  # 0.003%
    # GST on brokerage + exchange txn + SEBI
    gst_pct: Decimal = Decimal("0.18")
    # DP charges on delivery sell, once per scrip per day
    dp_charge_per_scrip_day: Decimal = Decimal("15.34")


@dataclass(frozen=True)
class ChargeBreakdown:
    turnover: Decimal
    brokerage: Decimal
    stt: Decimal
    exchange_txn: Decimal
    sebi: Decimal
    stamp_duty: Decimal
    gst: Decimal
    dp_charge: Decimal

    @property
    def total(self) -> Decimal:
        return to_paise(
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi
            + self.stamp_duty
            + self.gst
            + self.dp_charge
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "turnover": str(self.turnover),
            "brokerage": str(self.brokerage),
            "stt": str(self.stt),
            "exchange_txn": str(self.exchange_txn),
            "sebi": str(self.sebi),
            "stamp_duty": str(self.stamp_duty),
            "gst": str(self.gst),
            "dp_charge": str(self.dp_charge),
            "total": str(self.total),
        }


def compute_charges(
    *,
    side: Side,
    product: ProductType,
    exchange: Exchange,
    quantity: int,
    price: Decimal,
    schedule: ChargeSchedule | None = None,
    apply_dp_charge: bool = False,
) -> ChargeBreakdown:
    """Compute all charges for one executed order.

    ``apply_dp_charge`` should be True only for the first CNC sell of a scrip on a given day
    (the caller knows the day's history; this function is pure).
    """
    s = schedule or ChargeSchedule()
    qty = Decimal(quantity)
    turnover = to_paise(price * qty)
    intraday = product == ProductType.MIS

    if intraday:
        brokerage = min(s.brokerage_intraday_cap, turnover * s.brokerage_intraday_pct)
    else:
        brokerage = s.brokerage_delivery_flat
    brokerage = to_paise(brokerage)

    if intraday:
        stt = turnover * s.stt_intraday_sell_pct if side == Side.SELL else Decimal(0)
    else:
        stt = turnover * s.stt_delivery_pct
    stt = to_paise(stt)

    exchange_txn = to_paise(turnover * s.exchange_txn_pct[exchange])
    sebi = to_paise(turnover * s.sebi_pct)

    if side == Side.BUY:
        stamp = turnover * (s.stamp_intraday_buy_pct if intraday else s.stamp_delivery_buy_pct)
    else:
        stamp = Decimal(0)
    stamp = to_paise(stamp)

    gst = to_paise((brokerage + exchange_txn + sebi) * s.gst_pct)

    dp = (
        to_paise(s.dp_charge_per_scrip_day)
        if (apply_dp_charge and not intraday and side == Side.SELL)
        else Decimal("0.00")
    )

    return ChargeBreakdown(
        turnover=turnover,
        brokerage=brokerage,
        stt=stt,
        exchange_txn=exchange_txn,
        sebi=sebi,
        stamp_duty=stamp,
        gst=gst,
        dp_charge=dp,
    )
