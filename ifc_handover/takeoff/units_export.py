"""Exact decimal rendering helpers shared by take-off aggregation.

Re-exports the exact rational-to-decimal routine from the core unit layer so
the take-off package has one place to document its numeric guarantees: totals
are :class:`fractions.Fraction` sums and a decimal rendering is produced only
when the expansion terminates; otherwise the exact fraction remains.
"""

from __future__ import annotations

from fractions import Fraction

from ..units import exact_si_decimal


def exact_decimal(value: Fraction) -> tuple[str | None, bool]:
    return exact_si_decimal(value)
