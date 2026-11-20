"""Offline handover release verification.

Reads one ingested object repository, one acceptance rules file, one signoff
revision ledger and one checkpoint set, and answers for every verification
checkpoint whether each zone meets its release conditions.  The result is an
independently re-openable dataset; all inputs stay read-only and a failed run
never replaces a previously completed report.
"""

from .dataset import (
    ANOMALY_AMBIGUOUS_TARGET,
    ANOMALY_INVALID_TARGET,
    ANOMALY_NO_ASSOCIATION,
    CLASS_COUNTED,
    CLASS_EXCLUDED,
    CLASS_PENDING,
    DECISION_EMPTY_SCOPE,
    DECISION_HELD,
    DECISION_RELEASED,
    DECISION_SCOPE_UNVERIFIED,
    DatasetError,
    ReleaseDataset,
    SCOPE_CONFIRMED,
    SCOPE_UNVERIFIED,
    STATUS_BLOCKED,
    STATUS_READY,
    STATUS_UNDETERMINED,
    is_complete_dataset,
)
from .errors import (
    CheckpointError,
    LedgerError,
    ReleaseVerificationError,
    RulesError,
)
from .verify import build_release_verification

__all__ = [
    "ANOMALY_AMBIGUOUS_TARGET",
    "ANOMALY_INVALID_TARGET",
    "ANOMALY_NO_ASSOCIATION",
    "CLASS_COUNTED",
    "CLASS_EXCLUDED",
    "CLASS_PENDING",
    "CheckpointError",
    "DECISION_EMPTY_SCOPE",
    "DECISION_HELD",
    "DECISION_RELEASED",
    "DECISION_SCOPE_UNVERIFIED",
    "DatasetError",
    "LedgerError",
    "ReleaseDataset",
    "ReleaseVerificationError",
    "RulesError",
    "SCOPE_CONFIRMED",
    "SCOPE_UNVERIFIED",
    "STATUS_BLOCKED",
    "STATUS_READY",
    "STATUS_UNDETERMINED",
    "build_release_verification",
    "is_complete_dataset",
]
