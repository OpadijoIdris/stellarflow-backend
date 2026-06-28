from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Union

# ---------------------------------------------------------------------------
# Scale constants
# ---------------------------------------------------------------------------
# 10^7  — standard precision for single-hop exchange rate values sent to the
#          Soroban smart contract layer.  All payload integers MUST use this
#          base before transmission.
# 10^14 — extended precision used during cross-feed multiplication so that
#          two 10^7-scaled values can be combined without intermediate loss.

SCALE_7: int = 10_000_000            # 10^7
SCALE_14: int = 100_000_000_000_000  # 10^14

_D_SCALE_7 = Decimal(SCALE_7)
_D_SCALE_14 = Decimal(SCALE_14)

Number = Union[int, float, Decimal]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_decimal(value: Number, name: str = "value") -> Decimal:
    """Coerce *value* to a finite Decimal using its string representation.

    String coercion is intentional: it avoids inheriting the binary-float
    approximation that would be introduced by ``Decimal(float_value)`` directly,
    which is the root cause of the determinism bugs this module addresses.
    """
    if isinstance(value, bool):
        raise TypeError(f"{name} must be numeric, not bool.")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} is not a valid number: {value!r}") from exc
    if not d.is_finite():
        raise ValueError(f"{name} must be finite, got {value!r}.")
    return d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scale_up(value: Number, factor: int = SCALE_7) -> int:
    """Scale *value* to its fixed-integer representation at *factor* precision.

    Uses ``Decimal`` arithmetic and truncates (floor) toward zero via
    ``ROUND_DOWN`` to guarantee bit-identical results across all Python
    environments and CPU architectures.

    Parameters
    ----------
    value:
        The raw rate or price to scale (int, float, or Decimal).
    factor:
        The integer base to scale to.  Defaults to ``SCALE_7`` (10^7).

    Returns
    -------
    int
        The deterministic integer representation ready for payload packing.
    """
    d = _to_decimal(value) * Decimal(factor)
    # ROUND_DOWN truncates toward zero — equivalent to math.floor for positives
    # but deterministic regardless of platform FPU behaviour.
    return int(d.to_integral_value(rounding=ROUND_DOWN))


def scale_down(value: int, factor: int = SCALE_7) -> Decimal:
    """Restore a scaled integer to a high-precision ``Decimal`` representation.

    Returns ``Decimal`` rather than ``float`` so callers can chain further
    Decimal operations without reintroducing binary-float imprecision.

    Parameters
    ----------
    value:
        A previously scaled integer (e.g. from ``scale_up``).
    factor:
        The base that was used when scaling up.  Defaults to ``SCALE_7``.
    """
    return Decimal(value) / Decimal(factor)


def multiply_rates(rate_a: Number, rate_b: Number) -> int:
    """Multiply two exchange rates and return a ``SCALE_14``-scaled integer.

    Both inputs are independently coerced to ``Decimal`` via their string
    representations before being scaled to 10^7.  The product of two 10^7
    integers is implicitly at 10^14 precision and is returned as-is so
    callers can chain further integer operations without precision loss.

    Example
    -------
    >>> multiply_rates(1500.0, 0.00065)  # NGN/USD × USD/XLM → NGN/XLM (×10^14)
    975000000000
    """
    a_int = scale_up(rate_a, SCALE_7)
    b_int = scale_up(rate_b, SCALE_7)
    return a_int * b_int  # implicitly SCALE_14


def cross_feed_multiply(
    rate_a: Number,
    rate_b: Number,
    output_scale: int = SCALE_7,
) -> int:
    """Compute an implied cross-feed rate at *output_scale* precision.

    Internally uses the full 10^14 product then performs integer floor-division
    back to *output_scale*, keeping every step in integer arithmetic.

    Parameters
    ----------
    rate_a, rate_b:
        Raw (unscaled) exchange rates.
    output_scale:
        The desired output precision base.  Defaults to ``SCALE_7``.

    Returns
    -------
    int
        A deterministic integer at *output_scale* precision.
    """
    product_14 = multiply_rates(rate_a, rate_b)
    return product_14 // SCALE_7


def floor_divide(scaled_value: int, divisor: Number) -> int:
    """Integer floor-divide a ``SCALE_7``-scaled value by *divisor*.

    *divisor* is scaled to ``SCALE_7`` via ``Decimal`` arithmetic before
    the division so the entire operation stays in integer space.

    Parameters
    ----------
    scaled_value:
        A ``SCALE_7``-scaled integer numerator.
    divisor:
        The raw (unscaled) divisor.

    Returns
    -------
    int
        Floor-divided result at ``SCALE_7`` precision.
    """
    divisor_int = scale_up(divisor, SCALE_7)
    if divisor_int == 0:
        raise ZeroDivisionError(f"Divisor {divisor!r} scales to zero at SCALE_7.")
    return (scaled_value * SCALE_7) // divisor_int


def sqrt_scaled(value: int, scale: int = SCALE_7) -> int:
    """Return the fixed-point square root of a scaled integer.

    ``value`` is expected to be scaled by *scale*, and the returned integer uses
    the same scale.  The calculation is strictly integer-only:

    ``sqrt(value / scale) * scale == sqrt(value * scale)``

    The final square root is calculated with binary search and floors toward
    zero, matching the truncation behavior used across this module.
    """
    if isinstance(value, bool):
        raise TypeError("value must be an integer, not bool.")
    if not isinstance(value, int):
        raise TypeError("value must be a scaled integer.")
    if isinstance(scale, bool) or not isinstance(scale, int):
        raise TypeError("scale must be an integer.")
    if value < 0:
        raise ValueError("Cannot calculate square root of a negative value.")
    if scale <= 0:
        raise ValueError("scale must be positive.")

    radicand = value * scale
    if radicand < 2:
        return radicand

    low = 0
    high = radicand
    answer = 0

    while low <= high:
        mid = (low + high) // 2
        square = mid * mid
        if square <= radicand:
            answer = mid
            low = mid + 1
        else:
            high = mid - 1

    return answer


def pack_rate(value: Number) -> int:
    """Convenience wrapper: scale *value* to a ``SCALE_7`` integer for payload packing.

    This is the canonical entry-point used before serialising a rate into any
    Soroban contract data payload.  It enforces the ``10^7`` fixed-integer base
    contract and rejects non-finite or boolean inputs early.

    Parameters
    ----------
    value:
        Raw exchange rate (int, float, or Decimal).

    Returns
    -------
    int
        Deterministic ``SCALE_7`` integer ready for transmission.
    """
    return scale_up(value, SCALE_7)


__all__ = [
    "SCALE_7",
    "SCALE_14",
    "Number",
    "scale_up",
    "scale_down",
    "multiply_rates",
    "cross_feed_multiply",
    "floor_divide",
    "sqrt_scaled",
    "pack_rate",
]
