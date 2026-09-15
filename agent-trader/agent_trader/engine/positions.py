"""Pure position arithmetic (no I/O) so it can be property-tested."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from ..charges import Side

FOUR_DP = Decimal("0.0001")
PAISE = Decimal("0.01")


def apply_fill(
    quantity: int, average_price: Decimal, side: Side, fill_qty: int, price: Decimal
) -> tuple[int, Decimal, Decimal]:
    """Return ``(new_quantity, new_average_price, realised_pnl)`` after a fill.

    ``quantity`` is signed (negative = short). Adding in the same direction re-weights the
    average; reducing books realised P&L on the closed portion; flipping through zero opens
    the remainder at ``price``.
    """
    if fill_qty <= 0:
        raise ValueError("fill_qty must be positive")
    signed = fill_qty if side == Side.BUY else -fill_qty
    if quantity == 0 or (quantity > 0) == (signed > 0):
        total_cost = average_price * abs(quantity) + price * fill_qty
        new_qty = quantity + signed
        new_avg = (total_cost / abs(new_qty)).quantize(FOUR_DP, rounding=ROUND_HALF_UP)
        return new_qty, new_avg, Decimal("0.00")

    closing = min(abs(quantity), fill_qty)
    per_share = (price - average_price) if quantity > 0 else (average_price - price)
    realised = (per_share * closing).quantize(PAISE, rounding=ROUND_HALF_UP)
    new_qty = quantity + signed
    if new_qty == 0:
        new_avg = Decimal("0")
    elif (new_qty > 0) == (signed > 0):  # flipped
        new_avg = price.quantize(FOUR_DP, rounding=ROUND_HALF_UP)
    else:
        new_avg = average_price
    return new_qty, new_avg, realised


def unrealised_pnl(quantity: int, average_price: Decimal, ltp: Decimal) -> Decimal:
    return ((ltp - average_price) * quantity).quantize(PAISE, rounding=ROUND_HALF_UP)
