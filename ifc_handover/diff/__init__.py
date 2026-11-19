"""Cross-version comparison of two offline IFC object repositories.

The subpackage reads two built repositories strictly read-only and emits an
independent, independently queryable change dataset.  It never merges the two
stores, overwrites an input repository, computes geometry or judges design
compliance.
"""

from .compare import ComparisonError, build_diff
from .dataset import (
    CAT_ADDED,
    CAT_INCOMPARABLE,
    CAT_PROVENANCE_CHANGE,
    CAT_REMOVED,
    CAT_SEMANTIC_CHANGE,
    ChangeDataset,
)
from .identity import IdentityMappingError, load_identity_mapping
from .snapshot import InputModifiedError, RepoSnapShot, SnapshotError

__all__ = [
    "CAT_ADDED",
    "CAT_INCOMPARABLE",
    "CAT_PROVENANCE_CHANGE",
    "CAT_REMOVED",
    "CAT_SEMANTIC_CHANGE",
    "ChangeDataset",
    "ComparisonError",
    "IdentityMappingError",
    "InputModifiedError",
    "RepoSnapShot",
    "SnapshotError",
    "build_diff",
    "load_identity_mapping",
]
