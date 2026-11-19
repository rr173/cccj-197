"""Explicit one-to-one identity correspondence between two repositories.

The automatic cross-version identity is the IfcRoot GlobalId.  When an object
really is the same business object but its GlobalId changed, the reviewer may
supply an explicit correspondence table.  It is the *only* accepted override:
names, entity types and spatial locations are never used to guess identity.

The table is validated structurally before any comparison runs:

* every mapping must resolve to exactly one endpoint on each side,
* an endpoint may appear in at most one mapping (no many-to-one),
* duplicated mapping rows are rejected.

A structurally invalid table aborts the whole comparison with
:class:`IdentityMappingError`; no change dataset is written.  A mapping whose
endpoint exists but is itself unusable (for example a duplicate GlobalId) is a
*local* incomparable result and is reported with evidence by the engine rather
than rejected here, because the evidence exists inside the repositories.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from ..keys import canonical_json
from .snapshot import RepoSnapShot


class IdentityMappingError(ValueError):
    """The supplied correspondence table is not a valid one-to-one mapping."""


@dataclass(frozen=True)
class IdentityPair:
    """One validated cross-version identity correspondence."""

    left_source_id: str
    left_global_id: str
    right_source_id: str
    right_global_id: str
    rationale: str
    index: int  # 0-based position in the supplied file, kept for evidence

    def as_json(self) -> dict[str, Any]:
        return {
            "left": {"source_id": self.left_source_id, "global_id": self.left_global_id},
            "right": {"source_id": self.right_source_id, "global_id": self.right_global_id},
            "rationale": self.rationale,
            "index": self.index,
        }


@dataclass
class IdentityMapping:
    pairs: tuple[IdentityPair, ...]
    # Lookup tables keyed by (source_id, global_id).
    left_index: dict[tuple[str, str], IdentityPair]
    right_index: dict[tuple[str, str], IdentityPair]

    def left_for(self, source_id: str, global_id: str) -> IdentityPair | None:
        return self.left_index.get((source_id, global_id))

    def right_for(self, source_id: str, global_id: str) -> IdentityPair | None:
        return self.right_index.get((source_id, global_id))

    def as_rows(self) -> list[dict[str, Any]]:
        return [pair.as_json() for pair in self._ordered()]

    def _ordered(self) -> list[IdentityPair]:
        return sorted(self.pairs, key=lambda p: p.index)


def load_identity_mapping(
    path: str, left: RepoSnapShot, right: RepoSnapShot
) -> IdentityMapping:
    """Load and fully validate a correspondence JSON file."""

    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "mappings" in data:
        raw_entries = data["mappings"]
    elif isinstance(data, list):
        raw_entries = data
    else:
        raise IdentityMappingError(
            "identity mapping must be a list or an object with a 'mappings' list"
        )
    if not isinstance(raw_entries, list):
        raise IdentityMappingError("identity mapping 'mappings' must be a list")

    pairs: list[IdentityPair] = []
    seen_rows: set[str] = set()
    left_seen: dict[tuple[str, str], int] = {}
    right_seen: dict[tuple[str, str], int] = {}
    errors: list[str] = []

    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            errors.append(f"entry #{index} is not an object")
            continue
        try:
            left_ref = entry["left"]
            right_ref = entry["right"]
            left_source = str(left_ref["source_id"])
            left_gid = str(left_ref["global_id"])
            right_source = str(right_ref["source_id"])
            right_gid = str(right_ref["global_id"])
        except (KeyError, TypeError):
            errors.append(
                f"entry #{index} must contain 'left' and 'right' objects with "
                "source_id and global_id"
            )
            continue
        rationale = str(entry.get("rationale", "")).strip()
        if not rationale:
            errors.append(f"entry #{index} is missing a non-empty 'rationale'")
            continue

        row_token = canonical_json(
            [left_source, left_gid, right_source, right_gid]
        )
        if row_token in seen_rows:
            errors.append(
                f"entry #{index} duplicates an earlier mapping "
                f"({left_source}/{left_gid} -> {right_source}/{right_gid})"
            )
            continue
        seen_rows.add(row_token)

        left_token = (left_source, left_gid)
        right_token = (right_source, right_gid)
        if left_token in left_seen:
            errors.append(
                f"entry #{index} reuses left endpoint {left_source}/{left_gid} "
                f"already mapped by entry #{left_seen[left_token]}; one-to-one required"
            )
            continue
        if right_token in right_seen:
            errors.append(
                f"entry #{index} reuses right endpoint {right_source}/{right_gid} "
                f"already mapped by entry #{right_seen[right_token]}; one-to-one required"
            )
            continue

        missing = _missing_endpoints(left, right, left_token, right_token)
        if missing:
            errors.append(f"entry #{index}: {missing}")
            continue

        pair = IdentityPair(
            left_source, left_gid, right_source, right_gid, rationale, index
        )
        pairs.append(pair)
        left_seen[left_token] = index
        right_seen[right_token] = index

    if errors:
        joined = "\n  ".join(errors)
        raise IdentityMappingError(
            f"identity mapping rejected with {len(errors)} problem(s):\n  {joined}"
        )

    return IdentityMapping(
        pairs=tuple(pairs),
        left_index={(p.left_source_id, p.left_global_id): p for p in pairs},
        right_index={(p.right_source_id, p.right_global_id): p for p in pairs},
    )


def _missing_endpoints(
    left: RepoSnapShot,
    right: RepoSnapShot,
    left_token: tuple[str, str],
    right_token: tuple[str, str],
) -> str | None:
    left_count = _count_global_id(left, *left_token)
    right_count = _count_global_id(right, *right_token)
    messages: list[str] = []
    if left_count == 0:
        messages.append(
            f"left endpoint {left_token[0]}/{left_token[1]} does not exist"
        )
    if right_count == 0:
        messages.append(
            f"right endpoint {right_token[0]}/{right_token[1]} does not exist"
        )
    if messages:
        return "; ".join(messages)
    return None


def _count_global_id(snapshot: RepoSnapShot, source_id: str, global_id: str) -> int:
    row = snapshot.conn.execute(
        "SELECT COUNT(*) c FROM roots WHERE source_id=? AND global_id=? AND key_kind='gid'",
        (source_id, global_id),
    ).fetchone()
    return int(row["c"])


def empty_mapping() -> IdentityMapping:
    return IdentityMapping(pairs=(), left_index={}, right_index={})


def mapping_file_hint(path: str | None) -> str:
    if path is None:
        return ""
    return os.path.abspath(path)
