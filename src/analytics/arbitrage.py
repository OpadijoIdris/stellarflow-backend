"""
Fixed-decimal arbitrage calculators for implied currency pairs.

Uses Python's decimal.Decimal for high-precision financial calculations
across multi-hop corridors, eliminating floating-point rounding errors.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import List, Optional

from .checked_math import (
    checked_mul,
    checked_div,
    safe_round,
    MAX_ABS_VALUE,
    MIN_DENOMINATOR,
    CheckedMathError,
    MathOverflowError,
    BoundaryViolationError,
)

ARBITRAGE_PRECISION = Decimal("1e-12")
DISPLAY_PLACES = 6


class ArbitrageError(CheckedMathError):
    """Raised when arbitrage calculation fails constraints."""


def implied_rate(
    base_to_intermediate: float,
    intermediate_to_quote: float,
) -> float:
    """Calculate implied cross-rate: base->intermediate * intermediate->quote."""
    return safe_round(
        checked_mul(base_to_intermediate, intermediate_to_quote),
        DISPLAY_PLACES,
    )


def implied_rate_div(
    base_to_intermediate: float,
    quote_to_intermediate: float,
) -> float:
    """Calculate implied cross-rate: base_to_intermediate / quote_to_intermediate."""
    return safe_round(
        checked_div(base_to_intermediate, quote_to_intermediate),
        DISPLAY_PLACES,
    )


def arbitrage_spread(
    implied: float,
    direct: float,
) -> dict:
    """Calculate arbitrage spread between implied and direct rates."""
    imp = Decimal(str(implied))
    dire = Decimal(str(direct))
    if dire == 0:
        return {"implied": implied, "direct": direct, "spread_absolute": 0.0, "spread_percent": 0.0, "is_arbitrage": False}
    spread_abs = abs(imp - dire)
    spread_pct = (spread_abs / dire * Decimal("100")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return {"implied": implied, "direct": direct, "spread_absolute": float(spread_abs), "spread_percent": float(spread_pct), "is_arbitrage": spread_pct > Decimal("1.0")}


def multi_hop_rate(rates: List[float]) -> float:
    """Calculate cumulative rate across multiple hops."""
    from .checked_math import multiply_fractional_metrics
    return safe_round(multiply_fractional_metrics(*rates), DISPLAY_PLACES)


def triangular_arbitrage(rate_ab: float, rate_bc: float, rate_ca: float, threshold_percent: float = 0.5) -> dict:
    """Detect triangular arbitrage opportunity across three currency pairs."""
    product = Decimal(str(rate_ab)) * Decimal(str(rate_bc)) * Decimal(str(rate_ca))
    deviation = abs(product - Decimal("1"))
    deviation_pct = (deviation * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"product": float(product), "deviation_percent": float(deviation_pct), "is_opportunity": deviation_pct > Decimal(str(threshold_percent))}


__all__ = ["implied_rate", "implied_rate_div", "arbitrage_spread", "multi_hop_rate", "triangular_arbitrage", "ArbitrageError"]
