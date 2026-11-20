"""Spatial quantity take-off from an offline IFC object repository.

The subpackage reads one built repository strictly read-only plus a UTF-8
JSON rules file, and emits an independent, independently queryable take-off
dataset.  It never modifies the repository, computes geometry, derives a
quantity from dimensions, or presents the result as a model-version diff or
a handover signoff conclusion.
"""

from .dataset import (
    LINE_CONFLICT,
    LINE_MISSING,
    LINE_SCOPE_UNCERTAIN,
    LINE_UNSUPPORTED,
    LINE_UNIT_UNAVAILABLE,
    LINE_VALID,
    SCOPE_CONFIRMED,
    SCOPE_UNVERIFIED,
    TakeoffDataset,
    TakeoffDatasetError,
    is_complete_dataset,
)
from .errors import RulesError, TakeoffError
from .inputs import (
    SUPPORTED_QUANTITY_TYPES,
    QuantityIdentity,
    TakeoffRule,
    TakeoffRules,
    load_rules,
)
from .verify import InputModifiedError, build_takeoff

__all__ = [
    "InputModifiedError",
    "LINE_CONFLICT",
    "LINE_MISSING",
    "LINE_SCOPE_UNCERTAIN",
    "LINE_UNSUPPORTED",
    "LINE_UNIT_UNAVAILABLE",
    "LINE_VALID",
    "QuantityIdentity",
    "RulesError",
    "SCOPE_CONFIRMED",
    "SCOPE_UNVERIFIED",
    "SUPPORTED_QUANTITY_TYPES",
    "TakeoffDataset",
    "TakeoffDatasetError",
    "TakeoffError",
    "TakeoffRule",
    "TakeoffRules",
    "build_takeoff",
    "is_complete_dataset",
    "load_rules",
]