"""Per-source ingestion into the offline repository.

Pipeline for one IFC source:

1. Hash the bytes, compare with the manifest, and (optionally) copy the source
   into a content-addressed store.
2. Parse the STEP text for exact source spellings and every reference, and let
   IfcOpenShell provide the schema-aware inventory.  Both views are scoped to
   this one source and released before the next source is opened.
3. Materialise roots, non-root nodes, hierarchy/type/property relations,
   materials (with hierarchy and order), units, property declarations and
   failure evidence in SQLite.
4. Interpret declarations per object using the object's project units.
5. Resolve instance-vs-type properties: an instance declaration overrides the
   same identity from its type, including explicit null; unmatched type
   declarations fill in.  A conflicting group at either level becomes
   ``conflict``; an instance group still masks a type conflict while retaining
   it as evidence.
"""

from __future__ import annotations

import hashlib
import json as _json
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import ifcopenshell

from . import schema as schema_tools
from .keys import conflicting_root_key, root_key, step_scoped_key
from .step import ListValue, Ref, StepFile, StringValue
from .store import Repository, dump_json
from .units import UnitResolver, ValueInterpreter
from .values import (
    STATE_CONFLICT,
    STATE_VALUE,
    VALUE_CONFLICT,
    PropertyReader,
    RawDeclaration,
    declaration_signature_key,
    interpret_declaration,
)

HIERARCHY_KINDS = ("decomposition", "containment")


class IngestionError(Exception):
    pass


@dataclass
class SourceManifestEntry:
    source_id: str
    path: str
    sha256: str


@dataclass
class IngestResult:
    source_id: str
    sha256: str
    status: str
    objects: int = 0
    types: int = 0
    properties: int = 0
    failures: int = 0
    error: str | None = None


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: str) -> list[SourceManifestEntry]:
    """Read a JSON manifest: a list or ``{"sources": [...]}``."""

    import json

    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError("manifest must be a list or contain a 'sources' list")
    out: list[SourceManifestEntry] = []
    seen: set[str] = set()
    base = os.path.dirname(os.path.abspath(path))
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"manifest entry {i} is not an object")
        source_id = entry.get("source_id")
        file_path = entry.get("path") or entry.get("file")
        digest = entry.get("sha256")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError(f"manifest entry {i} has no source_id")
        if not isinstance(file_path, str) or not file_path:
            raise ValueError(f"manifest entry {i} has no path")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"manifest entry {i} has no SHA-256")
        if source_id in seen:
            raise ValueError(f"duplicate source_id {source_id!r} in manifest")
        seen.add(source_id)
        resolved = file_path
        if not os.path.isabs(resolved):
            resolved = os.path.join(base, resolved)
        out.append(
            SourceManifestEntry(
                source_id=source_id,
                path=os.path.abspath(resolved),
                sha256=digest.lower(),
            )
        )
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if value is None:
        return None
    return str(value)


def _enum_name(value: Any) -> str | None:
    from .step import EnumValue

    return value.name if isinstance(value, EnumValue) else None


def _raw(value: Any) -> Any:
    from .step import NumberValue

    if isinstance(value, NumberValue):
        return {"raw": value.raw, "decimal": str(value.value)}
    if isinstance(value, StringValue):
        return value.value
    if isinstance(value, EnumValue):
        return value.name
    if isinstance(value, Ref):
        return {"step_id": value.step_id}
    if isinstance(value, ListValue):
        return [_raw(v) for v in value.items]
    if value is None:
        return None
    return repr(value)


@dataclass
class SourceContext:
    """Everything needed to resolve one source, held only during ingestion."""

    repo: Repository
    manifest: SourceManifestEntry
    sha: str
    schema_name: str
    step: StepFile
    file: Any
    resolver: UnitResolver
    reader: PropertyReader
    store_path: str | None
    failures: list[dict[str, Any]] = field(default_factory=list)
    key_by_step: dict[int, str] = field(default_factory=dict)
    root_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    node_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    relations: list[dict[str, Any]] = field(default_factory=list)
    psets: list[dict[str, Any]] = field(default_factory=list)
    declarations: list[dict[str, Any]] = field(default_factory=list)
    declarations_by_pset: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    object_pset_links: list[dict[str, Any]] = field(default_factory=list)
    type_links: dict[str, str] = field(default_factory=dict)
    project_units: dict[int, dict[str, dict[str, Any]]] = field(default_factory=dict)
    project_of_object: dict[str, int] = field(default_factory=dict)
    material_nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    material_edges: list[dict[str, Any]] = field(default_factory=list)
    material_assignments: list[dict[str, Any]] = field(default_factory=list)
    external_refs: list[dict[str, Any]] = field(default_factory=list)
    resolved: list[dict[str, Any]] = field(default_factory=list)
    # Resolved rows are streamed to a temporary table instead of being kept in
    # memory; the list above is only a small batching buffer.
    stream_conn: Any = None
    stream_table: str = "resolved_properties_stage"
    stream_buffer: list[dict[str, Any]] = field(default_factory=list)
    stream_count: int = 0
    source_entity_buffer: list[tuple[Any, ...]] = field(default_factory=list)
    source_entity_count: int = 0
    gid_groups: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    archived: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))
    counts: dict[str, int] = field(
        default_factory=lambda: {"objects": 0, "types": 0, "properties": 0}
    )
    project_roots: list[str] = field(default_factory=list)
    spatial_roots: list[str] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)

    def fail(
        self,
        category: str,
        severity: str,
        detail: dict[str, Any],
        *,
        step_id: int | None = None,
        entity_key: str | None = None,
        relation_key: str | None = None,
    ) -> None:
        self.failures.append(
            {
                "category": category,
                "severity": severity,
                "entity_key": entity_key,
                "step_id": step_id,
                "relation_key": relation_key,
                "detail": detail,
            }
        )

    def stage_resolved(self, record: dict[str, Any]) -> None:
        """Append one resolved property to the bounded staging buffer."""

        self.stream_buffer.append(record)
        if len(self.stream_buffer) >= 200:
            self.flush_resolved()

    def flush_resolved(self) -> None:
        if not self.stream_buffer:
            return
        if self.stream_conn is None:
            raise IngestionError("resolved-property staging is not initialised")
        rows = [
            (
                r["object_key"],
                r["source_id"],
                r["pset_name"],
                r["prop_name"],
                r["prop_kind"],
                r["identity"],
                r["state"],
                r["value_status"],
                r["scope_used"],
                int(r["is_conflict"]),
                r["reason"],
                dump_json(r["json"]),
            )
            for r in self.stream_buffer
        ]
        self.stream_conn.executemany(
            "INSERT INTO resolved_properties_stage(object_key, source_id, pset_name, "
            "prop_name, prop_kind, identity, state, value_status, scope_used, "
            "is_conflict, reason, json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.stream_count += len(rows)
        self.stream_buffer.clear()

    def stage_source_entity(self, row: tuple[Any, ...]) -> None:
        self.source_entity_buffer.append(row)
        if len(self.source_entity_buffer) >= 1000:
            self.flush_source_entities()

    def flush_source_entities(self) -> None:
        if not self.source_entity_buffer:
            return
        if self.stream_conn is None:
            raise IngestionError("source-entity staging is not initialised")
        self.stream_conn.executemany(
            "INSERT INTO source_entities(source_id, sha256, step_id, entity_type, role, "
            "line_start, line_end, raw_text, business_ref) VALUES(?,?,?,?,?,?,?,?,?)",
            self.source_entity_buffer,
        )
        self.source_entity_count += len(self.source_entity_buffer)
        self.source_entity_buffer.clear()

    def archive(self, step_id: int, role: str) -> None:
        self.archived[step_id].add(role)

    def key_for(self, step_id: int) -> str | None:
        return self.key_by_step.get(step_id)

    def relation_key(self, step_id: int) -> str:
        return f"rel:{self.sha}:{step_id}"

    def pset_key(self, step_id: int) -> str:
        return f"pset:{self.sha}:{step_id}"

    def material_key(self, step_id: int) -> str:
        return f"mat:{self.sha}:{step_id}"


class SourceIngestor:
    """Ingest one IFC source into an open repository."""

    def __init__(
        self,
        repo: Repository,
        entry: SourceManifestEntry,
        *,
        store_dir: str | None = None,
        strict_hash: bool = True,
    ):
        self.repo = repo
        self.entry = entry
        self.source_id = entry.source_id
        self.store_dir = store_dir
        self.strict_hash = strict_hash
        self.ctx: SourceContext | None = None

    # -- entry point -------------------------------------------------------
    def run(self) -> IngestResult:
        started = _now()
        sha = ""
        try:
            sha = sha256_file(self.entry.path)
            if self.strict_hash and sha != self.entry.sha256:
                raise IngestionError(
                    f"SHA-256 mismatch for {self.source_id}: manifest "
                    f"{self.entry.sha256}, file {sha}"
                )
            step = StepFile.from_path(self.entry.path)
            try:
                file = ifcopenshell.open(self.entry.path)
            except Exception as exc:
                raise IngestionError(
                    f"IfcOpenShell could not open {self.entry.path}: {exc}"
                ) from exc
            resolver = UnitResolver(step)
            reader = PropertyReader(step, file.schema, resolver)
            store_path = self._store_source()
            self.ctx = SourceContext(
                repo=self.repo,
                manifest=self.entry,
                sha=sha,
                schema_name=file.schema,
                step=step,
                file=file,
                resolver=resolver,
                reader=reader,
                store_path=store_path,
            )
            # A dedicated connection owns the long-lived write transaction for
            # this source.  Rows that are many-per-source (resolved properties
            # and source excerpts) are streamed into temporary tables through a
            # bounded buffer, so ingestion memory does not grow with the number
            # of properties.
            conn = self.repo.conn
            conn.execute("BEGIN")
            conn.execute("DROP TABLE IF EXISTS resolved_properties_stage")
            conn.execute(
                "CREATE TEMP TABLE resolved_properties_stage("
                "object_key TEXT, source_id TEXT, pset_name TEXT, prop_name TEXT, "
                "prop_kind TEXT, identity TEXT, state TEXT, value_status TEXT, "
                "scope_used TEXT, is_conflict INTEGER, reason TEXT, json TEXT)"
            )
            self.ctx.stream_conn = conn
            self._collect_roots()
            self._collect_nodes()
            self._collect_project_units()
            self._collect_relations()
            self._collect_psets()
            self._resolve_properties()
            self._collect_materials()
            self._inherit_type_materials()
            self._collect_external_references()
            self._collect_cycles()
            self.ctx.flush_resolved()
            self.ctx.flush_source_entities()
            self._write(started)
            return IngestResult(
                source_id=self.source_id,
                sha256=sha,
                status="ok",
                objects=self.ctx.counts["objects"],
                types=self.ctx.counts["types"],
                properties=self.ctx.counts["properties"],
                failures=len(self.ctx.failures),
            )
        except Exception as exc:
            try:
                self.repo.conn.rollback()
            except Exception:
                pass
            return self._fail_source(started, sha, exc)
        finally:
            if self.ctx is not None:
                self.ctx.file = None
                self.ctx.step = None
                self.ctx.stream_conn = None
                self.ctx.stream_buffer.clear()
                self.ctx.source_entity_buffer.clear()
            self.ctx = None

    def _store_source(self) -> str:
        """Copy the exact source bytes into the repository's source store.

        Export must include the raw source lines that back each business row.
        Keeping a copy next to the repository (by default
        ``<repository>.sources/<source_id>/source.ifc``) means later export does
        not depend on the contractor's original path.
        """

        store_dir = self.store_dir or (self.repo.path + ".sources")
        assert store_dir
        dest_dir = os.path.join(store_dir, self.source_id)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "source.ifc")
        tmp = dest + ".tmp"
        shutil.copyfile(self.entry.path, tmp)
        os.replace(tmp, dest)
        return dest

    def _fail_source(self, started: str, sha: str, exc: Exception) -> IngestResult:
        message = str(exc)
        failures = list(self.ctx.failures) if self.ctx else []
        failures.append(
            {
                "category": "ingestion_error",
                "severity": "error",
                "detail": {"message": message, "type": type(exc).__name__},
            }
        )
        with self.repo.transaction() as conn:
            self.repo.delete_source_rows(conn, self.source_id)
            conn.execute(
                "INSERT INTO sources(source_id, declared_sha256, sha256, file_name, "
                "stored_path, schema_name, status, error, ingested_at, stats_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    self.source_id,
                    self.entry.sha256,
                    sha or None,
                    os.path.basename(self.entry.path),
                    None,
                    None,
                    "failed",
                    message,
                    started,
                    dump_json({"failures": len(failures)}),
                ),
            )
            for failure in failures:
                self._insert_failure(conn, failure, sha or None)
        return IngestResult(
            source_id=self.source_id,
            sha256=sha,
            status="failed",
            error=message,
            failures=len(failures),
        )

    # -- roots --------------------------------------------------------------
    def _collect_roots(self) -> None:
        ctx = self.ctx
        assert ctx is not None
        pending: list[dict[str, Any]] = []
        for entity in ctx.file.by_type("IfcRoot", include_subtypes=True):
            step_id = entity.id()
            if step_id not in ctx.step.entities:
                ctx.fail(
                    "unparsed_entity",
                    "error",
                    {"step_id": step_id, "entity_type": entity.is_a()},
                    step_id=step_id,
                )
                continue
            entity_type = entity.is_a()
            gid = _text(getattr(entity, "GlobalId", None)) or None
            expected = len(schema_tools.all_attribute_names(ctx.schema_name, entity_type))
            found = len(ctx.step.args(step_id))
            if expected and found != expected:
                ctx.fail(
                    "attribute_count_mismatch",
                    "error",
                    {
                        "step_id": step_id,
                        "entity_type": entity_type,
                        "found": found,
                        "expected": expected,
                    },
                    step_id=step_id,
                )
            taxonomy = schema_tools.type_chain(ctx.schema_name, entity_type)
            is_object = schema_tools.is_a(ctx.schema_name, entity_type, "IfcObject")
            is_spatial = schema_tools.is_a(ctx.schema_name, entity_type, "IfcSpatialElement")
            is_type = schema_tools.is_a(ctx.schema_name, entity_type, "IfcTypeObject")
            is_product = schema_tools.is_a(ctx.schema_name, entity_type, "IfcProduct")
            category = self._category(entity_type)
            row = {
                "key": None,
                "source_id": self.source_id,
                "sha256": ctx.sha,
                "step_id": step_id,
                "global_id": gid,
                "key_kind": "gid" if gid else "step",
                "entity_type": entity_type,
                "category": category,
                "name": _text(getattr(entity, "Name", None)),
                "object_type": _text(getattr(entity, "ObjectType", None)),
                "predefined_type": _text(getattr(entity, "PredefinedType", None)),
                "is_object": bool(is_object),
                "is_spatial": bool(is_spatial),
                "is_type": bool(is_type),
                "is_product": bool(is_product),
                "is_definition_root": True,
                "taxonomy": taxonomy,
                "attributes": self._entity_attributes(step_id, entity_type),
                "type_key": None,
                "conflict_state": None,
                "conflict_detail": None,
            }
            pending.append(row)
            if gid:
                ctx.gid_groups[gid].append(step_id)
            ctx.archive(step_id, "type" if is_type else "root")
            if is_type:
                ctx.counts["types"] += 1
            elif is_object:
                ctx.counts["objects"] += 1
        duplicated = {
            gid for gid, step_ids in ctx.gid_groups.items() if len(step_ids) > 1
        }
        for row in pending:
            gid = row["global_id"]
            step_id = row["step_id"]
            if gid and gid in duplicated:
                key = conflicting_root_key(self.source_id, gid, step_id)
                row["key"] = key
                row["business_key"] = root_key(
                    self.source_id, gid, ctx.sha, step_id
                )
                row["conflict_state"] = "duplicate_global_id"
            else:
                key = root_key(self.source_id, gid, ctx.sha, step_id)
                row["key"] = key
            ctx.key_by_step[step_id] = key
            ctx.root_rows[key] = row
            if row["category"] == "project":
                ctx.project_roots.append(key)
            if row["is_spatial"]:
                ctx.spatial_roots.append(key)
        for gid, step_ids in ctx.gid_groups.items():
            if len(step_ids) < 2:
                continue
            keys = [ctx.key_by_step[step_id] for step_id in step_ids]
            business_key = root_key(self.source_id, gid, ctx.sha, step_ids[0])
            detail = {
                "global_id": gid,
                "step_ids": sorted(step_ids),
                "keys": keys,
                "business_key": business_key,
                "message": (
                    "the same GlobalId occurs for distinct entities; both are "
                    "retained as unusable conflict evidence and are not merged"
                ),
            }
            ctx.fail(
                "duplicate_global_id",
                "error",
                detail,
                entity_key=keys[0],
                step_id=step_ids[0],
            )
            for key in keys:
                ctx.root_rows[key]["conflict_detail"] = detail

    def _category(self, entity_type: str) -> str:
        if entity_type == "IfcProject":
            return "project"
        if entity_type in {
            "IfcSite",
            "IfcBuilding",
            "IfcBuildingStorey",
            "IfcSpace",
            "IfcSpatialZone",
            "IfcExternalSpatialElement",
        }:
            return "spatial"
        return "object"

    def _entity_attributes(self, step_id: int, entity_type: str) -> dict[str, Any]:
        ctx = self.ctx
        assert ctx is not None
        names = schema_tools.all_attribute_names(ctx.schema_name, entity_type)
        args = ctx.step.args(step_id)
        return {
            name: _raw(args[i] if i < len(args) else None)
            for i, name in enumerate(names)
        }

    # -- non-root nodes -----------------------------------------------------
    def _collect_nodes(self) -> None:
        """Index non-IfcRoot entities that relationships point at."""

        ctx = self.ctx
        assert ctx is not None
        for entity in ctx.file:
            step_id = entity.id()
            if step_id in ctx.key_by_step:
                continue
            entity_type = entity.is_a()
            key = step_scoped_key(self.source_id, ctx.sha, step_id)
            if key in ctx.node_rows:
                continue
            ctx.node_rows[key] = {
                "key": key,
                "source_id": self.source_id,
                "sha256": ctx.sha,
                "step_id": step_id,
                "entity_type": entity_type,
                "name": _text(getattr(entity, "Name", None)),
                "taxonomy": schema_tools.type_chain(ctx.schema_name, entity_type),
                "attributes": {},
            }
            ctx.key_by_step.setdefault(step_id, key)

    # -- units --------------------------------------------------------------
    def _collect_project_units(self) -> None:
        ctx = self.ctx
        assert ctx is not None
        for project in ctx.file.by_type("IfcProject"):
            units: dict[str, dict[str, Any]] = {}
            assignment = getattr(project, "UnitsInContext", None)
            if assignment is not None:
                assignment_id = assignment.id()
                ctx.archive(assignment_id, "unit_assignment")
                args = ctx.step.args(assignment_id)
                unit_list = args[0] if args else None
                refs = unit_list.items if isinstance(unit_list, ListValue) else ()
                for ordinal, ref in enumerate(refs):
                    if not isinstance(ref, Ref):
                        continue
                    ctx.archive(ref.step_id, "unit")
                    unit = ctx.resolver.resolve(ref.step_id)
                    ctx.resolver.prefetch(ref.step_id)
                    if not unit.dimension:
                        continue
                    entry = {
                        "unit_step_id": ref.step_id,
                        "source": "project",
                        "assignment_step_id": assignment_id,
                        "ordinal": ordinal,
                        "state": "ok" if unit.scale is not None else "unavailable",
                        "detail": unit.as_json(),
                    }
                    if unit.dimension not in units:
                        units[unit.dimension] = entry
                    else:
                        units[unit.dimension].setdefault("additional_units", []).append(
                            entry
                        )
            ctx.project_units[project.id()] = units
            self._map_objects_to_project(project.id(), units)

    def _map_objects_to_project(self, project_id: int, units: dict[str, Any]) -> None:
        """Assign project units to every object via decomposition/inheritance.

        IfcRelAggregates is present for most files; objects that are not
        aggregated inherit the units of the project that owns their spatial
        ancestors.  If multiple projects exist, the containment/aggregation walk
        determines ownership; unowned objects use the first project and are
        flagged when ambiguous.
        """

        ctx = self.ctx
        assert ctx is not None
        # Build decomposition adjacency from raw aggregates.
        children: dict[int, list[int]] = defaultdict(list)
        for entity in ctx.file.by_type("IfcRelAggregates"):
            parent = getattr(entity, "RelatingObject", None)
            if parent is None:
                continue
            for child in getattr(entity, "RelatedObjects", None) or ():
                children[parent.id()].append(child.id())
        for entity in ctx.file.by_type("IfcRelContainedInSpatialStructure"):
            parent = getattr(entity, "RelatingStructure", None)
            if parent is None:
                continue
            for child in getattr(entity, "RelatedElements", None) or ():
                children[parent.id()].append(child.id())
        stack = [(project_id, 0)]
        seen: set[int] = set()
        while stack:
            current, depth = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            key = ctx.key_by_step.get(current)
            if key and key in ctx.root_rows:
                ctx.project_of_object[key] = project_id
            if depth > 128:
                continue
            for child in children.get(current, ()):
                stack.append((child, depth + 1))
        if len(ctx.project_roots) > 1:
            ctx.fail(
                "multiple_projects",
                "warning",
                {
                    "project_step_ids": [
                        ctx.root_rows[k]["step_id"] for k in ctx.project_roots
                    ],
                    "message": "objects are attributed by spatial ownership",
                },
            )

    def _interpreter_for(self, project_id: int | None) -> ValueInterpreter:
        ctx = self.ctx
        assert ctx is not None
        units = ctx.project_units.get(project_id or -1, {})
        return ValueInterpreter(ctx.resolver, units)

    # -- relations ----------------------------------------------------------
    def _collect_relations(self) -> None:
        ctx = self.ctx
        assert ctx is not None
        for entity in ctx.file.by_type("IfcRelationship", include_subtypes=True):
            entity_type = entity.is_a()
            step_id = entity.id()
            if entity_type in {"IfcRelAggregates", "IfcRelContainedInSpatialStructure"}:
                self._collect_hierarchy(entity)
            elif entity_type == "IfcRelDefinesByType":
                self._collect_type_relation(entity)
            elif entity_type == "IfcRelDefinesByProperties":
                self._collect_property_relation(entity)
            # Material, document and other associations are collected with
            # their payloads so hierarchy/order and external targets survive.

    def _collect_hierarchy(self, entity: Any) -> None:
        ctx = self.ctx
        assert ctx is not None
        entity_type = entity.is_a()
        step_id = entity.id()
        kind = "decomposition" if entity_type == "IfcRelAggregates" else "containment"
        parent_attr = "RelatingObject" if kind == "decomposition" else "RelatingStructure"
        child_attr = "RelatedObjects" if kind == "decomposition" else "RelatedElements"
        parent = getattr(entity, parent_attr, None)
        parent_step = parent.id() if parent is not None else None
        parent_key = ctx.key_by_step.get(parent_step) if parent_step else None
        raw = ctx.step.args(step_id)
        # IfcRelAggregates:      ..., RelatingObject(4), RelatedObjects(5)
        # IfcRelContainedIn...:  ..., RelatedElements(4), RelatingStructure(5)
        if kind == "decomposition":
            raw_parent = raw[4] if len(raw) > 4 else None
            raw_children = raw[5] if len(raw) > 5 else None
        else:
            raw_children = raw[4] if len(raw) > 4 else None
            raw_parent = raw[5] if len(raw) > 5 else None
        raw_parent_id = raw_parent.step_id if isinstance(raw_parent, Ref) else None
        raw_child_ids = (
            [r.step_id for r in raw_children.items if isinstance(r, Ref)]
            if isinstance(raw_children, ListValue)
            else []
        )
        relation_key = ctx.relation_key(step_id)
        ctx.archive(step_id, "relation")
        state = "ok"
        errors: list[str] = []
        if raw_parent is None or isinstance(raw_parent, Ref) and parent_key is None:
            state = "missing"
            endpoint_step = raw_parent_id
            errors.append("relating endpoint is missing")
            ctx.fail(
                "missing_endpoint",
                "error",
                {
                    "relation_key": relation_key,
                    "relation_type": entity_type,
                    "endpoint": "relating",
                    "step_id": endpoint_step,
                },
                step_id=step_id,
                relation_key=relation_key,
            )
        resolved_children = {c.id() for c in (getattr(entity, child_attr, None) or ())}
        edges = []
        for ordinal, child_id in enumerate(raw_child_ids):
            child_key = ctx.key_by_step.get(child_id)
            edge_state = "ok"
            reason = None
            if child_key is None:
                edge_state = "missing"
                reason = f"related endpoint #{child_id} is not a resolvable entity"
                state = "missing" if state == "ok" else state
                ctx.fail(
                    "missing_endpoint",
                    "error",
                    {
                        "relation_key": relation_key,
                        "relation_type": entity_type,
                        "endpoint": "related",
                        "step_id": child_id,
                    },
                    step_id=step_id,
                    relation_key=relation_key,
                )
            if child_id not in resolved_children:
                edge_state = "missing"
                reason = "IfcOpenShell dropped this reference"
            edges.append(
                {
                    "kind": kind,
                    "ordinal": ordinal,
                    "parent_key": parent_key,
                    "parent_step_id": parent_step,
                    "child_key": child_key,
                    "child_step_id": child_id,
                    "edge_state": edge_state,
                    "reason": reason,
                }
            )
        ctx.relations.append(
            {
                "relation_key": relation_key,
                "source_id": self.source_id,
                "sha256": ctx.sha,
                "step_id": step_id,
                "relation_type": entity_type,
                "relating_key": parent_key,
                "relating_step_id": parent_step,
                "state": state,
                "error": "; ".join(errors) or None,
                "kind": kind,
                "edges": edges,
                "raw": {
                    "relating_step_id": raw_parent_id,
                    "related_step_ids": raw_child_ids,
                    "resolved_related_step_ids": sorted(resolved_children),
                },
            }
        )

    def _collect_type_relation(self, entity: Any) -> None:
        ctx = self.ctx
        assert ctx is not None
        step_id = entity.id()
        relation_key = ctx.relation_key(step_id)
        ctx.archive(step_id, "relation")
        related = list(getattr(entity, "RelatedObjects", None) or ())
        relating = getattr(entity, "RelatingType", None)
        type_step = relating.id() if relating is not None else None
        type_key = ctx.key_by_step.get(type_step) if type_step else None
        raw = ctx.step.args(step_id)
        raw_related = raw[4] if len(raw) > 4 else None
        raw_type = raw[5] if len(raw) > 5 else None
        raw_related_ids = (
            [r.step_id for r in raw_related.items if isinstance(r, Ref)]
            if isinstance(raw_related, ListValue)
            else []
        )
        state = "ok"
        errors: list[str] = []
        if type_key is None:
            state = "missing"
            errors.append("relating type is missing")
            ctx.fail(
                "missing_endpoint",
                "error",
                {
                    "relation_key": relation_key,
                    "relation_type": "IfcRelDefinesByType",
                    "endpoint": "relating",
                    "step_id": raw_type.step_id if isinstance(raw_type, Ref) else None,
                },
                step_id=step_id,
                relation_key=relation_key,
            )
        resolved = {o.id() for o in related}
        edges = []
        for ordinal, object_id in enumerate(raw_related_ids):
            object_key = ctx.key_by_step.get(object_id)
            edge_state = "ok"
            if object_key is None:
                edge_state = "missing"
                state = "missing" if state == "ok" else state
                ctx.fail(
                    "missing_endpoint",
                    "error",
                    {
                        "relation_key": relation_key,
                        "relation_type": "IfcRelDefinesByType",
                        "endpoint": "related",
                        "step_id": object_id,
                    },
                    step_id=step_id,
                    relation_key=relation_key,
                )
            if object_id not in resolved:
                edge_state = "missing"
            edges.append(
                {
                    "kind": "type",
                    "ordinal": ordinal,
                    "parent_key": type_key,
                    "parent_step_id": type_step,
                    "child_key": object_key,
                    "child_step_id": object_id,
                    "edge_state": edge_state,
                    "reason": None
                    if edge_state == "ok"
                    else "type assignment endpoint is not resolvable",
                }
            )
            if object_key and type_key:
                if object_key in ctx.type_links and ctx.type_links[object_key] != type_key:
                    ctx.fail(
                        "multiple_types",
                        "conflict",
                        {
                            "object_key": object_key,
                            "types": [ctx.type_links[object_key], type_key],
                        },
                        entity_key=object_key,
                    )
                ctx.type_links[object_key] = type_key
                row = ctx.root_rows.get(object_key)
                if row is not None:
                    row["type_key"] = type_key
        ctx.relations.append(
            {
                "relation_key": relation_key,
                "source_id": self.source_id,
                "sha256": ctx.sha,
                "step_id": step_id,
                "relation_type": "IfcRelDefinesByType",
                "relating_key": type_key,
                "relating_step_id": type_step,
                "state": state,
                "error": "; ".join(errors) or None,
                "kind": "type",
                "edges": edges,
                "raw": {
                    "relating_step_id": type_step,
                    "related_step_ids": raw_related_ids,
                    "resolved_related_step_ids": sorted(resolved),
                },
            }
        )

    def _collect_property_relation(self, entity: Any) -> None:
        ctx = self.ctx
        assert ctx is not None
        step_id = entity.id()
        relation_key = ctx.relation_key(step_id)
        ctx.archive(step_id, "relation")
        definition = getattr(entity, "RelatingPropertyDefinition", None)
        pset_step = definition.id() if definition is not None else None
        raw = ctx.step.args(step_id)
        raw_related = raw[4] if len(raw) > 4 else None
        raw_definition = raw[5] if len(raw) > 5 else None
        raw_related_ids = (
            [r.step_id for r in raw_related.items if isinstance(r, Ref)]
            if isinstance(raw_related, ListValue)
            else []
        )
        resolved = {o.id() for o in (entity.RelatedObjects or ())}
        state = "ok"
        if pset_step is None:
            state = "missing"
            ctx.fail(
                "missing_endpoint",
                "error",
                {
                    "relation_key": relation_key,
                    "relation_type": "IfcRelDefinesByProperties",
                    "endpoint": "relating",
                    "step_id": raw_definition.step_id
                    if isinstance(raw_definition, Ref)
                    else None,
                },
                step_id=step_id,
                relation_key=relation_key,
            )
        edges = []
        for ordinal, object_id in enumerate(raw_related_ids):
            object_key = ctx.key_by_step.get(object_id)
            edge_state = "ok"
            if object_key is None:
                edge_state = "missing"
                state = "missing" if state == "ok" else state
                ctx.fail(
                    "missing_endpoint",
                    "error",
                    {
                        "relation_key": relation_key,
                        "relation_type": "IfcRelDefinesByProperties",
                        "endpoint": "related",
                        "step_id": object_id,
                    },
                    step_id=step_id,
                    relation_key=relation_key,
                )
            if object_id not in resolved:
                edge_state = "missing"
            if object_key and pset_step is not None:
                ctx.object_pset_links.append(
                    {
                        "object_key": object_key,
                        "pset_key": ctx.pset_key(pset_step),
                        "pset_step_id": pset_step,
                        "scope": "instance",
                        "relation_key": relation_key,
                        "state": "ok" if edge_state == "ok" else "missing",
                        "reason": None if edge_state == "ok" else "property set reference dropped",
                    }
                )
            edges.append(
                {
                    "kind": "property",
                    "ordinal": ordinal,
                    "parent_key": ctx.pset_key(pset_step) if pset_step else None,
                    "parent_step_id": pset_step,
                    "child_key": object_key,
                    "child_step_id": object_id,
                    "edge_state": edge_state,
                    "reason": None
                    if edge_state == "ok"
                    else "property assignment endpoint is not resolvable",
                }
            )
        ctx.relations.append(
            {
                "relation_key": relation_key,
                "source_id": self.source_id,
                "sha256": ctx.sha,
                "step_id": step_id,
                "relation_type": "IfcRelDefinesByProperties",
                "relating_key": ctx.pset_key(pset_step) if pset_step else None,
                "relating_step_id": pset_step,
                "state": state,
                "error": None,
                "kind": "property",
                "edges": edges,
                "raw": {
                    "relating_step_id": pset_step,
                    "related_step_ids": raw_related_ids,
                    "resolved_related_step_ids": sorted(resolved),
                },
            }
        )

    # -- property sets ------------------------------------------------------
    def _collect_psets(self) -> None:
        ctx = self.ctx
        assert ctx is not None
        # Cache property sets once; IfcOpenShell resolves inverse relations
        # lazily and by_type over all sets inside the per-pset loop was O(n^2).
        pset_entities = {
            entity.id(): entity
            for entity in ctx.file.by_type("IfcPropertySet", include_subtypes=True)
        }
        links_by_pset: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for link in ctx.object_pset_links:
            links_by_pset[link["pset_key"]].append(link)
        # Type-level property sets (HasPropertySets).
        for type_entity in ctx.file.by_type("IfcTypeObject"):
            type_step = type_entity.id()
            type_key = ctx.key_by_step.get(type_step)
            if not type_key:
                continue
            raw = ctx.step.args(type_step)
            raw_psets = raw[5] if len(raw) > 5 else None
            raw_ids = (
                [r.step_id for r in raw_psets.items if isinstance(r, Ref)]
                if isinstance(raw_psets, ListValue)
                else []
            )
            resolved = {p.id() for p in (getattr(type_entity, "HasPropertySets", None) or ())}
            if not raw_ids and resolved:
                # Attribute positions do not match the schema (e.g. a malformed
                # file).  Use IfcOpenShell's resolved list and keep evidence.
                raw_ids = sorted(resolved)
                ctx.fail(
                    "attribute_position_mismatch",
                    "warning",
                    {
                        "step_id": type_step,
                        "entity_type": type_entity.is_a(),
                        "message": "HasPropertySets was resolved from the semantic view "
                        "because raw argument positions did not yield it",
                        "pset_step_ids": raw_ids,
                    },
                    step_id=type_step,
                )
            for pset_step in raw_ids:
                if pset_step not in resolved:
                    ctx.fail(
                        "missing_pset",
                        "error",
                        {"type_key": type_key, "pset_step_id": pset_step},
                        step_id=type_step,
                    )
                    continue
                ctx.object_pset_links.append(
                    {
                        "object_key": type_key,
                        "pset_key": ctx.pset_key(pset_step),
                        "pset_step_id": pset_step,
                        "scope": "type",
                        "relation_key": None,
                        "state": "ok",
                        "reason": None,
                    }
                )
        for step_id, entity in pset_entities.items():
            entity_type = entity.is_a()
            if step_id not in ctx.step.entities:
                continue
            ctx.archive(step_id, "pset")
            name = _text(getattr(entity, "Name", None)) or ""
            pset_key = ctx.pset_key(step_id)
            links = links_by_pset.get(pset_key, [])
            scopes = {l["scope"] for l in links if l["state"] == "ok"}
            owner_scope = "instance" if "instance" in scopes else (
                "type" if "type" in scopes else "unassigned"
            )
            row = {
                "pset_key": pset_key,
                "source_id": self.source_id,
                "sha256": ctx.sha,
                "step_id": step_id,
                "name": name,
                "entity_type": entity_type,
                "owner_scope": owner_scope,
                "description": _text(getattr(entity, "Description", None)),
                "owner_keys": sorted({l["object_key"] for l in links if l["state"] == "ok"}),
            }
            ctx.psets.append(row)
            if entity_type != "IfcPropertySet":
                # ElementQuantity and other property definitions have no
                # HasProperties; they are retained as empty definitions.
                continue
            raw = ctx.step.args(step_id)
            raw_props = raw[4] if len(raw) > 4 else None
            raw_prop_ids = (
                [r.step_id for r in raw_props.items if isinstance(r, Ref)]
                if isinstance(raw_props, ListValue)
                else []
            )
            resolved_props = {p.id() for p in (getattr(entity, "HasProperties", None) or ())}
            seen: set[int] = set()
            for ordinal, prop_step in enumerate(raw_prop_ids):
                if prop_step in seen:
                    ctx.fail(
                        "duplicate_property_in_set",
                        "warning",
                        {
                            "pset_key": pset_key,
                            "pset_name": name,
                            "prop_step_id": prop_step,
                            "ordinal": ordinal,
                        },
                        step_id=prop_step,
                    )
                    continue
                seen.add(prop_step)
                step_entity = ctx.step.entities.get(prop_step)
                if step_entity is None or prop_step not in resolved_props:
                    ctx.fail(
                        "missing_property",
                        "error",
                        {
                            "pset_key": pset_key,
                            "pset_name": name,
                            "prop_step_id": prop_step,
                        },
                        step_id=prop_step,
                    )
                    continue
                ctx.archive(prop_step, "property")
                declaration = ctx.reader.read(prop_step, step_entity.type_name)
                if declaration.extra.get("attribute_count_mismatch"):
                    ctx.fail(
                        "attribute_count_mismatch",
                        "error",
                        {
                            "step_id": prop_step,
                            "entity_type": step_entity.type_name,
                            **declaration.extra["attribute_count_mismatch"],
                        },
                        step_id=prop_step,
                    )
                ctx.declarations.append(
                    {
                        "pset_key": pset_key,
                        "pset_name": name,
                        "ordinal": ordinal,
                        "declaration": declaration,
                        "signature": declaration_signature_key(declaration, ctx.resolver),
                    }
                )
                ctx.declarations_by_pset.setdefault(pset_key, []).append(
                    ctx.declarations[-1]
                )
                ctx.counts["properties"] += 1

    # -- property resolution -------------------------------------------------
    def _resolve_properties(self) -> None:
        ctx = self.ctx
        assert ctx is not None
        instance_by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for link in ctx.object_pset_links:
            if link["state"] == "ok" and link["scope"] == "instance":
                instance_by_object[link["object_key"]].extend(
                    ctx.declarations_by_pset.get(link["pset_key"], [])
                )
        type_cache: dict[str, list[dict[str, Any]]] = {}

        def type_declarations(type_key: str) -> list[dict[str, Any]]:
            if type_key not in type_cache:
                acc: list[dict[str, Any]] = []
                for link in ctx.object_pset_links:
                    if (
                        link["object_key"] == type_key
                        and link["scope"] == "type"
                        and link["state"] == "ok"
                    ):
                        acc.extend(ctx.declarations_by_pset.get(link["pset_key"], []))
                type_cache[type_key] = acc
            return type_cache[type_key]

        for object_key, row in ctx.root_rows.items():
            if not row.get("is_object"):
                continue
            type_key = ctx.type_links.get(object_key)
            type_decls = type_declarations(type_key) if type_key else []
            instance_decls = instance_by_object.get(object_key, [])
            self._resolve_for_object(object_key, instance_decls, type_decls)

        # Type objects also carry their own type declarations; expose them as
        # type-scope rows so a reviewer can inspect the unmasked evidence.
        for type_key, row in ctx.root_rows.items():
            if not row.get("is_type"):
                continue
            decls = type_declarations(type_key)
            for group in _group_by_identity(decls).values():
                conflict = _has_conflict(group)
                record = self._make_record(
                    object_key=type_key,
                    group=group,
                    scope="type",
                    project_id=ctx.project_of_object.get(type_key),
                    conflict=conflict,
                    shadowed=None,
                    shadow_reason=None,
                    type_conflict=False,
                )
                ctx.stage_resolved(record)

    def _resolve_for_object(
        self,
        object_key: str,
        instance_decls: list[dict[str, Any]],
        type_decls: list[dict[str, Any]],
    ) -> None:
        ctx = self.ctx
        assert ctx is not None
        project_id = ctx.project_of_object.get(object_key)
        instance_groups = _group_by_identity(instance_decls)
        type_groups = _group_by_identity(type_decls)
        conflicted: set[str] = set()
        for identity in sorted(set(instance_groups) | set(type_groups)):
            inst = instance_groups.get(identity, [])
            typ = type_groups.get(identity, [])
            instance_conflict = _has_conflict(inst)
            type_conflict = _has_conflict(typ)
            if inst:
                if instance_conflict:
                    conflicted.add(identity)
                    ctx.stage_resolved(
                        self._make_record(
                            object_key=object_key,
                            group=inst,
                            scope="instance",
                            project_id=project_id,
                            conflict=True,
                            shadowed=typ or None,
                            shadow_reason="instance declarations override the type declaration",
                            type_conflict=type_conflict,
                        )
                    )
                else:
                    ctx.stage_resolved(
                        self._make_record(
                            object_key=object_key,
                            group=inst,
                            scope="instance",
                            project_id=project_id,
                            conflict=False,
                            shadowed=typ or None,
                            shadow_reason=(
                                "instance declaration overrides type declaration"
                                if typ
                                else None
                            ),
                            type_conflict=type_conflict,
                        )
                    )
            elif typ:
                if type_conflict:
                    conflicted.add(identity)
                record = self._make_record(
                    object_key=object_key,
                    group=typ,
                    scope="type",
                    project_id=project_id,
                    conflict=type_conflict,
                    shadowed=None,
                    shadow_reason=None,
                    type_conflict=False,
                )
                ctx.stage_resolved(record)
        if conflicted:
            row = ctx.root_rows.get(object_key)
            if row is not None and row.get("is_object"):
                if row.get("conflict_state") in (None, "property_conflict"):
                    row["conflict_state"] = "property_conflict"
                    row["conflict_detail"] = {
                        "identities": sorted(conflicted)
                    }

    def _make_record(
        self,
        *,
        object_key: str,
        group: list[dict[str, Any]],
        scope: str,
        project_id: int | None,
        conflict: bool,
        shadowed: list[dict[str, Any]] | None,
        shadow_reason: str | None,
        type_conflict: bool,
    ) -> dict[str, Any]:
        ctx = self.ctx
        assert ctx is not None
        canonical = group[0]
        declaration: RawDeclaration = canonical["declaration"]
        interpreter = self._interpreter_for(project_id)
        interpretation = interpret_declaration(
            declaration, interpreter, project_step_id=project_id
        )
        sources = [
            {
                "source_id": self.source_id,
                "sha256": ctx.sha,
                "step_id": entry["declaration"].prop_step_id,
                "pset_key": entry["pset_key"],
                "pset_name": entry["pset_name"],
                "prop_kind": entry["declaration"].prop_kind,
                "ordinal": entry["ordinal"],
                "signature_key": _signature_json_key(entry["signature"]),
            }
            for entry in group
        ]
        declaration_json = declaration.as_json()
        declaration_json["interpretation"] = interpretation.as_json()
        payload: dict[str, Any] = {
            "identity": canonical["identity"] if "identity" in canonical else _identity(
                canonical["pset_name"],
                declaration.name,
                declaration.prop_kind,
            ),
            "pset_name": canonical["pset_name"],
            "prop_name": declaration.name,
            "prop_kind": declaration.prop_kind,
            "scope_used": scope,
            "project_step_id": project_id,
            "merged_source_count": len(sources),
            "sources": sources,
            "declaration": declaration_json,
        }
        if conflict:
            payload["conflicting_declarations"] = _conflict_evidence(group)
            payload["reason"] = "multiple conflicting declarations at the same level"
        if shadowed is not None:
            shadow_interpretation = interpret_declaration(
                shadowed[0]["declaration"], interpreter, project_step_id=project_id
            )
            shadow_json = shadowed[0]["declaration"].as_json()
            shadow_json["interpretation"] = shadow_interpretation.as_json()
            payload["type_declaration_ignored"] = {
                "scope": "type",
                "reason": shadow_reason,
                "merged_source_count": len(shadowed),
                "type_conflict": type_conflict,
                "declaration": shadow_json,
                "sources": [
                    {
                        "source_id": self.source_id,
                        "sha256": ctx.sha,
                        "step_id": entry["declaration"].prop_step_id,
                        "pset_key": entry["pset_key"],
                        "pset_name": entry["pset_name"],
                        "signature_key": _signature_json_key(entry["signature"]),
                    }
                    for entry in shadowed
                ],
                "conflicting_declarations": _conflict_evidence(shadowed)
                if type_conflict
                else None,
                "evidence_note": (
                    "the instance declaration overrides this type declaration "
                    "even if its value is explicitly null"
                ),
            }
        reason = None
        if conflict:
            reason = "multiple conflicting declarations at the same level"
        elif shadowed is not None:
            reason = "instance declaration overrides type declaration"
        state = STATE_CONFLICT if conflict else interpretation.state
        value_status = VALUE_CONFLICT if conflict else interpretation.value_status
        if not conflict and state == STATE_VALUE and shadowed is not None and type_conflict:
            reason = (
                "instance declaration overrides a conflicting type declaration; "
                "the type conflict is retained as masked evidence"
            )
        return {
            "object_key": object_key,
            "source_id": self.source_id,
            "pset_name": canonical["pset_name"],
            "prop_name": declaration.name,
            "prop_kind": declaration.prop_kind,
            "identity": payload["identity"],
            "state": state,
            "value_status": value_status,
            "scope_used": scope,
            "is_conflict": conflict,
            "reason": reason,
            "json": payload,
        }

    # -- materials ----------------------------------------------------------
    def _collect_materials(self) -> None:
        ctx = self.ctx
        assert ctx is not None
        # IfcMaterialList is an IfcMaterialSelect member but is not an
        # IfcMaterialDefinition subtype in IFC4, so include it explicitly.
        definitions = list(
            ctx.file.by_type("IfcMaterialDefinition", include_subtypes=True)
        ) + list(ctx.file.by_type("IfcMaterialList"))
        seen_steps: set[int] = set()
        for entity in definitions:
            step_id = entity.id()
            if step_id in seen_steps:
                continue
            seen_steps.add(step_id)
            entity_type = entity.is_a()
            ctx.archive(step_id, "material")
            if entity_type == "IfcMaterial":
                self._material_node(
                    step_id,
                    "material",
                    _text(getattr(entity, "Name", None)),
                    {
                        "description": _text(getattr(entity, "Description", None)),
                        "category": _text(getattr(entity, "Category", None)),
                    },
                )
            elif entity_type == "IfcMaterialLayer":
                thickness_arg = ctx.step.args(step_id)[1] if len(ctx.step.args(step_id)) > 1 else None
                self._material_node(
                    step_id,
                    "layer",
                    _text(getattr(entity, "Name", None)),
                    {
                        "thickness_raw": thickness_arg.raw
                        if hasattr(thickness_arg, "raw")
                        else None,
                        "priority": getattr(entity, "Priority", None),
                        "category": _text(getattr(entity, "Category", None)),
                    },
                )
                raw = ctx.step.args(step_id)
                material_ref = raw[0] if raw else None
                if isinstance(material_ref, Ref):
                    ctx.material_edges.append(
                        {
                            "parent_key": ctx.material_key(step_id),
                            "child_key": ctx.material_key(material_ref.step_id),
                            "ordinal": 0,
                            "slot": "layer_material",
                            "detail": {"role": "material_of_layer"},
                        }
                    )
            elif entity_type == "IfcMaterialLayerSet":
                self._material_node(
                    step_id,
                    "layer_set",
                    _text(getattr(entity, "LayerSetName", None)),
                    {"description": _text(getattr(entity, "Description", None))},
                )
                raw = ctx.step.args(step_id)
                raw_layers = raw[0] if raw else None
                raw_ids = (
                    [r.step_id for r in raw_layers.items if isinstance(r, Ref)]
                    if isinstance(raw_layers, ListValue)
                    else []
                )
                resolved = {l.id() for l in (getattr(entity, "MaterialLayers", None) or ())}
                for ordinal, layer_step in enumerate(raw_ids):
                    if layer_step not in resolved:
                        ctx.fail(
                            "missing_material_endpoint",
                            "error",
                            {
                                "layer_set_step_id": step_id,
                                "missing_layer_step_id": layer_step,
                            },
                            step_id=step_id,
                        )
                        continue
                    ctx.material_edges.append(
                        {
                            "parent_key": ctx.material_key(step_id),
                            "child_key": ctx.material_key(layer_step),
                            "ordinal": ordinal,
                            "slot": "layer",
                            "detail": {"role": "ordered_layer", "ordinal": ordinal},
                        }
                    )
            elif entity_type == "IfcMaterialList":
                self._material_node(step_id, "material_list", None, {})
                raw = ctx.step.args(step_id)
                raw_materials = raw[0] if raw else None
                raw_ids = (
                    [r.step_id for r in raw_materials.items if isinstance(r, Ref)]
                    if isinstance(raw_materials, ListValue)
                    else []
                )
                for ordinal, material_step in enumerate(raw_ids):
                    ctx.material_edges.append(
                        {
                            "parent_key": ctx.material_key(step_id),
                            "child_key": ctx.material_key(material_step),
                            "ordinal": ordinal,
                            "slot": "material",
                            "detail": {"role": "listed_material", "ordinal": ordinal},
                        }
                    )
            elif entity_type in {
                "IfcMaterialConstituentSet",
                "IfcMaterialProfileSet",
                "IfcMaterialConstituent",
                "IfcMaterialProfile",
            }:
                self._material_node(
                    step_id, entity_type, _text(getattr(entity, "Name", None)), {}
                )
                raw = ctx.step.args(step_id)
                material_index = 2 if entity_type in {
                    "IfcMaterialConstituent",
                    "IfcMaterialProfile",
                } else 2
                material_ref = raw[material_index] if len(raw) > material_index else None
                if entity_type in {"IfcMaterialConstituent", "IfcMaterialProfile"}:
                    if isinstance(material_ref, Ref):
                        ctx.material_edges.append(
                            {
                                "parent_key": ctx.material_key(step_id),
                                "child_key": ctx.material_key(material_ref.step_id),
                                "ordinal": 0,
                                "slot": "constituent_material",
                                "detail": {"role": "material_of_constituent"},
                            }
                        )
                else:
                    raw_components = raw[2] if len(raw) > 2 else None
                    raw_ids = (
                        [r.step_id for r in raw_components.items if isinstance(r, Ref)]
                        if isinstance(raw_components, ListValue)
                        else []
                    )
                    for ordinal, comp_step in enumerate(raw_ids):
                        ctx.material_edges.append(
                            {
                                "parent_key": ctx.material_key(step_id),
                                "child_key": ctx.material_key(comp_step),
                                "ordinal": ordinal,
                                "slot": "constituent",
                                "detail": {"role": "constituent", "ordinal": ordinal},
                            }
                        )

        for relation in ctx.file.by_type("IfcRelAssociatesMaterial"):
            step_id = relation.id()
            ctx.archive(step_id, "relation")
            raw = ctx.step.args(step_id)
            raw_material = raw[5] if len(raw) > 5 else None
            raw_related = raw[4] if len(raw) > 4 else None
            material_step = raw_material.step_id if isinstance(raw_material, Ref) else None
            usage: dict[str, Any] = {}
            if material_step is not None:
                usage_entity = ctx.step.entities.get(material_step)
                if usage_entity is not None and usage_entity.type_name in {
                    "IFCMATERIALLAYERSETUSAGE",
                    "IFCMATERIALPROFILESETUSAGE",
                    "IFCMATERIALCONSTITUENTSETUSAGE",
                }:
                    ctx.archive(material_step, "material_usage")
                    usage_args = ctx.step.args(material_step)
                    target = usage_args[0] if usage_args else None
                    usage = {
                        "usage_step_id": material_step,
                        "usage_kind": usage_entity.type_name,
                        "layer_set_direction": _enum_name(usage_args[1])
                        if len(usage_args) > 1
                        else None,
                        "direction_sense": _enum_name(usage_args[2])
                        if len(usage_args) > 2
                        else None,
                        "offset_from_reference_line": _raw(
                            usage_args[3] if len(usage_args) > 3 else None
                        ),
                    }
                    material_step = target.step_id if isinstance(target, Ref) else None
            resolved_related = {o.id() for o in (relation.RelatedObjects or ())}
            raw_related_ids = (
                [r.step_id for r in raw_related.items if isinstance(r, Ref)]
                if isinstance(raw_related, ListValue)
                else []
            )
            for object_id in raw_related_ids:
                object_key = ctx.key_by_step.get(object_id)
                state = "ok"
                if object_key is None or material_step is None:
                    state = "missing"
                    ctx.fail(
                        "missing_material_endpoint",
                        "error",
                        {
                            "relation_step_id": step_id,
                            "object_step_id": object_id,
                            "material_step_id": material_step,
                        },
                        step_id=step_id,
                        relation_key=ctx.relation_key(step_id),
                    )
                if object_id not in resolved_related:
                    state = "missing"
                ctx.material_assignments.append(
                    {
                        "object_key": object_key,
                        "source_id": self.source_id,
                        "relation_key": ctx.relation_key(step_id),
                        "material_step_id": material_step,
                        "root_key": ctx.material_key(material_step)
                        if material_step
                        else None,
                        "state": state,
                        "json": {
                            "relation_step_id": step_id,
                            "material_step_id": material_step,
                            "usage": usage or None,
                            "object_step_id": object_id,
                            "object_resolved": object_id in resolved_related,
                        },
                    }
                )

    def _inherit_type_materials(self) -> None:
        """Copy type material associations to objects without their own.

        IFC material can be associated with a type; occurrences then inherit it.
        The copy is marked, and one informational failure row records the count,
        so the export can point at the type declaration rather than inventing a
        direct association.
        """

        ctx = self.ctx
        assert ctx is not None
        direct = {a["object_key"] for a in ctx.material_assignments}
        inherited: list[dict[str, Any]] = []
        for assignment in ctx.material_assignments:
            type_key = assignment["object_key"]
            if type_key is None:
                continue
            row = ctx.root_rows.get(type_key)
            if row is None or not row.get("is_type"):
                continue
            for object_key, obj_row in ctx.root_rows.items():
                if obj_row.get("type_key") != type_key:
                    continue
                if object_key in direct or not obj_row.get("is_object"):
                    continue
                clone = dict(assignment)
                clone["object_key"] = object_key
                clone["json"] = dict(assignment["json"])
                clone["json"]["inherited_from_type"] = True
                clone["json"]["inherited_from_key"] = type_key
                inherited.append(clone)
        ctx.material_assignments.extend(inherited)
        if inherited:
            ctx.fail(
                "material_inherited_from_type",
                "info",
                {
                    "count": len(inherited),
                    "objects": [a["object_key"] for a in inherited],
                },
            )

    def _material_node(
        self, step_id: int, kind: str, name: str | None, detail: dict[str, Any]
    ) -> None:
        ctx = self.ctx
        assert ctx is not None
        key = ctx.material_key(step_id)
        if key not in ctx.material_nodes:
            ctx.material_nodes[key] = {
                "key": key,
                "source_id": self.source_id,
                "sha256": ctx.sha,
                "step_id": step_id,
                "kind": kind,
                "name": name,
                "detail": detail,
            }

    # -- external references ------------------------------------------------
    def _collect_external_references(self) -> None:
        ctx = self.ctx
        assert ctx is not None
        seen_targets: set[int] = set()
        for entity in ctx.file.by_type("IfcRelAssociatesDocument"):
            step_id = entity.id()
            ctx.archive(step_id, "relation")
            raw = ctx.step.args(step_id)
            raw_target = raw[5] if len(raw) > 5 else None
            raw_related = raw[4] if len(raw) > 4 else None
            target_step = raw_target.step_id if isinstance(raw_target, Ref) else None
            target = self._external_target(target_step, seen_targets)
            raw_related_ids = (
                [r.step_id for r in raw_related.items if isinstance(r, Ref)]
                if isinstance(raw_related, ListValue)
                else []
            )
            for object_id in raw_related_ids:
                ctx.external_refs.append(
                    {
                        "source_id": self.source_id,
                        "sha256": ctx.sha,
                        "from_key": ctx.key_by_step.get(object_id),
                        "relation_key": ctx.relation_key(step_id),
                        "target_step_id": target_step,
                        "target_kind": target["target_kind"],
                        "location": target["location"],
                        "identification": target["identification"],
                        "name": target["name"],
                        "state": target["state"],
                        "json": {
                            "relation_type": "IfcRelAssociatesDocument",
                            "object_step_id": object_id,
                            "target": target,
                            "network_access": "not attempted",
                        },
                    }
                )
        for entity in ctx.file.by_type("IfcExternalReferenceRelationship"):
            step_id = entity.id()
            ctx.archive(step_id, "relation")
            raw = ctx.step.args(step_id)
            raw_reference = raw[0] if raw else None
            raw_related = raw[1] if len(raw) > 1 else None
            target_step = (
                raw_reference.step_id if isinstance(raw_reference, Ref) else None
            )
            target = self._external_target(target_step, seen_targets)
            raw_related_ids = (
                [r.step_id for r in raw_related.items if isinstance(r, Ref)]
                if isinstance(raw_related, ListValue)
                else []
            )
            for object_id in raw_related_ids:
                ctx.external_refs.append(
                    {
                        "source_id": self.source_id,
                        "sha256": ctx.sha,
                        "from_key": ctx.key_by_step.get(object_id),
                        "relation_key": ctx.relation_key(step_id),
                        "target_step_id": target_step,
                        "target_kind": target["target_kind"],
                        "location": target["location"],
                        "identification": target["identification"],
                        "name": target["name"],
                        "state": target["state"],
                        "json": {
                            "relation_type": "IfcExternalReferenceRelationship",
                            "object_step_id": object_id,
                            "target": target,
                            "network_access": "not attempted",
                        },
                    }
                )

    def _external_target(self, target_step: int | None, seen: set[int]) -> dict[str, Any]:
        ctx = self.ctx
        assert ctx is not None
        if target_step is None:
            return {
                "target_kind": None,
                "location": None,
                "identification": None,
                "name": None,
                "state": "missing",
                "attributes": None,
            }
        entity = ctx.step.entities.get(target_step)
        if entity is None:
            return {
                "target_kind": None,
                "location": None,
                "identification": None,
                "name": None,
                "state": "missing",
                "attributes": None,
            }
        if target_step not in seen:
            seen.add(target_step)
            ctx.archive(target_step, "external_target")
        args = ctx.step.args(target_step)
        location = args[0].value if args and isinstance(args[0], StringValue) else None
        identification = (
            args[1].value if len(args) > 1 and isinstance(args[1], StringValue) else None
        )
        name = args[2].value if len(args) > 2 and isinstance(args[2], StringValue) else None
        return {
            "target_kind": entity.type_name,
            "location": location,
            "identification": identification,
            "name": name,
            # Recorded only.  No network or filesystem access is performed.
            "state": "external"
            if location or identification
            else "recorded",
            "attributes": {
                "Location": location,
                "Identification": identification,
                "Name": name,
            },
        }

    # -- cycles -------------------------------------------------------------
    def _collect_cycles(self) -> None:
        ctx = self.ctx
        assert ctx is not None
        graph: dict[str, list[str]] = defaultdict(list)
        for relation in ctx.relations:
            if relation["kind"] not in HIERARCHY_KINDS:
                continue
            for edge in relation["edges"]:
                if edge["parent_key"] and edge["child_key"] and edge["edge_state"] == "ok":
                    graph[edge["parent_key"]].append(edge["child_key"])
        cycles = _find_cycles(graph)
        ctx.cycles = cycles
        cycle_edges: set[tuple[str, str]] = set()
        for cycle in cycles:
            for i in range(len(cycle) - 1):
                cycle_edges.add((cycle[i], cycle[i + 1]))
        for cycle in cycles:
            ctx.fail(
                "spatial_cycle",
                "error",
                {"cycle": cycle, "edges": [list(e) for e in zip(cycle, cycle[1:])]},
                entity_key=cycle[0],
            )
            for key in cycle:
                row = ctx.root_rows.get(key)
                if row is not None:
                    row["conflict_state"] = "spatial_cycle"
                    row["conflict_detail"] = {"cycle": cycle}
        # Relations that participate in a cycle are conflict evidence, not ok.
        for relation in ctx.relations:
            if relation["kind"] not in HIERARCHY_KINDS:
                continue
            touched = False
            for edge in relation["edges"]:
                if (edge["parent_key"], edge["child_key"]) in cycle_edges:
                    edge["edge_state"] = "conflict"
                    edge["reason"] = "edge participates in a spatial decomposition cycle"
                    touched = True
            if touched:
                relation["state"] = "conflict"
                relation["error"] = (
                    "relation participates in a spatial decomposition cycle"
                )
        for duplicate in ctx.step.duplicate_ids:
            ctx.fail(
                "duplicate_step_id",
                "error",
                duplicate,
                step_id=duplicate["step_id"],
            )
        if not ctx.project_roots:
            ctx.fail("no_project", "warning", {"message": "source declares no IfcProject"})

    # -- writing ------------------------------------------------------------
    def _write(self, started: str) -> None:
        ctx = self.ctx
        assert ctx is not None
        conn = self.repo.conn
        try:
            self.repo.delete_source_rows(conn, self.source_id)
            stats = self._write_rows(conn)
            conn.execute(
                "INSERT INTO resolved_properties(object_key, source_id, pset_name, "
                "prop_name, prop_kind, identity, state, value_status, scope_used, "
                "is_conflict, reason, json) SELECT object_key, source_id, pset_name, "
                "prop_name, prop_kind, identity, state, value_status, scope_used, "
                "is_conflict, reason, json FROM resolved_properties_stage"
            )
            conn.execute(
                "INSERT INTO sources(source_id, declared_sha256, sha256, file_name, "
                "stored_path, schema_name, status, error, entity_count, root_count, "
                "object_count, ingested_at, stats_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.source_id,
                    ctx.manifest.sha256,
                    ctx.sha,
                    os.path.basename(ctx.manifest.path),
                    ctx.store_path,
                    ctx.schema_name,
                    "ok",
                    None,
                    len(ctx.step.entities),
                    len(ctx.root_rows) + len(ctx.node_rows),
                    ctx.counts["objects"],
                    started,
                    dump_json(stats),
                ),
            )
            for failure in ctx.failures:
                self._insert_failure(conn, failure, ctx.sha)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.execute("DROP TABLE IF EXISTS resolved_properties_stage")

    def _insert_failure(self, conn: Any, failure: dict[str, Any], sha: str | None) -> None:
        conn.execute(
            "INSERT INTO failure_evidence(source_id, sha256, category, severity, "
            "entity_key, step_id, relation_key, detail_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                self.source_id,
                sha,
                failure["category"],
                failure["severity"],
                failure.get("entity_key"),
                failure.get("step_id"),
                failure.get("relation_key"),
                dump_json(failure.get("detail", {})),
            ),
        )

    def _write_rows(self, conn: Any) -> dict[str, Any]:
        ctx = self.ctx
        assert ctx is not None
        # Unusable objects: duplicate GlobalId or spatial cycle.
        conflict_objects = {
            key
            for key, row in ctx.root_rows.items()
            if row.get("conflict_state") is not None
        }
        roots: list[tuple[Any, ...]] = []
        for key, row in ctx.root_rows.items():
            roots.append(
                (
                    key,
                    row["source_id"],
                    row["sha256"],
                    row["step_id"],
                    row["global_id"],
                    row["key_kind"],
                    row["entity_type"],
                    row["category"],
                    row["name"],
                    row["object_type"],
                    row["predefined_type"],
                    int(row["is_object"]),
                    int(row["is_spatial"]),
                    int(row["is_type"]),
                    int(row["is_product"]),
                    0 if key in conflict_objects else 1,
                    row["conflict_state"],
                    dump_json(row["conflict_detail"]) if row["conflict_detail"] else None,
                    row.get("type_key"),
                    dump_json(row["attributes"]),
                    dump_json(row["taxonomy"]),
                )
            )
        conn.executemany(
            "INSERT INTO roots(key, source_id, sha256, step_id, global_id, key_kind, "
            "entity_type, category, name, object_type, predefined_type, is_object, "
            "is_spatial, is_type, is_product, usable, conflict_state, conflict_detail_json, "
            "type_key, attributes_json, taxonomy_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            roots,
        )
        conn.executemany(
            "INSERT INTO nodes(key, source_id, sha256, step_id, entity_type, name, "
            "attributes_json, taxonomy_json) VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    key,
                    row["source_id"],
                    row["sha256"],
                    row["step_id"],
                    row["entity_type"],
                    row["name"],
                    dump_json(row["attributes"]),
                    dump_json(row["taxonomy"]),
                )
                for key, row in ctx.node_rows.items()
            ],
        )
        conn.executemany(
            "INSERT INTO relations(relation_key, source_id, sha256, step_id, relation_type, "
            "relating_key, relating_step_id, state, error, json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    rel["relation_key"],
                    rel["source_id"],
                    rel["sha256"],
                    rel["step_id"],
                    rel["relation_type"],
                    rel["relating_key"],
                    rel["relating_step_id"],
                    rel["state"],
                    rel["error"],
                    dump_json(
                        {
                            "kind": rel["kind"],
                            "raw": rel["raw"],
                            "edges": rel["edges"],
                        }
                    ),
                )
                for rel in ctx.relations
            ],
        )
        edges: list[tuple[Any, ...]] = []
        for rel in ctx.relations:
            for edge in rel["edges"]:
                edges.append(
                    (
                        rel["relation_key"],
                        rel["source_id"],
                        rel["sha256"],
                        rel["relation_type"],
                        edge["kind"],
                        edge["ordinal"],
                        edge["parent_key"],
                        edge["parent_step_id"],
                        edge["child_key"],
                        edge["child_step_id"],
                        edge["edge_state"],
                        edge["reason"],
                    )
                )
        conn.executemany(
            "INSERT INTO relation_edges(relation_key, source_id, sha256, relation_type, "
            "kind, ordinal, parent_key, parent_step_id, child_key, child_step_id, "
            "edge_state, reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            edges,
        )
        conn.executemany(
            "INSERT INTO pset_definitions(pset_key, source_id, sha256, step_id, name, "
            "entity_type, owner_scope, json) VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    p["pset_key"],
                    p["source_id"],
                    p["sha256"],
                    p["step_id"],
                    p["name"],
                    p["entity_type"],
                    p["owner_scope"],
                    dump_json(
                        {
                            "description": p["description"],
                            "owner_keys": p["owner_keys"],
                        }
                    ),
                )
                for p in ctx.psets
            ],
        )
        decl_rows = []
        for entry in ctx.declarations:
            declaration: RawDeclaration = entry["declaration"]
            # The declaration table stores raw declarations, not per-project
            # interpretation: the same type declaration can resolve differently
            # for objects in different projects.  Structural state and reason
            # are recorded; no unit status is invented here.
            decl_rows.append(
                (
                    f"decl:{ctx.sha}:{entry['pset_key']}:"
                    f"{declaration.prop_step_id}:"
                    f"{_identity(entry['pset_name'], declaration.name, declaration.prop_kind)}",
                    self.source_id,
                    ctx.sha,
                    entry["pset_key"],
                    entry["pset_name"],
                    declaration.prop_step_id,
                    declaration.prop_kind,
                    declaration.name,
                    _identity(entry["pset_name"], declaration.name, declaration.prop_kind),
                    _signature_json_key(entry["signature"]),
                    declaration.structural_state,
                    "not_interpreted",
                    declaration.values[0].type_name
                    if len(declaration.values) == 1
                    else None,
                    declaration.unit_step_id,
                    "property" if declaration.unit_step_id else None,
                    declaration.values[0].raw if declaration.values else None,
                    declaration.expression,
                    dump_json(declaration.as_json()),
                )
            )
        conn.executemany(
            "INSERT INTO property_declarations(decl_key, source_id, sha256, pset_key, "
            "pset_name, prop_step_id, prop_kind, prop_name, identity, signature, "
            "state, value_status, declared_type, unit_step_id, unit_source, "
            "original_raw, expression, json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            decl_rows,
        )
        conn.executemany(
            "INSERT INTO object_psets(object_key, source_id, pset_key, pset_step_id, "
            "scope, relation_key, state, reason) VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    link["object_key"],
                    self.source_id,
                    link["pset_key"],
                    link["pset_step_id"],
                    link["scope"],
                    link["relation_key"],
                    link["state"],
                    link["reason"],
                )
                for link in ctx.object_pset_links
            ],
        )
        # resolved_properties rows were streamed into the staging table and are
        # copied over by _write() after this method returns.
        conn.executemany(
            "INSERT INTO material_nodes(key, source_id, sha256, step_id, kind, name, json) "
            "VALUES(?,?,?,?,?,?,?)",
            [
                (
                    node["key"],
                    node["source_id"],
                    node["sha256"],
                    node["step_id"],
                    node["kind"],
                    node["name"],
                    dump_json(node["detail"]),
                )
                for node in ctx.material_nodes.values()
            ],
        )
        conn.executemany(
            "INSERT INTO material_edges(parent_key, child_key, ordinal, slot, json) "
            "VALUES(?,?,?,?,?)",
            [
                (
                    edge["parent_key"],
                    edge["child_key"],
                    edge["ordinal"],
                    edge["slot"],
                    dump_json(edge["detail"]),
                )
                for edge in ctx.material_edges
            ],
        )
        conn.executemany(
            "INSERT INTO material_assignments(object_key, source_id, relation_key, "
            "material_step_id, root_key, state, json) VALUES(?,?,?,?,?,?,?)",
            [
                (
                    a["object_key"],
                    self.source_id,
                    a["relation_key"],
                    a["material_step_id"],
                    a["root_key"],
                    a["state"],
                    dump_json(a["json"]),
                )
                for a in ctx.material_assignments
            ],
        )
        # Units: every unit entity archived during ingestion.
        unit_rows = []
        for step_id, roles in ctx.archived.items():
            if "unit" not in roles and "unit_assignment" not in roles:
                continue
            entity = ctx.step.entities.get(step_id)
            if entity is None:
                continue
            if entity.type_name == "IFCUNITASSIGNMENT":
                continue
            unit = ctx.resolver.resolve(step_id)
            unit_rows.append(
                (
                    f"unit:{ctx.sha}:{step_id}",
                    self.source_id,
                    ctx.sha,
                    step_id,
                    unit.dimension,
                    int(unit.scale is not None and unit.dimension is not None),
                    dump_json(unit.as_json()),
                )
            )
        conn.executemany(
            "INSERT INTO units(unit_key, source_id, sha256, step_id, dimension, usable, "
            "json) VALUES(?,?,?,?,?,?,?)",
            unit_rows,
        )
        project_unit_rows = []
        for project_step, units in ctx.project_units.items():
            for dimension, entry in units.items():
                project_unit_rows.append(
                    (
                        self.source_id,
                        project_step,
                        dimension,
                        entry["unit_step_id"],
                        entry["state"],
                        dump_json(entry),
                    )
                )
        conn.executemany(
            "INSERT INTO project_units(source_id, project_step_id, dimension, unit_step_id, "
            "state, json) VALUES(?,?,?,?,?,?)",
            project_unit_rows,
        )
        conn.executemany(
            "INSERT INTO external_references(source_id, sha256, from_key, relation_key, "
            "target_step_id, target_kind, location, identification, name, state, json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    ref["source_id"],
                    ref["sha256"],
                    ref["from_key"],
                    ref["relation_key"],
                    ref["target_step_id"],
                    ref["target_kind"],
                    ref["location"],
                    ref["identification"],
                    ref["name"],
                    ref["state"],
                    dump_json(ref["json"]),
                )
                for ref in ctx.external_refs
            ],
        )
        # Source excerpts: exact lines that back each business row. Roles are
        # stored so export can select only what the subset uses.
        self._stage_source_entities()
        conn.execute(
            "INSERT INTO source_headers(source_id, sha256, header_text) VALUES(?,?,?)",
            (self.source_id, ctx.sha, ctx.step.header_text()),
        )
        stats = {
            "objects": ctx.counts["objects"],
            "types": ctx.counts["types"],
            "properties": ctx.counts["properties"],
            "declarations": len(ctx.declarations),
            "relations": len(ctx.relations),
            "material_nodes": len(ctx.material_nodes),
            "material_assignments": len(ctx.material_assignments),
            "external_references": len(ctx.external_refs),
            "failures": len(ctx.failures),
            "project_units": {
                str(k): sorted(v) for k, v in ctx.project_units.items()
            },
        }
        return stats

    def _stage_source_entities(self) -> None:
        """Stream exact source lines into source_entities in bounded batches.

        Raw text is the most memory-hungry per-entity data, so it never
        accumulates into a full-file list.
        """

        ctx = self.ctx
        assert ctx is not None
        for step_id, roles in ctx.archived.items():
            entity = ctx.step.entities.get(step_id)
            if entity is None:
                continue
            key = ctx.key_by_step.get(step_id)
            for role in sorted(roles):
                ctx.stage_source_entity(
                    (
                        self.source_id,
                        ctx.sha,
                        step_id,
                        entity.type_name,
                        role,
                        entity.line_start,
                        entity.line_end,
                        entity.text,
                        key,
                    )
                )
        ctx.flush_source_entities()


def _signature_json_key(key: Any) -> Any:
    """Render a stored signature key as JSON-friendly data.

    Tuple signatures are stored as a short digest at declaration time; the
    verbose form is not needed in every resolved row.
    """

    if isinstance(key, str):
        return key
    payload = _json.dumps(key, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _identity(pset_name: str, prop_name: str | None, prop_kind: str) -> str:
    # Property identity includes the property set name, the property name and
    # the declared kind, so a single-value and an enumerated value of the same
    # name in one set stay separate.
    return f"{pset_name}\u001f{prop_name or ''}\u001f{prop_kind}"


def _group_by_identity(
    declarations: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in declarations:
        declaration: RawDeclaration = entry["declaration"]
        identity = _identity(entry["pset_name"], declaration.name, declaration.prop_kind)
        entry["identity"] = identity
        groups[identity].append(entry)
    return groups


def _has_conflict(group: list[dict[str, Any]]) -> bool:
    signatures = {entry["signature"] for entry in group}
    return len(signatures) > 1


def _conflict_evidence(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for entry in group:
        declaration: RawDeclaration = entry["declaration"]
        out.append(
            {
                "step_id": declaration.prop_step_id,
                "prop_kind": declaration.prop_kind,
                "pset_name": entry["pset_name"],
                "signature": entry["signature"],
                "declaration": declaration.as_json(),
            }
        )
    return out


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Find simple cycles in a directed graph (spatial decomposition).

    Union-find is not enough because the direction matters, so this is a
    depth-first search with an explicit path.  The graph is small (hierarchy
    edges only) and only cycles are returned.
    """

    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    cycles: list[list[str]] = []
    seen_cycles: set[tuple[str, ...]] = set()

    def canonical(cycle: list[str]) -> tuple[str, ...]:
        body = cycle[:-1]
        if not body:
            return tuple(cycle)
        rotations = [tuple(body[i:] + body[:i]) for i in range(len(body))]
        return min(rotations) + (min(rotations)[0],)

    for start in list(graph):
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        path_index: dict[str, int] = {}
        color[start] = GREY
        path.append(start)
        path_index[start] = 0
        while stack:
            node, index = stack[-1]
            neighbours = graph.get(node, [])
            if index < len(neighbours):
                stack[-1] = (node, index + 1)
                nxt = neighbours[index]
                state = color[nxt]
                if state == GREY:
                    cycle = path[path_index[nxt] :] + [nxt]
                    key = canonical(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(cycle)
                elif state == WHITE:
                    color[nxt] = GREY
                    path_index[nxt] = len(path)
                    path.append(nxt)
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                stack.pop()
                path.pop()
                path_index.pop(node, None)
    return cycles
