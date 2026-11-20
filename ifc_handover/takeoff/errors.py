"""Exception types for spatial quantity take-off.

Every error raised here rejects the whole take-off run.  The caller never
publishes a partial dataset: a previously completed dataset at the
destination is preserved untouched.
"""

from __future__ import annotations


class TakeoffError(RuntimeError):
    """Base error: the run is rejected and no take-off dataset is published."""


class RulesError(TakeoffError):
    """The take-off rules file is structurally invalid."""
