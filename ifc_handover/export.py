"""Subset export: a self-contained handover package.

Selection rule
--------------
The caller gives one or more spatial roots (object keys).  Selection walks
``decomposition`` and ``containment`` edges only, downward from those roots.
Nothing is selected because it shares a type, a material or a STEP section with
a selected object.  This is what prevents "other rooms of the same type" from
leaking into a package.

Closure rule
------------
For each selected object the package includes exactly the evidence that object
actually uses:

* its resolving property declarations (instance and, where not overridden,
  type declarations), including masked type declarations and unit evidence;
* its type declaration if a type declaration contributed a property or a
  material to the object;
* its material assignment tree (ordered), including shared layer sets once;
* the project unit assignment for every unit referenced by those properties;
* source excerpts for every STEP entity backing a business row.

Shared definitions are stored once, keyed by their repository key.

Independence
------------
The package embeds its own SQLite database, JSON tables and a raw STEP excerpt
per source.  It contains no reference to the original file paths.  Writing is
atomic: the package is built in a temporary sibling directory and renamed into
place only after every file is written and fsynced.  A failed export never
replaces an existing package.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .keys import safe_file_component
from .step import StepFile, build_excerpt_ifc
from .store import Repository, dump_json
from .stored import StoredSource

PACKAGE_VERSION = 1
PACKAGE_TABLES = [
    "objects",
    "object_types",
    "properties",
    "property_declarations",
    "type_declarations",
    "materials",
    "material_edges",
    "object_materials",
    "units",
    "project_units",
    "relations",
    "relation_edges",
    "external_references",
    "failures",
    "sources",
    "excluded_objects",
    "excluded_properties",
    "manifest",
]


class ExportError(Exception):
    pass


@dataclass
class Selection:
    roots: list[str]
    objects: list[str] = field(default_factory=list)
    by_depth: dict[str, int] = field(default_factory=dict)
    via: dict[str, str] = field(default_factory=dict)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    root_ancestors: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "roots": self.roots,
            "objects": self.objects,
            "by_depth": self.by_depth,
            "via": self.via,
            "excluded": self.excluded,
            "root_ancestors": self.root_ancestors,
        }


def select_subset(repo: Repository, roots: Sequence[str]) -> Selection:
    """Select the roots and their decomposition/containment descendants."""

    selection = Selection(roots=list(roots))
    seen: set[str] = set()
    for root in roots:
        row = repo.get_object(root)
        if row is None:
            raise ExportError(f"spatial root {root!r} is not in the repository")
        if not row["is_spatial"]:
            raise ExportError(
                f"root {root!r} ({row['entity_type']}) is not an IfcSpatialElement"
            )
        if row["key"] in seen:
            continue
        seen.add(row["key"])
        selection.objects.append(root)
        selection.by_depth[root] = 0
        selection.via[root] = "root"
        selection.root_ancestors[root] = repo.ancestors(root)
    # Traverse downward, but prune at unusable nodes: an object whose spatial
    # membership runs through a conflicted room cannot be handed over silently.
    for root in roots:
        queue: list[tuple[str, int, str]] = [(root, 0, "root")]
        while queue:
            current, depth, kind = queue.pop(0)
            for edge in repo.children(current, kinds=("decomposition", "containment")):
                child = edge["child_key"]
                if edge["edge_state"] != "ok":
                    # A conflicted or missing edge cannot carry ordinary
                    # containment.  Retain it as evidence and prune.
                    selection.excluded.append(
                        {
                            "key": child,
                            "step_id": edge["child_step_id"],
                            "reason": f"edge is {edge['edge_state']}: {edge['reason']}",
                            "via_root": root,
                            "kind": edge["kind"],
                            "parent_key": current,
                        }
                    )
                    if child is not None and child not in seen:
                        seen.add(child)
                        _mark_subtree_excluded(
                            repo, child, root, selection, seen, depth + 1
                        )
                    continue
                if child is None:
                    selection.excluded.append(
                        {
                            "key": None,
                            "step_id": edge["child_step_id"],
                            "reason": "selected edge has a missing endpoint",
                            "via_root": root,
                            "kind": edge["kind"],
                            "parent_key": current,
                        }
                    )
                    continue
                if child in seen:
                    continue
                seen.add(child)
                row = repo.get_object(child)
                if row is None:
                    selection.excluded.append(
                        {
                            "key": child,
                            "reason": "selected edge reaches a node outside the object table",
                            "via_root": root,
                            "kind": edge["kind"],
                        }
                    )
                    continue
                child_depth = depth + 1
                if not row["usable"]:
                    selection.excluded.append(
                        {
                            "key": child,
                            "reason": "object is not usable ("
                            + str(row["conflict_state"])
                            + ")",
                            "via_root": root,
                            "kind": edge["kind"],
                            "depth": child_depth,
                            "entity_type": row["entity_type"],
                        }
                    )
                    _mark_subtree_excluded(repo, child, root, selection, seen, child_depth)
                    continue
                selection.objects.append(child)
                selection.by_depth[child] = child_depth
                selection.via[child] = edge["kind"]
                queue.append((child, child_depth, edge["kind"]))
    return selection


def _mark_subtree_excluded(
    repo: Repository,
    parent: str,
    root: str,
    selection: Selection,
    seen: set[str],
    depth: int,
) -> None:
    """Record descendants of an unusable node without selecting them."""

    queue: list[tuple[str, int]] = [(parent, depth)]
    limit = depth + 256
    while queue:
        current, current_depth = queue.pop(0)
        if current_depth > limit:
            continue
        for edge in repo.children(current, kinds=("decomposition", "containment")):
            child = edge["child_key"]
            if child is None or child in seen:
                continue
            seen.add(child)
            row = repo.get_object(child)
            if row is not None and not row["usable"]:
                # The descendant is itself conflicted; cite its own reason.
                reason = "object is not usable (" + str(row["conflict_state"]) + ")"
            else:
                reason = "ancestor is not usable; subtree is not handed over"
            selection.excluded.append(
                {
                    "key": child,
                    "reason": reason,
                    "via_root": root,
                    "kind": edge["kind"],
                    "depth": current_depth + 1,
                    "entity_type": row["entity_type"] if row else None,
                    "ancestor_key": parent,
                }
            )
            queue.append((child, current_depth + 1))


@dataclass
class Closure:
    """Definitions actually used by the selected objects."""

    objects: list[dict[str, Any]] = field(default_factory=list)
    types: dict[str, dict[str, Any]] = field(default_factory=dict)
    type_psets: set[str] = field(default_factory=set)
    properties: list[dict[str, Any]] = field(default_factory=list)
    declarations: list[dict[str, Any]] = field(default_factory=list)
    material_roots: set[str] = field(default_factory=set)
    object_materials: list[dict[str, Any]] = field(default_factory=list)
    material_nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    material_edges: list[dict[str, Any]] = field(default_factory=list)
    units: dict[str, dict[str, Any]] = field(default_factory=dict)
    project_units: list[dict[str, Any]] = field(default_factory=list)
    relation_keys: set[str] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)
    external_references: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    unit_step_ids: dict[str, set[int]] = field(
        default_factory=dict
    )  # source_id -> step ids
    declaration_step_ids: dict[str, set[int]] = field(default_factory=dict)
    object_step_ids: dict[str, set[int]] = field(default_factory=dict)

    def as_summary(self) -> dict[str, Any]:
        return {
            "objects": len(self.objects),
            "types": len(self.types),
            "properties": len(self.properties),
            "declarations": len(self.declarations),
            "material_roots": len(self.material_roots),
            "material_nodes": len(self.material_nodes),
            "units": len(self.units),
            "relations": len(self.relation_keys),
            "sources": sorted(self.source_ids),
        }


def build_closure(repo: Repository, selection: Selection) -> Closure:
    closure = Closure()
    object_rows = []
    for key in selection.objects:
        row = repo.get_object(key)
        if row is None:
            continue
        if not row["usable"]:
            continue
        object_rows.append(row)
        closure.source_ids.add(row["source_id"])
        closure.object_step_ids.setdefault(row["source_id"], set()).add(row["step_id"])
    closure.objects = object_rows

    seen_decl: set[tuple[str, int]] = set()
    for row in object_rows:
        key = row["key"]
        properties = repo.properties(key)
        closure.properties.extend(properties)
        for prop in properties:
            detail = prop["detail"]
            for source in detail.get("sources", []):
                marker = (str(source.get("pset_key")), int(source.get("step_id")))
                if marker in seen_decl:
                    continue
                seen_decl.add(marker)
                closure.declarations.extend(
                    repo.declarations_for(marker[0], marker[1])
                )
        for prop in properties:
            detail = prop["detail"]
            for source in detail.get("sources", []):
                closure.source_ids.add(source["source_id"])
                closure.declaration_step_ids.setdefault(source["source_id"], set()).add(
                    int(source["step_id"])
                )
            type_decl = detail.get("type_declaration_ignored") or {}
            if type_decl:
                for source in type_decl.get("sources", []):
                    closure.source_ids.add(source["source_id"])
                    closure.declaration_step_ids.setdefault(
                        source["source_id"], set()
                    ).add(int(source["step_id"]))
            declaration = detail.get("declaration") or {}
            type_ignored = (detail.get("type_declaration_ignored") or {}).get(
                "declaration"
            ) or {}
            for decl in (declaration, type_ignored):
                for measured in (decl.get("interpretation") or {}).get("measured", []):
                    if not measured:
                        continue
                    unit = measured.get("unit")
                    if unit and unit.get("unit_step_id"):
                        unit_source = measured.get("unit_source") or "project"
                        closure.units[f"{row['source_id']}:{unit['unit_step_id']}"] = {
                            "source_id": row["source_id"],
                            "unit_step_id": unit["unit_step_id"],
                            "unit_source": unit_source,
                            "unit": unit,
                        }
                        closure.unit_step_ids.setdefault(row["source_id"], set()).add(
                            int(unit["unit_step_id"])
                        )
            # A unit can also be explicit even when interpretation failed
            # before producing a measured value (cycle/missing/dimension
            # mismatch).  It is still evidence and must be exported.
            decl_unit = declaration.get("unit_step_id")
            if decl_unit is not None:
                closure.units.setdefault(
                    f"{row['source_id']}:{decl_unit}",
                    {
                        "source_id": row["source_id"],
                        "unit_step_id": decl_unit,
                        "unit_source": "property",
                        "unit": None,
                    },
                )
                closure.unit_step_ids.setdefault(row["source_id"], set()).add(int(decl_unit))

        for assignment in repo.material_assignments(key):
            if assignment["state"] != "ok" or assignment["root_key"] is None:
                continue
            closure.material_roots.add(assignment["root_key"])
            tree = assignment.get("tree") or {}
            closure.object_materials.append(
                {
                    "object_key": key,
                    "material_key": assignment["root_key"],
                    "state": assignment["state"],
                    "inherited_from_key": tree.get("inherited_from_key"),
                    "detail": tree,
                }
            )

        type_key = row.get("type_key")
        if type_key and _type_contributes(repo, row, properties):
            type_row = repo.get_object(type_key)
            if type_row is not None:
                closure.types[type_key] = type_row
                closure.source_ids.add(type_row["source_id"])
                closure.object_step_ids.setdefault(type_row["source_id"], set()).add(
                    type_row["step_id"]
                )
                closure.relation_keys.update(
                    rel["relation_key"] for rel in repo.relations_for(key)
                    if rel["relation_type"] == "IfcRelDefinesByType"
                )

        for relation in repo.relations_for(key):
            if relation["state"] in {"ok", "conflict"}:
                closure.relation_keys.add(relation["relation_key"])

        for ref in repo.external_references(key):
            closure.external_references.append(ref)

    # Material trees: only roots reachable from the selected objects.
    for root_key in sorted(closure.material_roots):
        rows = repo.conn.execute(
            "SELECT * FROM material_nodes WHERE key=?", (root_key,)
        ).fetchall()
        queue = [root_key]
        while queue:
            current = queue.pop(0)
            if current in closure.material_nodes:
                continue
            node = repo.conn.execute(
                "SELECT * FROM material_nodes WHERE key=?", (current,)
            ).fetchone()
            if node is None:
                closure.material_nodes[current] = {
                    "key": current,
                    "kind": "missing",
                    "name": None,
                    "source_id": None,
                    "step_id": None,
                    "detail": None,
                }
                continue
            closure.material_nodes[current] = {
                "key": node["key"],
                "source_id": node["source_id"],
                "sha256": node["sha256"],
                "step_id": node["step_id"],
                "kind": node["kind"],
                "name": node["name"],
                "detail": json.loads(node["json"]) if node["json"] else None,
            }
            closure.source_ids.add(node["source_id"])
            for edge in repo.conn.execute(
                "SELECT * FROM material_edges WHERE parent_key=? ORDER BY slot, ordinal",
                (current,),
            ):
                closure.material_edges.append(dict(edge))
                queue.append(edge["child_key"])

    # Project units referenced by the selected properties, per source/project.
    needed_project_units: set[tuple[str, int, str]] = set()
    for prop in closure.properties:
        detail = prop["detail"]
        project = detail.get("project_step_id")
        for measured in ((detail.get("declaration") or {}).get("interpretation") or {}).get(
            "measured", []
        ):
            if measured and measured.get("unit_source") == "project":
                needed_project_units.add((prop["source_id"], int(project or 0), measured["dimension"] or ""))
    for source_id, project_step, dimension in sorted(needed_project_units):
        row = repo.conn.execute(
            "SELECT * FROM project_units WHERE source_id=? AND project_step_id=? "
            "AND dimension=?",
            (source_id, project_step, dimension),
        ).fetchone()
        if row is not None:
            closure.project_units.append(
                {
                    "source_id": row["source_id"],
                    "project_step_id": row["project_step_id"],
                    "dimension": row["dimension"],
                    "unit_step_id": row["unit_step_id"],
                    "state": row["state"],
                    "detail": json.loads(row["json"]) if row["json"] else None,
                }
            )
            if row["unit_step_id"] is not None:
                closure.unit_step_ids.setdefault(source_id, set()).add(
                    int(row["unit_step_id"])
                )
                project_detail = (
                    json.loads(row["json"]) if row["json"] else None
                )
                embedded = None
                if isinstance(project_detail, dict):
                    embedded = project_detail.get("detail")
                closure.units.setdefault(
                    f"{source_id}:{row['unit_step_id']}",
                    {
                        "source_id": source_id,
                        "unit_step_id": row["unit_step_id"],
                        "unit_source": "project",
                        "unit": embedded,
                    },
                )

    # Failures touching selected objects or their relations.
    selected_keys = {row["key"] for row in object_rows} | set(closure.types)
    seen_failure: set[tuple[Any, ...]] = set()
    for source_id in closure.source_ids:
        for failure in repo.failures(source_id=source_id):
            touches = (
                failure["entity_key"] in selected_keys
                or failure["relation_key"] in closure.relation_keys
                or failure["category"] in {"duplicate_global_id", "spatial_cycle"}
            )
            if not touches:
                continue
            marker = (
                failure["category"],
                failure["entity_key"],
                failure["step_id"],
                failure["relation_key"],
                dump_json(failure["detail"]),
            )
            if marker in seen_failure:
                continue
            seen_failure.add(marker)
            closure.failures.append(failure)
    return closure


def _type_contributes(
    repo: Repository, object_row: dict[str, Any], properties: list[dict[str, Any]]
) -> bool:
    """Whether a type is needed by the object's exported evidence."""

    for prop in properties:
        if prop["detail"].get("type_declaration_ignored"):
            return True
        if prop["scope_used"] == "type":
            return True
    for assignment in repo.material_assignments(object_row["key"]):
        if assignment["tree"] and assignment["tree"].get("inherited_from_type"):
            return True
    return False


# ---------------------------------------------------------------------------
# Package writing
# ---------------------------------------------------------------------------

PACKAGE_DB_SCHEMA = """
CREATE TABLE objects (
    object_key TEXT PRIMARY KEY, source_id TEXT, step_id INTEGER, global_id TEXT,
    entity_type TEXT, category TEXT, name TEXT, type_key TEXT, usable INTEGER,
    conflict_state TEXT, detail_json TEXT
);
CREATE TABLE object_types (
    type_key TEXT PRIMARY KEY, source_id TEXT, step_id INTEGER, global_id TEXT,
    entity_type TEXT, name TEXT, detail_json TEXT
);
CREATE TABLE properties (
    object_key TEXT, identity TEXT, pset_name TEXT, prop_name TEXT, prop_kind TEXT,
    state TEXT, value_status TEXT, scope_used TEXT, is_conflict INTEGER,
    reason TEXT, detail_json TEXT,
    PRIMARY KEY(object_key, identity)
);
CREATE TABLE property_declarations (
    object_key TEXT, scope TEXT, source_id TEXT, step_id INTEGER, pset_key TEXT,
    pset_name TEXT, prop_name TEXT, prop_kind TEXT, signature TEXT,
    state TEXT, value_status TEXT, declaration_json TEXT
);
CREATE TABLE type_declarations (
    type_key TEXT, source_id TEXT, step_id INTEGER, pset_key TEXT, pset_name TEXT,
    prop_name TEXT, prop_kind TEXT, state TEXT, value_status TEXT, declaration_json TEXT
);
CREATE TABLE materials (
    material_key TEXT PRIMARY KEY, source_id TEXT, step_id INTEGER, kind TEXT,
    name TEXT, detail_json TEXT
);
CREATE TABLE material_edges (
    parent_key TEXT, child_key TEXT, ordinal INTEGER, slot TEXT, detail_json TEXT
);
CREATE TABLE object_materials (
    object_key TEXT, material_key TEXT, state TEXT, inherited_from_key TEXT,
    detail_json TEXT, PRIMARY KEY(object_key, material_key)
);
CREATE TABLE units (
    source_id TEXT, unit_step_id INTEGER, unit_source TEXT, unit_json TEXT,
    PRIMARY KEY(source_id, unit_step_id, unit_source)
);
CREATE TABLE project_units (
    source_id TEXT, project_step_id INTEGER, dimension TEXT, unit_step_id INTEGER,
    state TEXT, detail_json TEXT
);
CREATE TABLE relations (
    relation_key TEXT PRIMARY KEY, source_id TEXT, step_id INTEGER,
    relation_type TEXT, relating_key TEXT, state TEXT, detail_json TEXT
);
CREATE TABLE relation_edges (
    relation_key TEXT, kind TEXT, ordinal INTEGER, parent_key TEXT, child_key TEXT,
    edge_state TEXT, reason TEXT
);
CREATE TABLE external_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, from_key TEXT,
    relation_key TEXT, target_step_id INTEGER, target_kind TEXT, location TEXT,
    identification TEXT, name TEXT, state TEXT, detail_json TEXT
);
CREATE TABLE failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, category TEXT,
    severity TEXT, entity_key TEXT, step_id INTEGER, relation_key TEXT,
    detail_json TEXT
);
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY, sha256 TEXT, schema_name TEXT, object_count INTEGER,
    excerpt_path TEXT, header_text TEXT, status TEXT
);
CREATE TABLE excluded_objects (
    object_key TEXT PRIMARY KEY, source_id TEXT, step_id INTEGER, global_id TEXT,
    entity_type TEXT, name TEXT, reason TEXT, conflict_state TEXT, detail_json TEXT
);
CREATE TABLE excluded_properties (
    object_key TEXT, identity TEXT, pset_name TEXT, prop_name TEXT, prop_kind TEXT,
    state TEXT, value_status TEXT, reason TEXT, detail_json TEXT
);
CREATE TABLE manifest (
    key TEXT PRIMARY KEY, value TEXT
);
"""


def export_subset(
    repo: Repository,
    roots: Sequence[str],
    dest: str,
    *,
    label: str | None = None,
) -> dict[str, Any]:
    """Export a subset package.  Atomic; never replaces on failure."""

    dest = os.path.abspath(dest)
    selection = select_subset(repo, roots)
    closure = build_closure(repo, selection)
    parent = os.path.dirname(dest) or "."
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".ifc-handover-export-", dir=parent)
    try:
        summary = _write_package(repo, selection, closure, staging, label=label)
        _fsync_tree(staging)
        if os.path.exists(dest):
            if not _is_complete_package(dest):
                # Replacing an unknown/incomplete directory is still an
                # explicit user action; refuse instead of destroying data.
                raise ExportError(
                    f"destination {dest} exists and is not a complete handover package"
                )
            old = dest + ".old-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            os.replace(dest, old)
            try:
                os.replace(staging, dest)
            except BaseException:
                os.replace(old, dest)
                raise
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.replace(staging, dest)
        summary["package"] = dest
        return summary
    finally:
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def _write_package(
    repo: Repository,
    selection: Selection,
    closure: Closure,
    staging: str,
    *,
    label: str | None,
) -> dict[str, Any]:
    os.makedirs(staging, exist_ok=True)
    excerpts_dir = os.path.join(staging, "sources")
    os.makedirs(excerpts_dir, exist_ok=True)
    db_path = os.path.join(staging, "handover.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(PACKAGE_DB_SCHEMA)
        _write_objects(conn, closure, repo, selection)
        _write_properties(conn, repo, closure)
        _write_materials(conn, closure)
        _write_units(conn, closure)
        _write_relations(conn, repo, closure)
        _write_external_refs(conn, closure)
        _write_failures(conn, closure)
        excerpt_index = _write_excerpts(repo, closure, excerpts_dir)
        _write_sources(conn, repo, closure, excerpt_index)
        manifest = {
            "package_version": PACKAGE_VERSION,
            "label": label,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "roots": selection.roots,
            "selection": selection.as_json(),
            "summary": closure.as_summary(),
            "sources": excerpt_index,
            "tables": PACKAGE_TABLES,
        }
        conn.execute(
            "INSERT INTO manifest(key, value) VALUES('manifest', ?)",
            (dump_json(manifest),),
        )
        conn.commit()
    finally:
        conn.close()
    # Human-readable copies of the main business tables.
    tables_dir = os.path.join(staging, "tables")
    os.makedirs(tables_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for table in PACKAGE_TABLES:
            if table == "manifest":
                continue
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            _write_csv(os.path.join(tables_dir, f"{table}.csv"), rows)
            with open(
                os.path.join(tables_dir, f"{table}.json"), "w", encoding="utf-8"
            ) as fh:
                json.dump(rows, fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.write("\n")
    finally:
        conn.close()
    with open(os.path.join(staging, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(_readme(selection, closure, excerpt_index))
    return manifest


def _write_csv(path: str, rows: list[dict[str, Any]]) -> None:
    import csv

    with open(path, "w", encoding="utf-8", newline="") as fh:
        if not rows:
            fh.write("")
            return
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_value(v) for k, v in row.items()})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return dump_json(value)
    return value


def _write_objects(
    conn: sqlite3.Connection,
    closure: Closure,
    repo: Repository,
    selection: Selection,
) -> None:
    conn.executemany(
        "INSERT INTO objects(object_key, source_id, step_id, global_id, entity_type, "
        "category, name, type_key, usable, conflict_state, detail_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                row["key"],
                row["source_id"],
                row["step_id"],
                row["global_id"],
                row["entity_type"],
                row["category"],
                row["name"],
                row.get("type_key"),
                int(row["usable"]),
                row.get("conflict_state"),
                dump_json(
                    {
                        "object_type": row.get("object_type"),
                        "predefined_type": row.get("predefined_type"),
                        "attributes": row.get("attributes"),
                        "taxonomy": row.get("taxonomy"),
                        "selection_depth": None,
                    }
                ),
            )
            for row in closure.objects
        ],
    )
    # Excluded objects are not usable objects; they are retained as evidence
    # together with the properties that made them unusable.
    excluded_rows = []
    excluded_property_rows = []
    for item in selection.excluded:
        key = item.get("key")
        row = repo.get_object(key) if key else None
        if row is None:
            excluded_rows.append(
                (
                    key,
                    None,
                    None,
                    None,
                    None,
                    None,
                    item["reason"],
                    None,
                    dump_json(item),
                )
            )
            continue
        excluded_rows.append(
            (
                row["key"],
                row["source_id"],
                row["step_id"],
                row["global_id"],
                row["entity_type"],
                row["name"],
                item["reason"],
                row["conflict_state"],
                dump_json(
                    {
                        "conflict_detail": row.get("conflict_detail"),
                        "via_root": item.get("via_root"),
                        "attributes": row.get("attributes"),
                    }
                ),
            )
        )
        for prop in repo.properties(row["key"]):
            if prop["state"] not in {"conflict"} and not prop["is_conflict"]:
                continue
            excluded_property_rows.append(
                (
                    row["key"],
                    prop["identity"],
                    prop["pset_name"],
                    prop["prop_name"],
                    prop["prop_kind"],
                    prop["state"],
                    prop["value_status"],
                    prop["reason"],
                    dump_json(prop["detail"]),
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO excluded_objects(object_key, source_id, step_id, "
        "global_id, entity_type, name, reason, conflict_state, detail_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        excluded_rows,
    )
    conn.executemany(
        "INSERT INTO excluded_properties(object_key, identity, pset_name, prop_name, "
        "prop_kind, state, value_status, reason, detail_json) VALUES(?,?,?,?,?,?,?,?,?)",
        excluded_property_rows,
    )
    conn.executemany(
        "INSERT INTO object_types(type_key, source_id, step_id, global_id, entity_type, "
        "name, detail_json) VALUES(?,?,?,?,?,?,?)",
        [
            (
                row["key"],
                row["source_id"],
                row["step_id"],
                row["global_id"],
                row["entity_type"],
                row["name"],
                dump_json(
                    {
                        "attributes": row.get("attributes"),
                        "taxonomy": row.get("taxonomy"),
                    }
                ),
            )
            for row in closure.types.values()
        ],
    )


def _write_properties(conn: sqlite3.Connection, repo: Repository, closure: Closure) -> None:
    property_rows = []
    declaration_rows = []
    type_declaration_rows = []
    for prop in closure.properties:
        detail = prop["detail"]
        property_rows.append(
            (
                prop["object_key"],
                prop["identity"],
                prop["pset_name"],
                prop["prop_name"],
                prop["prop_kind"],
                prop["state"],
                prop["value_status"],
                prop["scope_used"],
                int(prop["is_conflict"]),
                prop["reason"],
                dump_json(detail),
            )
        )
        declaration = detail.get("declaration") or {}
        declaration_rows.append(
            (
                prop["object_key"],
                prop["scope_used"],
                prop["source_id"],
                declaration.get("prop_step_id"),
                None,
                prop["pset_name"],
                prop["prop_name"],
                prop["prop_kind"],
                None,
                prop["state"],
                prop["value_status"],
                dump_json(declaration),
            )
        )
    for type_key in closure.types:
        for prop in repo.properties(type_key):
            type_declaration_rows.append(
                (
                    type_key,
                    prop["source_id"],
                    (prop["detail"].get("declaration") or {}).get("prop_step_id"),
                    None,
                    prop["pset_name"],
                    prop["prop_name"],
                    prop["prop_kind"],
                    prop["state"],
                    prop["value_status"],
                    dump_json(prop["detail"]),
                )
            )
    conn.executemany(
        "INSERT INTO properties(object_key, identity, pset_name, prop_name, prop_kind, "
        "state, value_status, scope_used, is_conflict, reason, detail_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        property_rows,
    )
    conn.executemany(
        "INSERT INTO property_declarations(object_key, scope, source_id, step_id, "
        "pset_key, pset_name, prop_name, prop_kind, signature, state, value_status, "
        "declaration_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        declaration_rows,
    )
    conn.executemany(
        "INSERT INTO type_declarations(type_key, source_id, step_id, pset_key, "
        "pset_name, prop_name, prop_kind, state, value_status, declaration_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        type_declaration_rows,
    )


def _write_materials(conn: sqlite3.Connection, closure: Closure) -> None:
    conn.executemany(
        "INSERT INTO materials(material_key, source_id, step_id, kind, name, detail_json) "
        "VALUES(?,?,?,?,?,?)",
        [
            (
                node["key"],
                node["source_id"],
                node["step_id"],
                node["kind"],
                node["name"],
                dump_json(node["detail"]),
            )
            for node in closure.material_nodes.values()
        ],
    )
    conn.executemany(
        "INSERT INTO object_materials(object_key, material_key, state, inherited_from_key, "
        "detail_json) VALUES(?,?,?,?,?)",
        [
            (
                row["object_key"],
                row["material_key"],
                row["state"],
                row["inherited_from_key"],
                dump_json(row["detail"]),
            )
            for row in closure.object_materials
        ],
    )
    conn.executemany(
        "INSERT INTO material_edges(parent_key, child_key, ordinal, slot, detail_json) "
        "VALUES(?,?,?,?,?)",
        [
            (
                edge["parent_key"],
                edge["child_key"],
                edge["ordinal"],
                edge["slot"],
                edge["json"],
            )
            for edge in closure.material_edges
        ],
    )


def _write_units(conn: sqlite3.Connection, closure: Closure) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO units(source_id, unit_step_id, unit_source, unit_json) "
        "VALUES(?,?,?,?)",
        [
            (
                entry["source_id"],
                entry["unit_step_id"],
                entry["unit_source"],
                dump_json(entry["unit"]),
            )
            for entry in closure.units.values()
        ],
    )
    conn.executemany(
        "INSERT INTO project_units(source_id, project_step_id, dimension, unit_step_id, "
        "state, detail_json) VALUES(?,?,?,?,?,?)",
        [
            (
                row["source_id"],
                row["project_step_id"],
                row["dimension"],
                row["unit_step_id"],
                row["state"],
                dump_json(row["detail"]),
            )
            for row in closure.project_units
        ],
    )


def _write_relations(conn: sqlite3.Connection, repo: Repository, closure: Closure) -> None:
    relation_rows = []
    edge_rows = []
    for relation_key in sorted(closure.relation_keys):
        row = repo.conn.execute(
            "SELECT * FROM relations WHERE relation_key=?", (relation_key,)
        ).fetchone()
        if row is None:
            continue
        relation_rows.append(
            (
                row["relation_key"],
                row["source_id"],
                row["step_id"],
                row["relation_type"],
                row["relating_key"],
                row["state"],
                row["json"],
            )
        )
        for edge in repo.relation_edges(relation_key):
            edge_rows.append(
                (
                    edge["relation_key"],
                    edge["kind"],
                    edge["ordinal"],
                    edge["parent_key"],
                    edge["child_key"],
                    edge["edge_state"],
                    edge["reason"],
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO relations(relation_key, source_id, step_id, "
        "relation_type, relating_key, state, detail_json) VALUES(?,?,?,?,?,?,?)",
        relation_rows,
    )
    conn.executemany(
        "INSERT INTO relation_edges(relation_key, kind, ordinal, parent_key, child_key, "
        "edge_state, reason) VALUES(?,?,?,?,?,?,?)",
        edge_rows,
    )


def _write_external_refs(conn: sqlite3.Connection, closure: Closure) -> None:
    conn.executemany(
        "INSERT INTO external_references(source_id, from_key, relation_key, "
        "target_step_id, target_kind, location, identification, name, state, detail_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            (
                ref["source_id"],
                ref["from_key"],
                ref["relation_key"],
                ref["target_step_id"],
                ref["target_kind"],
                ref["location"],
                ref["identification"],
                ref["name"],
                ref["state"],
                dump_json(ref["detail"]),
            )
            for ref in closure.external_references
        ],
    )


def _write_failures(conn: sqlite3.Connection, closure: Closure) -> None:
    conn.executemany(
        "INSERT INTO failures(source_id, category, severity, entity_key, step_id, "
        "relation_key, detail_json) VALUES(?,?,?,?,?,?,?)",
        [
            (
                failure["source_id"],
                failure["category"],
                failure["severity"],
                failure["entity_key"],
                failure["step_id"],
                failure["relation_key"],
                dump_json(failure["detail"]),
            )
            for failure in closure.failures
        ],
    )


def _write_excerpts(
    repo: Repository, closure: Closure, excerpts_dir: str
) -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    for source_id in sorted(closure.source_ids):
        source = repo.source(source_id)
        if source is None:
            continue
        stored = StoredSource(repo, source_id)
        step = stored.step()
        if step is None:
            raise ExportError(
                f"raw source bytes for {source_id!r} are unavailable; re-ingest "
                "with a source store before exporting (the package must contain "
                "the source lines behind its business rows)"
            )
        step_ids: set[int] = set()
        step_ids.update(closure.object_step_ids.get(source_id, set()))
        step_ids.update(closure.declaration_step_ids.get(source_id, set()))
        step_ids.update(closure.unit_step_ids.get(source_id, set()))
        for node in closure.material_nodes.values():
            if node.get("source_id") == source_id and node.get("step_id"):
                step_ids.add(int(node["step_id"]))
        # Resolve nested unit/measure-with-unit entities so an excerpt's unit
        # evidence is readable without the original file.
        for unit_step in list(closure.unit_step_ids.get(source_id, set())):
            step_ids.update(stored.unit_closure_ids(int(unit_step)))
        # Relations and property-set containers are useful context.
        for relation_key in closure.relation_keys:
            row = repo.conn.execute(
                "SELECT source_id, step_id FROM relations WHERE relation_key=?",
                (relation_key,),
            ).fetchone()
            if row is not None and row["source_id"] == source_id:
                step_ids.add(int(row["step_id"]))
                entity = step.entities.get(int(row["step_id"]))
                if entity is not None:
                    step_ids.update(step.refs(int(row["step_id"])))
        for prop in closure.properties:
            if prop["source_id"] != source_id:
                continue
            # Include property set containers even if they did not contribute
            # a resolving declaration (e.g. the whole group was overridden).
            for source in prop["detail"].get("type_declaration_ignored", {}).get(
                "sources", []
            ):
                try:
                    pset_step = int(str(source.get("pset_key", "")).rsplit(":", 1)[-1])
                except (ValueError, IndexError):
                    pset_step = None
                if pset_step and pset_step in step.entities:
                    step_ids.add(pset_step)
            for source in prop["detail"].get("sources", []):
                prop_step = int(source["step_id"])
                step_ids.add(prop_step)
                # The containing property set line, in the form pset:<sha>:<step>.
                pset_key = str(source.get("pset_key") or "")
                try:
                    pset_step = int(pset_key.rsplit(":", 1)[-1])
                except (ValueError, IndexError):
                    pset_step = None
                if pset_step is not None and pset_step in step.entities:
                    step_ids.add(pset_step)
                    # Include unit/enumeration references of the property.
                    for ref in step.refs(prop_step):
                        entity = step.entities.get(ref)
                        if entity is not None and entity.type_name in {
                            "IFCSIUNIT",
                            "IFCCONVERSIONBASEDUNIT",
                            "IFCCONVERSIONBASEDUNITWITHOFFSET",
                            "IFCDERIVEDUNIT",
                            "IFCCONTEXTDEPENDENTUNIT",
                            "IFCMEASUREWITHUNIT",
                            "IFCDERIVEDUNITELEMENT",
                            "IFCPROPERTYENUMERATION",
                        }:
                            step_ids.add(ref)
        step_ids = {
            sid for sid in step_ids if sid in step.entities
        }
        path = os.path.join(excerpts_dir, f"{safe_file_component(source_id)}.ifc.excerpt")
        text = build_excerpt_ifc(
            step,
            step_ids,
            source_note=f"source_id={source_id} sha256={source.get('sha256')}",
        )
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        index.append(
            {
                "source_id": source_id,
                "sha256": source.get("sha256"),
                "schema_name": source.get("schema_name"),
                "excerpt_path": os.path.relpath(path, os.path.dirname(excerpts_dir)),
                "step_ids": sorted(step_ids),
                "locators": _locators(step, step_ids),
            }
        )
    return index


def _locators(step: StepFile, step_ids: Iterable[int]) -> dict[str, list[int]]:
    return {
        str(sid): [step.entities[sid].line_start, step.entities[sid].line_end]
        for sid in sorted(step_ids)
        if sid in step.entities
    }


def _write_sources(
    conn: sqlite3.Connection,
    repo: Repository,
    closure: Closure,
    excerpt_index: list[dict[str, Any]],
) -> None:
    by_source = {entry["source_id"]: entry for entry in excerpt_index}
    rows = []
    for source_id in sorted(closure.source_ids):
        source = repo.source(source_id) or {}
        entry = by_source.get(source_id, {})
        header_row = repo.conn.execute(
            "SELECT header_text FROM source_headers WHERE source_id=?", (source_id,)
        ).fetchone()
        header_text = header_row["header_text"] if header_row else None
        rows.append(
            (
                source_id,
                source.get("sha256"),
                source.get("schema_name"),
                len(closure.object_step_ids.get(source_id, ())),
                entry.get("excerpt_path"),
                header_text,
                source.get("status"),
            )
        )
    conn.executemany(
        "INSERT INTO sources(source_id, sha256, schema_name, object_count, excerpt_path, "
        "header_text, status) VALUES(?,?,?,?,?,?,?)",
        rows,
    )


def _fsync_tree(root: str) -> None:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                os.fsync(fh.fileno())
        try:
            fd = os.open(dirpath, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _is_complete_package(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    db_path = os.path.join(path, "handover.sqlite")
    manifest_path = os.path.join(path, "README.md")
    if not (os.path.isfile(db_path) and os.path.isfile(manifest_path)):
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM manifest WHERE key='manifest'"
            ).fetchone()
            if row is None:
                return False
            data = json.loads(row[0])
            return int(data.get("package_version", 0)) == PACKAGE_VERSION
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def _readme(selection: Selection, closure: Closure, excerpts: list[dict[str, Any]]) -> str:
    lines = [
        "# IFC handover subset",
        "",
        "This package is self-contained: it needs no access to the original IFC",
        "file paths and records only external targets for external references.",
        "",
        "## Selection",
        "",
        f"- spatial roots: {', '.join(selection.roots)}",
        f"- objects selected: {len(closure.objects)}",
        "- traversal: decomposition and containment edges only, downward from the roots",
        "",
        "## Contents",
        "",
        "- `handover.sqlite` — queryable business tables",
        "- `tables/*.csv` and `tables/*.json` — human-readable copies",
        "- `sources/*.ifc.excerpt` — exact STEP lines that back the business rows",
        "- `manifest` table — package metadata and per-source locators",
        "",
        "## Sources",
        "",
    ]
    for entry in excerpts:
        lines.append(
            f"- `{entry['source_id']}` sha256=`{entry['sha256']}` "
            f"excerpt=`{entry['excerpt_path']}`"
        )
    lines += [
        "",
        "## Definitions included",
        "",
    ]
    for key, row in sorted(closure.types.items()):
        lines.append(f"- type `{row['entity_type']}` {row.get('name') or ''} (`{key}`)")
    for key, node in sorted(closure.material_nodes.items()):
        lines.append(f"- material {node['kind']} `{node.get('name')}` (`{key}`)")
    if selection.excluded:
        lines += ["", "## Excluded (evidence)", ""]
        for item in selection.excluded:
            lines.append(f"- `{item['key']}`: {item['reason']}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Re-open a package
# ---------------------------------------------------------------------------


class HandoverPackage:
    """Read-only view of an exported package, independent of the repository."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        db_path = os.path.join(self.path, "handover.sqlite")
        if not os.path.isfile(db_path):
            raise FileNotFoundError(f"not a handover package: {self.path}")
        self.db_path = db_path
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "HandoverPackage":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def manifest(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT value FROM manifest WHERE key='manifest'"
        ).fetchone()
        return {} if row is None else json.loads(row[0])

    def objects(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM objects ORDER BY object_key")]

    def properties(self, object_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM properties WHERE object_key=? ORDER BY pset_name, prop_name",
            (object_key,),
        )
        out = []
        for row in rows:
            data = dict(row)
            data["detail"] = json.loads(data.pop("detail_json"))
            out.append(data)
        return out

    def property(
        self, object_key: str, pset_name: str, prop_name: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM properties WHERE object_key=? AND pset_name=? AND prop_name=?",
            (object_key, pset_name, prop_name),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["detail"] = json.loads(data.pop("detail_json"))
        return data

    def materials(self, object_key: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT m.* FROM materials m"
        params: tuple[Any, ...] = ()
        if object_key is not None:
            sql += (
                " JOIN object_materials om ON om.material_key = m.material_key"
                " WHERE om.object_key = ?"
            )
            params = (object_key,)
        sql += " ORDER BY m.material_key"
        return [
            {
                "key": r["material_key"],
                "source_id": r["source_id"],
                "step_id": r["step_id"],
                "kind": r["kind"],
                "name": r["name"],
                "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
            }
            for r in self.conn.execute(sql, params)
        ]

    def object_material_links(self, object_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM object_materials WHERE object_key=?", (object_key,)
        )
        return [
            {
                "object_key": r["object_key"],
                "material_key": r["material_key"],
                "state": r["state"],
                "inherited_from_key": r["inherited_from_key"],
                "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
            }
            for r in rows
        ]

    def material_tree(self, material_key: str) -> dict[str, Any]:
        """Ordered tree rooted at a material definition."""

        nodes = {
            r["material_key"]: {
                "key": r["material_key"],
                "kind": r["kind"],
                "name": r["name"],
                "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
                "children": [],
            }
            for r in self.conn.execute("SELECT * FROM materials")
        }
        for edge in self.conn.execute(
            "SELECT * FROM material_edges ORDER BY slot, ordinal"
        ):
            parent = nodes.get(edge["parent_key"])
            child = nodes.get(edge["child_key"])
            if parent is None or child is None:
                continue
            parent["children"].append(
                {
                    "slot": edge["slot"],
                    "ordinal": edge["ordinal"],
                    "detail": json.loads(edge["detail_json"]) if edge["detail_json"] else None,
                    "material": child,
                }
            )
        return nodes.get(material_key) or {}

    def excerpt_path(self, source_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT excerpt_path FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if row is None or row["excerpt_path"] is None:
            return None
        return os.path.join(self.path, row["excerpt_path"])

    def table(self, name: str) -> list[dict[str, Any]]:
        if name not in PACKAGE_TABLES:
            raise ValueError(f"unknown package table {name!r}")
        return [dict(r) for r in self.conn.execute(f"SELECT * FROM {name}")]
