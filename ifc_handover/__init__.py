"""Public API for the IFC handover offline repository."""

from .diff import (
    CAT_ADDED,
    CAT_INCOMPARABLE,
    CAT_PROVENANCE_CHANGE,
    CAT_REMOVED,
    CAT_SEMANTIC_CHANGE,
    ChangeDataset,
    ComparisonError,
    IdentityMappingError,
    InputModifiedError,
    build_diff,
    load_identity_mapping,
)
from .export import (
    ExportError,
    HandoverPackage,
    build_closure,
    export_subset,
    select_subset,
)
from .ingest import (
    IngestionError,
    SourceIngestor,
    SourceManifestEntry,
    load_manifest,
    sha256_file,
)
from .store import Repository

__all__ = [
    "CAT_ADDED",
    "CAT_INCOMPARABLE",
    "CAT_PROVENANCE_CHANGE",
    "CAT_REMOVED",
    "CAT_SEMANTIC_CHANGE",
    "ChangeDataset",
    "ComparisonError",
    "ExportError",
    "HandoverPackage",
    "IdentityMappingError",
    "IngestionError",
    "InputModifiedError",
    "Repository",
    "SourceIngestor",
    "SourceManifestEntry",
    "build_closure",
    "build_diff",
    "export_subset",
    "load_identity_mapping",
    "load_manifest",
    "select_subset",
    "sha256_file",
]

__version__ = "0.1.0"
