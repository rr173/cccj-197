"""Exception types for offline handover release verification.

Every error raised here rejects the whole verification run.  The caller
(:func:`ifc_handover.release.verify.build_release_verification`) never
publishes a partial dataset: a previously completed report at the destination
is preserved untouched.
"""

from __future__ import annotations


class ReleaseVerificationError(RuntimeError):
    """Base error: the run is rejected and no release dataset is published."""


class RulesError(ReleaseVerificationError):
    """The acceptance rules file is structurally invalid."""


class CheckpointError(ReleaseVerificationError):
    """The checkpoint set file is structurally invalid."""


class LedgerError(ReleaseVerificationError):
    """The signoff revision ledger is structurally invalid or inconsistent.

    ``line_no`` is the 1-based line number in the JSONL ledger that caused
    the rejection, so the auditor can locate the offending revision.
    """

    def __init__(self, message: str, line_no: int | None = None):
        self.line_no = line_no
        if line_no is not None:
            message = f"ledger line {line_no}: {message}"
        super().__init__(message)
