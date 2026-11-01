"""Bounded exact decimal arithmetic. Never accept binary floats as money."""
from decimal import Decimal, Inexact, localcontext, ROUND_FLOOR
import re

ZERO = Decimal("0")
ONE = Decimal("1")
PATTERN = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?\Z")


def decimal(value: str | Decimal | int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, Decimal, int)):
        raise ValueError("decimal requires a decimal string, Decimal, or integer")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite decimal")
        digits = value.as_tuple()
        if digits.exponent < -8 or value.adjusted() >= 18:
            raise ValueError("decimal exceeds precision bounds")
    raw = format(value, "f") if isinstance(value, Decimal) else str(value)
    if len(raw) > 28 or not PATTERN.fullmatch(raw):
        raise ValueError("malformed or oversized decimal")
    unsigned = raw.lstrip("-")
    whole, _, fraction = unsigned.partition(".")
    if len(whole) > 18 or len(fraction) > 8:
        raise ValueError("decimal exceeds 18 integer / 8 fractional digits")
    return Decimal(raw)


def positive(value: str | Decimal | int) -> Decimal:
    result = decimal(value)
    if result <= 0:
        raise ValueError("must be positive")
    return result


def nonnegative(value: str | Decimal | int) -> Decimal:
    result = decimal(value)
    if result < 0:
        raise ValueError("must be nonnegative")
    return result


def add(*values: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        ctx.traps[Inexact] = True
        return sum(values, ZERO)


def mul(*values: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        ctx.traps[Inexact] = True
        result = ONE
        for value in values:
            result *= value
        return result


def floor_quantity(notional: Decimal, price: Decimal, increment: Decimal) -> Decimal:
    nonnegative(notional)
    positive(price)
    positive(increment)
    with localcontext() as ctx:
        ctx.prec = 80
        units = (notional / (price * increment)).to_integral_value(rounding=ROUND_FLOOR)
        return units * increment


def aligned(value: Decimal, increment: Decimal) -> bool:
    with localcontext() as ctx:
        ctx.prec = 80
        return value % positive(increment) == 0
