"""SQLite storage for the offline handover repository.

The database is the durable object store.  Per-source ingestion writes inside
one transaction and releases the in-memory IfcOpenShell/STEP view afterwards,
so the process never holds the property graph of the whole collection.  Spatial
paths are not cached as rows either; ancestor queries walk parent edges on
demand.

Business identity is ``(source_id, GlobalId)`` for IfcRoot objects.  Nodes
without a GlobalId use ``(source_id, sha256, step_id)``.  File paths and STEP
ids are provenance, not merge keys.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    declared_sha256 TEXT NOT NULL,
    sha256 TEXT,
    file_name TEXT,
    stored_path TEXT,
    schema_name TEXT,
    status TEXT NOT NULL,
    error TEXT,
    entity_count INTEGER,
    root_count INTEGER,
    object_count INTEGER,
    ingested_at TEXT,
    stats_json TEXT
);

CREATE TABLE IF NOT EXISTS roots (
    key TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    global_id TEXT,
    key_kind TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    category TEXT NOT NULL,
    name TEXT,
    object_type TEXT,
    predefined_type TEXT,
    is_object INTEGER NOT NULL DEFAULT 0,
    is_spatial INTEGER NOT NULL DEFAULT 0,
    is_type INTEGER NOT NULL DEFAULT 0,
    is_product INTEGER NOT NULL DEFAULT 0,
    usable INTEGER NOT NULL DEFAULT 1,
    conflict_state TEXT,
    conflict_detail_json TEXT,
    type_key TEXT,
    attributes_json TEXT,
    taxonomy_json TEXT,
    UNIQUE(source_id, step_id)
);
CREATE INDEX IF NOT EXISTS roots_entity_type ON roots(entity_type);
CREATE INDEX IF NOT EXISTS roots_source ON roots(source_id);
CREATE INDEX IF NOT EXISTS roots_type_key ON roots(type_key);
CREATE INDEX IF NOT EXISTS roots_usable ON roots(usable);
CREATE INDEX IF NOT EXISTS roots_gid ON roots(source_id, global_id);

CREATE TABLE IF NOT EXISTS nodes (
    key TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    name TEXT,
    attributes_json TEXT,
    taxonomy_json TEXT,
    UNIQUE(source_id, step_id)
);
CREATE INDEX IF NOT EXISTS nodes_entity_type ON nodes(entity_type);
CREATE INDEX IF NOT EXISTS nodes_source ON nodes(source_id);

CREATE TABLE IF NOT EXISTS relations (
    relation_key TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL,
    relating_key TEXT,
    relating_step_id INTEGER,
    state TEXT NOT NULL,
    error TEXT,
    json TEXT
);
CREATE INDEX IF NOT EXISTS relations_source ON relations(source_id);
CREATE INDEX IF NOT EXISTS relations_type ON relations(relation_type);
CREATE INDEX IF NOT EXISTS relations_state ON relations(state);

CREATE TABLE IF NOT EXISTS relation_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    kind TEXT NOT NULL,          -- decomposition | containment
    ordinal INTEGER NOT NULL,
    parent_key TEXT,
    parent_step_id INTEGER,
    child_key TEXT,
    child_step_id INTEGER,
    edge_state TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS edges_parent ON relation_edges(parent_key);
CREATE INDEX IF NOT EXISTS edges_child ON relation_edges(child_key);
CREATE INDEX IF NOT EXISTS edges_source ON relation_edges(source_id);
CREATE INDEX IF NOT EXISTS edges_relation ON relation_edges(relation_key);

CREATE TABLE IF NOT EXISTS pset_definitions (
    pset_key TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    owner_scope TEXT NOT NULL,   -- instance | type | unassigned
    json TEXT
);
CREATE INDEX IF NOT EXISTS psets_name ON pset_definitions(name);

CREATE TABLE IF NOT EXISTS property_declarations (
    decl_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_key TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    pset_key TEXT NOT NULL,
    pset_name TEXT NOT NULL,
    prop_step_id INTEGER NOT NULL,
    prop_kind TEXT NOT NULL,
    prop_name TEXT,
    identity TEXT NOT NULL,
    signature TEXT NOT NULL,
    state TEXT NOT NULL,
    value_status TEXT NOT NULL,
    declared_type TEXT,
    unit_step_id INTEGER,
    unit_source TEXT,
    original_raw TEXT,
    expression TEXT,
    json TEXT
);
CREATE INDEX IF NOT EXISTS decl_identity ON property_declarations(identity);
CREATE INDEX IF NOT EXISTS decl_pset ON property_declarations(pset_key);
CREATE INDEX IF NOT EXISTS decl_source ON property_declarations(source_id);

CREATE TABLE IF NOT EXISTS object_psets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    pset_key TEXT,
    pset_step_id INTEGER,
    scope TEXT NOT NULL,         -- instance | type
    relation_key TEXT,
    state TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS object_psets_object ON object_psets(object_key);
CREATE INDEX IF NOT EXISTS object_psets_pset ON object_psets(pset_key);

CREATE TABLE IF NOT EXISTS resolved_properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    pset_name TEXT NOT NULL,
    prop_name TEXT,
    prop_kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    state TEXT NOT NULL,
    value_status TEXT NOT NULL,
    scope_used TEXT,
    is_conflict INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    json TEXT,
    UNIQUE(object_key, identity)
);
CREATE INDEX IF NOT EXISTS resolved_object ON resolved_properties(object_key);
CREATE INDEX IF NOT EXISTS resolved_state ON resolved_properties(state);
CREATE INDEX IF NOT EXISTS resolved_status ON resolved_properties(value_status);
CREATE INDEX IF NOT EXISTS resolved_name ON resolved_properties(pset_name, prop_name);

CREATE TABLE IF NOT EXISTS material_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    relation_key TEXT,
    material_step_id INTEGER,
    root_key TEXT,
    state TEXT NOT NULL,
    json TEXT
);
CREATE INDEX IF NOT EXISTS matassign_object ON material_assignments(object_key);
CREATE INDEX IF NOT EXISTS matassign_root ON material_assignments(root_key);

CREATE TABLE IF NOT EXISTS material_nodes (
    key TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT,
    json TEXT,
    UNIQUE(source_id, step_id)
);
CREATE INDEX IF NOT EXISTS matnode_source ON material_nodes(source_id);

CREATE TABLE IF NOT EXISTS material_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_key TEXT NOT NULL,
    child_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    slot TEXT NOT NULL,
    json TEXT,
    UNIQUE(parent_key, slot, ordinal)
);
CREATE INDEX IF NOT EXISTS matedge_parent ON material_edges(parent_key);
CREATE INDEX IF NOT EXISTS matedge_child ON material_edges(child_key);

CREATE TABLE IF NOT EXISTS units (
    unit_key TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    dimension TEXT,
    usable INTEGER NOT NULL,
    json TEXT
);
CREATE INDEX IF NOT EXISTS units_source ON units(source_id);

CREATE TABLE IF NOT EXISTS project_units (
    source_id TEXT NOT NULL,
    project_step_id INTEGER,
    dimension TEXT NOT NULL,
    unit_step_id INTEGER,
    state TEXT NOT NULL,
    json TEXT,
    PRIMARY KEY (source_id, project_step_id, dimension)
);

CREATE TABLE IF NOT EXISTS external_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    from_key TEXT,
    relation_key TEXT,
    target_step_id INTEGER,
    target_kind TEXT,
    location TEXT,
    identification TEXT,
    name TEXT,
    state TEXT NOT NULL,
    json TEXT
);
CREATE INDEX IF NOT EXISTS extrefs_from ON external_references(from_key);
CREATE INDEX IF NOT EXISTS extrefs_source ON external_references(source_id);

CREATE TABLE IF NOT EXISTS source_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    role TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    raw_text TEXT,
    business_ref TEXT,
    UNIQUE(source_id, step_id, role)
);
CREATE INDEX IF NOT EXISTS src_ent_source ON source_entities(source_id);
CREATE INDEX IF NOT EXISTS src_ent_step ON source_entities(source_id, step_id);

CREATE TABLE IF NOT EXISTS source_headers (
    source_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    header_text TEXT
);

CREATE TABLE IF NOT EXISTS failure_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT,
    sha256 TEXT,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    entity_key TEXT,
    step_id INTEGER,
    relation_key TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS failures_category ON failure_evidence(category);
CREATE INDEX IF NOT EXISTS failures_source ON failure_evidence(source_id);
"""


class AmbiguousIdentity(LookupError):
    """A business identity matched more than one entity.

    Duplicate GlobalIds are retained as conflicts; callers must inspect the
    conflict evidence rather than receive an arbitrary entity.
    """

    def __init__(self, source_id: str, global_id: str, step_ids: list[int]):
        self.source_id = source_id
        self.global_id = global_id
        self.step_ids = step_ids
        super().__init__(
            f"GlobalId {global_id!r} in source {source_id!r} matches "
            f"{len(step_ids)} entities: {step_ids}"
        )


class Repository:
    """Read/write facade over the SQLite handover store."""

    def __init__(self, path: str, *, create: bool = False):
        self.path = os.path.abspath(path)
        if not create and not os.path.exists(self.path):
            raise FileNotFoundError(f"repository not found: {self.path}")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        if create:
            self.conn.executescript(SCHEMA_SQL)
            with self.transaction():
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
        self._check_version()

    @classmethod
    def create(cls, path: str) -> "Repository":
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        return cls(path, create=True)

    def _check_version(self) -> None:
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"not an ifc_handover repository: {self.path}") from exc
        if row is None or int(row["value"]) != SCHEMA_VERSION:
            raise ValueError(
                f"repository schema version mismatch at {self.path}: "
                f"{None if row is None else row['value']} != {SCHEMA_VERSION}"
            )

    # -- transactions ---------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.conn.execute("BEGIN")
            yield self.conn
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- source helpers -------------------------------------------------
    def source(self, source_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def sources(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM sources ORDER BY source_id")
        ]

    def source_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"])

    def delete_source(self, source_id: str) -> None:
        with self.transaction() as conn:
            self.delete_source_rows(conn, source_id)

    @staticmethod
    def delete_source_rows(conn: sqlite3.Connection, source_id: str) -> None:
        """Remove every row belonging to a source inside a caller transaction.

        Called at the start of each ingestion transaction so a re-ingested
        source never mixes old observations with new ones, and a failed
        ingestion leaves no partial business rows behind.
        """

        material_keys = [
            row[0]
            for row in conn.execute(
                "SELECT key FROM material_nodes WHERE source_id=?", (source_id,)
            )
        ]
        if material_keys:
            # Material edges have no source column; delete by scoped keys.
            conn.executemany(
                "DELETE FROM material_edges WHERE parent_key=?", [(k,) for k in material_keys]
            )
        for table in (
            "resolved_properties",
            "object_psets",
            "material_assignments",
            "property_declarations",
            "pset_definitions",
            "relation_edges",
            "relations",
            "roots",
            "nodes",
            "material_nodes",
            "units",
            "project_units",
            "external_references",
            "source_entities",
            "source_headers",
            "failure_evidence",
            "sources",
        ):
            conn.execute(f"DELETE FROM {table} WHERE source_id=?", (source_id,))

    # -- object queries -------------------------------------------------
    def get_object(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM roots WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        return self._object_row(row)

    def find_object(
        self, source_id: str, global_id: str | None = None, step_id: int | None = None
    ) -> dict[str, Any] | None:
        if global_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM roots WHERE source_id=? AND global_id=?",
                (source_id, global_id),
            ).fetchall()
            if len(rows) > 1:
                # A duplicated GlobalId is a conflict, not a synonym for "first
                # match".  Refuse to silently pick one entity.
                raise AmbiguousIdentity(
                    source_id, global_id, [row["step_id"] for row in rows]
                )
            row = rows[0] if rows else None
        elif step_id is not None:
            row = self.conn.execute(
                "SELECT * FROM roots WHERE source_id=? AND step_id=?",
                (source_id, step_id),
            ).fetchone()
        else:
            return None
        return None if row is None else self._object_row(row)

    def _object_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["usable"] = bool(data["usable"])
        for flag in ("is_object", "is_spatial", "is_type", "is_product"):
            data[flag] = bool(data[flag])
        data["conflict_detail"] = _load_json(data.pop("conflict_detail_json"))
        data["attributes"] = _load_json(data.pop("attributes_json"))
        data["taxonomy"] = _load_json(data.pop("taxonomy_json"))
        return data

    def identity_conflicts(self, source_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM roots WHERE source_id=? AND conflict_state='duplicate_global_id' "
            "ORDER BY step_id",
            (source_id,),
        )
        return [self._object_row(row) for row in rows]

    def query_objects(
        self,
        *,
        entity_type: str | None = None,
        type_key: str | None = None,
        ancestor: str | None = None,
        usable: bool | None = None,
        spatial_only: bool = False,
        include_types: bool = False,
        property_pset: str | None = None,
        property_name: str | None = None,
        property_state: str | None = None,
        property_status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_types:
            clauses.append("r.is_object = 1")
        if entity_type:
            clauses.append("r.entity_type = ?")
            params.append(entity_type)
        if type_key:
            clauses.append("r.type_key = ?")
            params.append(type_key)
        if usable is not None:
            clauses.append("r.usable = ?")
            params.append(1 if usable else 0)
        if spatial_only:
            clauses.append("r.is_spatial = 1")
        if ancestor:
            # Only usable hierarchy edges are traversed.  Conflicted or missing
            # edges are reported by descendants_detail/ancestors instead of
            # being silently treated as normal containment.
            clauses.append(
                "r.key IN ("
                "WITH RECURSIVE sub(k) AS ("
                " SELECT ? UNION SELECT e.child_key FROM relation_edges e"
                " JOIN sub ON e.parent_key = sub.k"
                " WHERE e.child_key IS NOT NULL AND e.edge_state = 'ok'"
                ") SELECT k FROM sub)"
            )
            params.append(ancestor)
        if property_pset or property_name or property_state or property_status:
            clauses.append(
                "r.key IN (SELECT rp.object_key FROM resolved_properties rp WHERE 1=1"
                + (" AND rp.pset_name = ?" if property_pset else "")
                + (" AND rp.prop_name = ?" if property_name else "")
                + (" AND rp.state = ?" if property_state else "")
                + (" AND rp.value_status = ?" if property_status else "")
                + ")"
            )
            for value in (property_pset, property_name, property_state, property_status):
                if value:
                    params.append(value)
        sql = "SELECT r.* FROM roots r"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY r.source_id, r.entity_type, r.step_id"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [self._object_row(row) for row in self.conn.execute(sql, params)]

    def count_objects(self, *, usable: bool | None = None) -> int:
        sql = "SELECT COUNT(*) c FROM roots WHERE is_object=1"
        params: list[Any] = []
        if usable is not None:
            sql += " AND usable=?"
            params.append(1 if usable else 0)
        return int(self.conn.execute(sql, params).fetchone()["c"])

    # -- property queries ----------------------------------------------
    def properties(self, object_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM resolved_properties WHERE object_key=? ORDER BY pset_name, prop_name, prop_kind",
            (object_key,),
        )
        return [self._property_row(row) for row in rows]

    def property(
        self, object_key: str, pset_name: str, prop_name: str, kind: str | None = None
    ) -> dict[str, Any] | None:
        sql = (
            "SELECT * FROM resolved_properties WHERE object_key=? AND pset_name=? "
            "AND prop_name=?"
        )
        params: list[Any] = [object_key, pset_name, prop_name]
        if kind:
            sql += " AND prop_kind=?"
            params.append(kind)
        row = self.conn.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
        return None if row is None else self._property_row(row)

    def _property_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["is_conflict"] = bool(data["is_conflict"])
        data["detail"] = _load_json(data.pop("json"))
        return data

    def property_states(self, object_key: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) c FROM resolved_properties WHERE object_key=? GROUP BY state",
            (object_key,),
        )
        return {row["state"]: int(row["c"]) for row in rows}

    def declarations_for(
        self, pset_key: str, prop_step_id: int
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM property_declarations WHERE pset_key=? AND prop_step_id=?",
            (pset_key, prop_step_id),
        )
        out = []
        for row in rows:
            data = dict(row)
            data["detail"] = _load_json(data.pop("json"))
            out.append(data)
        return out

    def all_declarations(self, source_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM property_declarations"
        params: list[Any] = []
        if source_id:
            sql += " WHERE source_id=?"
            params.append(source_id)
        rows = self.conn.execute(sql + " ORDER BY pset_name, prop_name, decl_id", params)
        out = []
        for row in rows:
            data = dict(row)
            data["detail"] = _load_json(data.pop("json"))
            out.append(data)
        return out

    # -- relations ------------------------------------------------------
    def relations_for(self, object_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT DISTINCT rel.* FROM relations rel LEFT JOIN relation_edges e "
            "ON e.relation_key = rel.relation_key "
            "WHERE e.parent_key=? OR e.child_key=? OR rel.relating_key=? "
            "ORDER BY rel.step_id",
            (object_key, object_key, object_key),
        )
        out = []
        for row in rows:
            data = dict(row)
            data["detail"] = _load_json(data.pop("json"))
            data["edges"] = self.relation_edges(data["relation_key"])
            out.append(data)
        return out

    def relation_edges(self, relation_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM relation_edges WHERE relation_key=? ORDER BY ordinal",
            (relation_key,),
        )
        return [dict(r) for r in rows]

    def children(
        self, object_key: str, *, kinds: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT e.*, r.entity_type child_entity_type, r.name child_name "
            "FROM relation_edges e LEFT JOIN roots r ON r.key = e.child_key "
            "WHERE e.parent_key=?"
        )
        params: list[Any] = [object_key]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND e.kind IN ({placeholders})"
            params.extend(kinds)
        sql += " ORDER BY e.kind, e.ordinal"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def parents(
        self, object_key: str, *, kinds: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT e.*, r.entity_type parent_entity_type, r.name parent_name "
            "FROM relation_edges e LEFT JOIN roots r ON r.key = e.parent_key "
            "WHERE e.child_key=?"
        )
        params: list[Any] = [object_key]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND e.kind IN ({placeholders})"
            params.extend(kinds)
        return [dict(r) for r in self.conn.execute(sql, params)]

    def ancestors(self, object_key: str) -> dict[str, Any]:
        """Walk spatial parents on demand; no path cache is kept."""

        paths: list[list[dict[str, Any]]] = []
        missing: list[dict[str, Any]] = []
        cycle_paths: list[list[str]] = []
        stack: list[tuple[str, list[str], list[dict[str, Any]]]] = [
            (object_key, [object_key], [])
        ]
        seen_states: set[tuple[str, ...]] = set()
        while stack:
            current, visited, chain = stack.pop()
            parents = self.parents(current, kinds=("decomposition", "containment"))
            if not parents:
                paths.append(chain)
                continue
            for edge in parents:
                if edge["edge_state"] == "missing":
                    missing.append(
                        {
                            "relation_key": edge["relation_key"],
                            "relation_type": edge["relation_type"],
                            "parent_step_id": edge["parent_step_id"],
                            "child_step_id": edge["child_step_id"],
                            "reason": edge["reason"],
                        }
                    )
                    paths.append(chain)
                    continue
                parent_key = edge["parent_key"]
                if parent_key is None:
                    continue
                if parent_key in visited:
                    cycle_paths.append(visited + [parent_key])
                    continue
                step = {
                    "key": parent_key,
                    "entity_type": edge["parent_entity_type"],
                    "name": edge["parent_name"],
                    "relation_key": edge["relation_key"],
                    "kind": edge["kind"],
                }
                state_key = tuple(visited + [parent_key])
                if state_key in seen_states:
                    continue
                seen_states.add(state_key)
                stack.append((parent_key, visited + [parent_key], chain + [step]))
        status = "ok"
        if missing and not paths:
            status = "missing"
        elif missing:
            status = "partial_missing"
        if cycle_paths:
            status = "conflict"
        return {
            "object_key": object_key,
            "paths": paths,
            "missing": missing,
            "cycles": cycle_paths,
            "status": status,
        }

    def descendants(
        self, object_key: str, *, kinds: Sequence[str] = ("decomposition", "containment")
    ) -> list[tuple[str, int, str]]:
        """Breadth-first descendants over non-conflicted edges; cycle-safe.

        Conflict and missing edges are not traversed as normal hierarchy.  Use
        :meth:`descendants_detail` to receive them as evidence rather than
        silently treating the relation as usable.
        """

        return [
            (item["key"], item["depth"], item["kind"])
            for item in self.descendants_detail(object_key, kinds=kinds)["objects"]
        ]

    def descendants_detail(
        self, object_key: str, *, kinds: Sequence[str] = ("decomposition", "containment")
    ) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        seen = {object_key}
        queue: list[tuple[str, int]] = [(object_key, 0)]
        while queue:
            current, depth = queue.pop(0)
            for edge in self.children(current, kinds=kinds):
                child = edge["child_key"]
                if edge["edge_state"] != "ok":
                    blocked.append(
                        {
                            "parent_key": current,
                            "child_key": child,
                            "child_step_id": edge["child_step_id"],
                            "kind": edge["kind"],
                            "edge_state": edge["edge_state"],
                            "reason": edge["reason"],
                            "relation_key": edge["relation_key"],
                        }
                    )
                    continue
                if child is None or child in seen:
                    continue
                seen.add(child)
                out.append({"key": child, "depth": depth + 1, "kind": edge["kind"]})
                queue.append((child, depth + 1))
        return {
            "object_key": object_key,
            "objects": out,
            "blocked_edges": blocked,
            "status": "conflict" if blocked else "ok",
        }

    # -- materials / units / external refs ------------------------------
    def material_assignments(self, object_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM material_assignments WHERE object_key=? ORDER BY id",
            (object_key,),
        )
        return [
            {
                "relation_key": r["relation_key"],
                "material_step_id": r["material_step_id"],
                "root_key": r["root_key"],
                "state": r["state"],
                "tree": _load_json(r["json"]),
            }
            for r in rows
        ]

    def units_for_source(self, source_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM units WHERE source_id=? ORDER BY step_id", (source_id,)
        )
        return [
            {
                "unit_key": r["unit_key"],
                "step_id": r["step_id"],
                "dimension": r["dimension"],
                "usable": bool(r["usable"]),
                "detail": _load_json(r["json"]),
            }
            for r in rows
        ]

    def project_units_for_source(self, source_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM project_units WHERE source_id=? ORDER BY dimension",
            (source_id,),
        )
        return [
            {
                "dimension": r["dimension"],
                "unit_step_id": r["unit_step_id"],
                "state": r["state"],
                "detail": _load_json(r["json"]),
            }
            for r in rows
        ]

    def external_references(self, object_key: str | None = None) -> list[dict[str, Any]]:
        if object_key:
            rows = self.conn.execute(
                "SELECT * FROM external_references WHERE from_key=? ORDER BY id",
                (object_key,),
            )
        else:
            rows = self.conn.execute("SELECT * FROM external_references ORDER BY id")
        return [
            {
                "source_id": r["source_id"],
                "from_key": r["from_key"],
                "relation_key": r["relation_key"],
                "target_step_id": r["target_step_id"],
                "target_kind": r["target_kind"],
                "location": r["location"],
                "identification": r["identification"],
                "name": r["name"],
                "state": r["state"],
                "detail": _load_json(r["json"]),
            }
            for r in rows
        ]

    # -- failures -------------------------------------------------------
    def failures(
        self, *, category: str | None = None, source_id: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM failure_evidence WHERE 1=1"
        params: list[Any] = []
        if category:
            sql += " AND category=?"
            params.append(category)
        if source_id:
            sql += " AND source_id=?"
            params.append(source_id)
        rows = self.conn.execute(sql + " ORDER BY id", params)
        return [
            {
                "source_id": r["source_id"],
                "sha256": r["sha256"],
                "category": r["category"],
                "severity": r["severity"],
                "entity_key": r["entity_key"],
                "step_id": r["step_id"],
                "relation_key": r["relation_key"],
                "detail": _load_json(r["detail_json"]),
            }
            for r in rows
        ]

    def stats(self) -> dict[str, Any]:
        def scalar(sql: str, params: Sequence[Any] = ()) -> int:
            return int(self.conn.execute(sql, params).fetchone()[0])

        return {
            "sources": scalar("SELECT COUNT(*) FROM sources"),
            "sources_failed": scalar(
                "SELECT COUNT(*) FROM sources WHERE status != 'ok'"
            ),
            "objects": scalar("SELECT COUNT(*) FROM roots WHERE is_object=1"),
            "objects_unusable": scalar(
                "SELECT COUNT(*) FROM roots WHERE is_object=1 AND usable=0"
            ),
            "types": scalar("SELECT COUNT(*) FROM roots WHERE is_type=1"),
            "relations": scalar("SELECT COUNT(*) FROM relations"),
            "relations_missing": scalar(
                "SELECT COUNT(*) FROM relations WHERE state IN ('missing','conflict')"
            ),
            "resolved_properties": scalar(
                "SELECT COUNT(*) FROM resolved_properties"
            ),
            "property_conflicts": scalar(
                "SELECT COUNT(*) FROM resolved_properties WHERE is_conflict=1"
            ),
            "failure_evidence": scalar("SELECT COUNT(*) FROM failure_evidence"),
            "material_assignments": scalar(
                "SELECT COUNT(*) FROM material_assignments"
            ),
        }


def _load_json(text: str | None) -> Any:
    if text is None:
        return None
    return json.loads(text)


def dump_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
