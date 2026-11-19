"""Public entry point for cross-version repository comparison.

Takes two already-built repositories as read-only inputs and produces an
independent change dataset.  Neither input repository is ever opened for
writing or merged; a failed run leaves no directory that can be mistaken for a
complete result.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from .dataset import (
    DATASET_SCHEMA_VERSION,
    ChangeDataset,
    ChangeDatasetWriter,
    make_staging,
    publish_dataset,
)
from .engine import compare_repositories
from .identity import IdentityMapping, IdentityMappingError, load_identity_mapping
from .snapshot import InputModifiedError, RepoSnapShot, SnapshotError

__all__ = [
    "ChangeDataset",
    "ComparisonError",
    "IdentityMapping",
    "IdentityMappingError",
    "InputModifiedError",
    "RepoSnapShot",
    "SnapshotError",
    "build_diff",
    "load_identity_mapping",
]


class ComparisonError(RuntimeError):
    """Raised when a comparison cannot produce a trustworthy dataset."""


def build_diff(
    left_repository: str,
    right_repository: str,
    dest: str,
    *,
    identity_mapping_path: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Compare two repositories and write a change dataset at ``dest``.

    Returns the dataset manifest.  Raises :class:`ComparisonError`,
    :class:`IdentityMappingError` or :class:`InputModifiedError` without
    publishing anything on failure.
    """

    dest = os.path.abspath(dest)
    parent = os.path.dirname(dest) or "."
    os.makedirs(parent, exist_ok=True)
    staging = make_staging(parent)
    left: RepoSnapShot | None = None
    right: RepoSnapShot | None = None
    writer: ChangeDatasetWriter | None = None
    try:
        left = RepoSnapShot(left_repository, label="left")
        right = RepoSnapShot(right_repository, label="right")
        if identity_mapping_path:
            mapping = load_identity_mapping(identity_mapping_path, left, right)
        else:
            from .identity import empty_mapping

            mapping = empty_mapping()

        writer = ChangeDatasetWriter(staging)
        writer.write_side_sources("left", left.sources())
        writer.write_side_sources("right", right.sources())
        writer.write_mappings(mapping.as_rows())

        run_info = compare_repositories(left, right, writer, mapping)
        # Final guard: the inputs must still be the exact snapshots we started
        # with.  assert_unchanged raises InputModifiedError on mismatch.
        left.assert_unchanged()
        right.assert_unchanged()

        manifest = {
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "label": label,
            "left": {
                "repository": os.path.abspath(left_repository),
                "fingerprint": left.start_fingerprint,
                "sources": [s.as_json() for s in left.sources()],
            },
            "right": {
                "repository": os.path.abspath(right_repository),
                "fingerprint": right.start_fingerprint,
                "sources": [s.as_json() for s in right.sources()],
            },
            "identity_mapping_file": identity_mapping_path,
            "identity_mapping_rows": len(mapping.pairs),
            "run": run_info,
            "determinism_note": (
                "records are keyed by content hash and ordered by a stable "
                "emission order; the same snapshots and mapping reproduce the "
                "same business records"
            ),
        }
        writer.finish(manifest=manifest)
        writer = None
        left.close()
        right.close()
        left = right = None
        publish_dataset(staging, dest)
        staging = ""
        with ChangeDataset(dest) as dataset:
            return dataset.manifest()
    finally:
        if writer is not None:
            writer.abort()
        if left is not None:
            left.close()
        if right is not None:
            right.close()
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
