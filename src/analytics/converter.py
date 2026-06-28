from __future__ import annotations

from fractions import Fraction
from typing import NamedTuple, Union

Number = Union[int, float, Fraction]

U64_MIN: int = 0
U64_MAX: int = 18_446_744_073_709_551_615


class ReducedFraction(NamedTuple):
    numerator: int
    denominator: int


class FractionBoundsError(ValueError):
    """Raised when a reduced fraction component exceeds u64 bounds."""


def _validate_u64(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{label} must be an int, got {type(value).__name__}.")
    if value < U64_MIN or value > U64_MAX:
        raise FractionBoundsError(
            f"{label} {value} is outside unsigned 64-bit range "
            f"[{U64_MIN}, {U64_MAX}]."
        )


def reduce_fraction(numerator: int, denominator: int) -> ReducedFraction:
    """Reduce a fraction to lowest terms using exact rational arithmetic.

    Both *numerator* and *denominator* are validated against u64 bounds before
    reduction.  The reduced result is also checked so callers can safely
    serialise the components into on-chain Soroban payloads.

    Parameters
    ----------
    numerator:
        The fraction numerator (must be ``int``, within ``[0, U64_MAX]``).
    denominator:
        The fraction denominator (must be ``int``, within ``[1, U64_MAX]``).

    Returns
    -------
    ReducedFraction
        A named tuple ``(numerator, denominator)`` in lowest terms.

    Raises
    ------
    FractionBoundsError
        If either operand or the reduced components exceed u64 bounds.
    TypeError
        If either operand is a ``bool``.
    """
    if isinstance(numerator, bool) or isinstance(denominator, bool):
        raise TypeError("Numerator and denominator must be numeric, not bool.")

    _validate_u64(numerator, "Numerator")
    _validate_u64(denominator, "Denominator")

    if denominator == 0:
        raise FractionBoundsError("Denominator must not be zero.")

    fraction = Fraction(numerator, denominator)
    reduced_num = fraction.numerator
    reduced_den = fraction.denominator

    # Reduction never increases magnitude beyond min(numerator, denominator),
    # but we validate explicitly so callers can trust the output.
    _validate_u64(reduced_num, "Reduced numerator")
    _validate_u64(reduced_den, "Reduced denominator")

    return ReducedFraction(numerator=reduced_num, denominator=reduced_den)


def decimal_to_fraction(
    value: Number | str,
    max_denominator: int = U64_MAX,
) -> ReducedFraction:
    """Convert a decimal value to an exact reduced fraction within u64 bounds.

    Internally uses ``Fraction.limit_denominator`` to cap the denominator at
    *max_denominator*, which defaults to ``U64_MAX`` so the result is
    guaranteed to fit inside a standard u64 integer.

    Parameters
    ----------
    value:
        The decimal value to convert (``int``, ``float``, ``Fraction``, or
        ``str``).
    max_denominator:
        Maximum allowed denominator for the reduced result.  Defaults to
        ``U64_MAX``.

    Returns
    -------
    ReducedFraction
        The best approximation of *value* as a reduced fraction whose
        denominator does not exceed *max_denominator*.

    Raises
    ------
    FractionBoundsError
        If the reduced components exceed the u64 range.
    """
    fraction = Fraction(value).limit_denominator(max_denominator)
    reduced_num = fraction.numerator
    reduced_den = fraction.denominator

    _validate_u64(reduced_num, "Reduced numerator")
    _validate_u64(reduced_den, "Reduced denominator")

    return ReducedFraction(numerator=reduced_num, denominator=reduced_den)


__all__ = [
    "U64_MIN",
    "U64_MAX",
    "ReducedFraction",
    "FractionBoundsError",
    "reduce_fraction",
    "decimal_to_fraction",
]
