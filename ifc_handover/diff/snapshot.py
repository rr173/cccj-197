"""Read-only snapshots of the two repositories being compared.

A diff never opens either input repository for writing.  The SQLite files are
attached through ``mode=ro`` URIs and every read goes through one immutable
snapshot fingerprint taken when the comparison starts.  The fingerprint is
recomputed before the output dataset is finalised; if either repository
changed in the meantime the comparison is rejected instead of producing a
mixed-version result.

The fingerprint hashes the durable business tables only (never transient WAL
bookkeeping or SQLite rowids), in a deterministic order, so a copy of a
repository keeps the same fingerprint while unrelated repacking cannot be
mistaken for a content change at the business level.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

# Tables whose contents define the business state of a repository.  Only
# durable, content-defined columns are included; AUTOINCREMENT ids and
# timestamps are deliberately excluded so identical snapshots compare equal.
FINGERPRINT_TABLES: tuple[str, ...] = (
    "meta",
    "sources",
    "roots",
    "nodes",
    "relations",
    "relation_edges",
    "pset_definitions",
    "property_declarations",
    "object_psets",
    "resolved_properties",
    "material_assignments",
    "material_nodes",
    "material_edges",
    "units",
    "project_units",
    "external_references",
    "source_entities",
    "source_headers",
    "failure_evidence",
)


class SnapshotError(RuntimeError):
    """Raised when an input repository cannot be read as a valid snapshot."""


class InputModifiedError(RuntimeError):
    """Raised when an input repository changes while a comparison is running."""


@dataclass(frozen=True)
class SourceRef:
    """One source participating in a side of the comparison."""

    source_id: str
    sha256: str
    status: str

    def as_json(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "sha256": self.sha256, "status": self.status}


class RepoSnapShot:
    """A read-only, fingerprint-protected view over one input repository."""

    def __init__(self, path: str, *, label: str):
        self.path = os.path.abspath(path)
        self.label = label
        if not os.path.isfile(self.path):
            raise SnapshotError(f"{label} repository database not found: {self.path}")
        uri = "file:" + self.path + "?mode=ro"
        try:
            self.conn = sqlite3.connect(uri, uri=True)
        except sqlite3.OperationalError as exc:
            raise SnapshotError(f"cannot open {label} repository read-only: {self.path}") from exc
        self.conn.row_factory = sqlite3.Row
        # Bound the SQLite page cache so a large input repository cannot drive
        # peak RSS linearly with its file size.  The diff streams via sorted
        # cursors and only holds one object's semantics in Python at a time, so
        # a small cache is sufficient; cache size is measured in ~1 KiB pages.
        self.conn.execute("PRAGMA cache_size=-8192")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self._assert_repository()
        self.start_fingerprint = self.compute_fingerprint()

    # -- lifecycle ------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "RepoSnapShot":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _assert_repository(self) -> None:
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise SnapshotError(
                f"{self.label} input is not an ifc_handover repository: {self.path}"
            ) from exc
        if row is None:
            raise SnapshotError(
                f"{self.label} input has no schema_version marker: {self.path}"
            )

    # -- fingerprint ----------------------------------------------------
    def compute_fingerprint(self) -> str:
        """Stable SHA-256 over every durable business table, in key order."""

        digest = hashlib.sha256()
        digest.update(b"ifc-handover-diff-snapshot\n")
        for table in FINGERPRINT_TABLES:
            digest.update(("table:" + table + "\n").encode("utf-8"))
            columns = self._column_names(table)
            if columns is None:
                digest.update(b"missing\n")
                continue
            ordered = [c for c in columns if not self._is_transient_column(table, c)]
            quoted_cols = ", ".join('"' + c + '"' for c in ordered)
            rows = self.conn.execute(
                f'SELECT {quoted_cols} FROM "{table}" ORDER BY {quoted_cols}'
            )
            for row in rows:
                digest.update(b"\x1f".join(_hash_cell(value) for value in row))
                digest.update(b"\x1e")
            digest.update(b"end-table\n")
        return digest.hexdigest()

    def assert_unchanged(self) -> None:
        current = self.compute_fingerprint()
        if current != self.start_fingerprint:
            raise InputModifiedError(
                f"{self.label} repository {self.path} changed during the comparison; "
                "refusing to emit a mixed-version change dataset"
            )

    def _column_names(self, table: str) -> list[str] | None:
        try:
            rows = self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        except sqlite3.DatabaseError:
            return None
        if not rows:
            return None
        return [str(r["name"]) for r in rows]

    @staticmethod
    def _is_transient_column(table: str, column: str) -> bool:
        # AUTOINCREMENT surrogate ids have no business meaning and can be
        # reassigned after a VACUUM without changing any business row.
        if column == "id" and table in {
            "relation_edges",
            "object_psets",
            "resolved_properties",
            "material_assignments",
            "material_edges",
            "external_references",
            "source_entities",
            "failure_evidence",
            "property_declarations",
        }:
            return True
        if column == "decl_id" and table == "property_declarations":
            return True
        return False

    # -- source metadata ------------------------------------------------
    def sources(self) -> list[SourceRef]:
        rows = self.conn.execute(
            "SELECT source_id, sha256, status FROM sources ORDER BY source_id"
        )
        return [SourceRef(r["source_id"], r["sha256"] or "", r["status"]) for r in rows]

    # -- streamed root groups -------------------------------------------
    def stream_root_groups(self) -> Iterator[list[sqlite3.Row]]:
        """Yield every IfcRoot sharing one GlobalId as one ordered group.

        Rows are streamed by ``(source_id, global_id)`` so the diff can run a
        merge join without holding both repositories in memory.  GlobalIds
        that are duplicated inside a source arrive as one multi-row group,
        which is exactly the evidence needed to mark the identity unusable.
        Nodes without a GlobalId (``key_kind='step'``) are not yielded: STEP
        ids are provenance and must never be used as a cross-version identity.
        """

        cursor = self.conn.execute(
            "SELECT * FROM roots WHERE key_kind='gid' "
            "ORDER BY global_id, source_id, step_id"
        )
        batch: list[sqlite3.Row] = []
        current: tuple[str, str] | None = None
        while True:
            rows = cursor.fetchmany(256)
            if not rows:
                break
            for row in rows:
                token = (row["global_id"], row["source_id"])
                if current is not None and token != current:
                    yield batch
                    batch = []
                current = token
                batch.append(row)
        if batch:
            yield batch

    # -- per-object streamed reads --------------------------------------
    def stream_resolved(self, object_keys: Sequence[str]) -> Iterator[sqlite3.Row]:
        """Stream resolved properties for one matched object's storage keys."""

        if not object_keys:
            return iter(())
        placeholders = ",".join("?" for _ in object_keys)
        return self.conn.execute(
            f"SELECT * FROM resolved_properties WHERE object_key IN ({placeholders}) "
            "ORDER BY identity",
            tuple(object_keys),
        )

    def stream_spatial_edges(self, object_keys: Sequence[str]) -> Iterator[sqlite3.Row]:
        if not object_keys:
            return iter(())
        placeholders = ",".join("?" for _ in object_keys)
        return self.conn.execute(
            f"SELECT * FROM relation_edges WHERE kind IN ('decomposition','containment') "
            f"AND (child_key IN ({placeholders}) OR parent_key IN ({placeholders})) "
            "ORDER BY relation_key, ordinal",
            tuple(object_keys) + tuple(object_keys),
        )

    def type_assignment(self, object_keys: Sequence[str]) -> list[sqlite3.Row]:
        if not object_keys:
            return []
        placeholders = ",".join("?" for _ in object_keys)
        return list(
            self.conn.execute(
                f"SELECT * FROM roots WHERE key IN ({placeholders})", tuple(object_keys)
            )
        )

    def material_assignments(self, object_keys: Sequence[str]) -> list[sqlite3.Row]:
        if not object_keys:
            return []
        placeholders = ",".join("?" for _ in object_keys)
        return list(
            self.conn.execute(
                f"SELECT * FROM material_assignments WHERE object_key IN ({placeholders}) "
                "ORDER BY id",
                tuple(object_keys),
            )
        )

    def material_subtree(self, root_keys: Sequence[str]) -> dict[str, list[sqlite3.Row]]:
        """Return node rows and ordered edges for the given material roots.

        The walk is bounded by the matched object's own material tree, not by
        the repository size.
        """

        nodes: dict[str, sqlite3.Row] = {}
        edges: list[sqlite3.Row] = []
        queue = list(root_keys)
        seen: set[str] = set()
        while queue:
            chunk = [k for k in queue if k not in seen]
            queue = []
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            for row in self.conn.execute(
                f'SELECT * FROM material_nodes WHERE key IN ({placeholders})', tuple(chunk)
            ):
                if row["key"] not in nodes:
                    nodes[row["key"]] = row
                    seen.add(row["key"])
            for row in self.conn.execute(
                f"SELECT * FROM material_edges WHERE parent_key IN ({placeholders}) "
                "ORDER BY slot, ordinal",
                tuple(chunk),
            ):
                edges.append(row)
                if row["child_key"] not in seen:
                    queue.append(row["child_key"])
        return {"nodes": list(nodes.values()), "edges": edges}

    def source_entity(self, source_id: str, step_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM source_entities WHERE source_id=? AND step_id=? "
            "ORDER BY role LIMIT 1",
            (source_id, step_id),
        ).fetchone()

    def root_by_key(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM roots WHERE key=?", (key,)).fetchone()


def _hash_cell(value: Any) -> bytes:
    if value is None:
        return b"\x00"
    if isinstance(value, int):
        return b"i" + str(value).encode("utf-8")
    if isinstance(value, float):
        return b"f" + repr(value).encode("utf-8")
    if isinstance(value, bytes):
        return b"b" + value
    return b"s" + str(value).encode("utf-8")
