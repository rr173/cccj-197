"""Zone scope determination against the fixed repository snapshot.

For every rule zone this walks the repository's spatial ``decomposition`` and
``containment`` edges downward from the zone root.  Type and material
relations never participate.  The walk follows *valid* edges, but every
invalid edge reachable from the root (missing endpoint, cycle conflict) is
recorded with its known endpoints as scope evidence -- scope damage is never
hidden by looking at valid edges only.  Damage is strictly per-zone: a broken
edge that is not reachable from a zone's root does not affect that zone.

A device enters the determined matters only when it is reached over valid
edges *and* has a clear ``(source_id, GlobalId)`` identity.  Devices with
only a source-local STEP identity or with a conflicting GlobalId, roots that
cannot be uniquely resolved, cycles and broken in-zone edges all mark the
zone ``scope_unverified`` while keeping the concrete object/edge evidence.

Coverage targets are computed once per determined matter (the model is fixed
for the whole run): the device's in-zone valid ancestors, its valid ancestors
beyond the zone root, and candidate ancestors reachable only through invalid
edges, each with one reproducible evidence path.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import deque
from typing import Any, Iterable

from ..keys import canonical_json
from .dataset import SCOPE_CONFIRMED, SCOPE_UNVERIFIED
from .inputs import Rules, ZoneRule

HIERARCHY_KINDS = ("decomposition", "containment")

# conflict_state values that make an object's (source, GlobalId) identity
# unusable.  A property_conflict leaves the identity intact and does not
# matter for handover scope.
IDENTITY_CONFLICT_STATES = ("duplicate_global_id", "spatial_cycle")

# Scope issue kinds.
ISSUE_ROOT_UNRESOLVED = "root_unresolved"
ISSUE_ROOT_AMBIGUOUS = "root_ambiguous"
ISSUE_ROOT_NOT_SPATIAL = "root_not_spatial"
ISSUE_ROOT_UNUSABLE = "root_unusable"
ISSUE_EDGE_MISSING = "edge_missing_endpoint"
ISSUE_EDGE_CONFLICT = "edge_conflict"
ISSUE_DEVICE_IDENTITY_MISSING = "device_identity_missing"
ISSUE_DEVICE_IDENTITY_CONFLICT = "device_identity_conflict"

# record_targets association kinds.
ASSOC_DEVICE = "device"
ASSOC_SPACE_VALID = "space_valid"
ASSOC_SPACE_BEYOND_ROOT = "space_beyond_root"
ASSOC_SPACE_CANDIDATE = "space_candidate"
ASSOC_SPACE_DEFECTIVE_OUTSIDE = "space_defective_outside"

_FLUSH = 200


class _BufferedInsert:
    """Small buffered executemany helper; memory stays bounded by the buffer."""

    def __init__(self, conn: sqlite3.Connection, sql: str):
        self.conn = conn
        self.sql = sql
        self.buffer: list[tuple[Any, ...]] = []

    def add(self, row: tuple[Any, ...]) -> None:
        self.buffer.append(row)
        if len(self.buffer) >= _FLUSH:
            self.flush()

    def flush(self) -> None:
        if self.buffer:
            self.conn.executemany(self.sql, self.buffer)
            self.buffer.clear()


class ScopeBuilder:
    """Computes zone scopes, determined matters and coverage targets."""

    def __init__(self, repo_conn: sqlite3.Connection, work_conn: sqlite3.Connection):
        self.repo = repo_conn
        self.work = work_conn
        self.devices = _BufferedInsert(
            work_conn,
            "INSERT INTO zone_devices(zone_id, device_key, source_id, global_id, step_id, "
            "entity_type, name) VALUES(?,?,?,?,?,?,?)",
        )
        self.matters = _BufferedInsert(
            work_conn,
            "INSERT INTO matters(record_id, ordinal, zone_id, device_key, device_source_id, "
            "device_global_id, device_step_id, device_entity_type, device_name, matter_code, "
            "coverage_mode, min_signatories) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        )
        self.targets = _BufferedInsert(
            work_conn,
            "INSERT INTO record_targets(record_id, object_key, assoc_kind, path_json) "
            "VALUES(?,?,?,?)",
        )
        self.evidence = _BufferedInsert(
            work_conn,
            "INSERT OR IGNORE INTO evidence(owner_kind, owner_id, source_id, sha256, step_id, "
            "entity_type, role, line_start, line_end, raw_text) VALUES(?,?,?,?,?,?,?,?,?,?)",
        )
        self._ordinal = 0
        self._zone_issue_count = 0

    # ------------------------------------------------------------------
    def build(self, rules: Rules) -> None:
        for ordinal, zone in enumerate(rules.zones):
            self._build_zone(zone, ordinal)
        self._flush_all()

    def _flush_all(self) -> None:
        for buffered in (self.devices, self.matters, self.targets, self.evidence):
            buffered.flush()

    # -- evidence --------------------------------------------------------
    def _add_entity_evidence(
        self, owner_kind: str, owner_id: str, source_id: str, step_id: int
    ) -> None:
        for row in self.repo.execute(
            "SELECT * FROM source_entities WHERE source_id=? AND step_id=?",
            (source_id, step_id),
        ):
            self.evidence.add(
                (
                    owner_kind,
                    owner_id,
                    row["source_id"],
                    row["sha256"],
                    row["step_id"],
                    row["entity_type"],
                    row["role"],
                    row["line_start"],
                    row["line_end"],
                    row["raw_text"],
                )
            )

    def _add_relation_evidence(self, owner_kind: str, owner_id: str, relation_key: str) -> None:
        row = self.repo.execute(
            "SELECT source_id, step_id FROM relations WHERE relation_key=?", (relation_key,)
        ).fetchone()
        if row is not None:
            self._add_entity_evidence(owner_kind, owner_id, row["source_id"], row["step_id"])

    def _issue(
        self,
        zone_id: str,
        kind: str,
        detail: dict[str, Any],
        *,
        object_key: str | None = None,
        relation_key: str | None = None,
        evidence_steps: Iterable[tuple[str, int]] = (),
        evidence_relation: bool = False,
    ) -> None:
        cursor = self.work.execute(
            "INSERT INTO zone_scope_issues(zone_id, issue_kind, object_key, relation_key, "
            "detail_json) VALUES(?,?,?,?,?)",
            (zone_id, kind, object_key, relation_key, canonical_json(detail)),
        )
        self._zone_issue_count += 1
        owner = f"scope_issue:{cursor.lastrowid}"
        for source_id, step_id in evidence_steps:
            self._add_entity_evidence("scope_issue", owner, source_id, step_id)
        if evidence_relation and relation_key is not None:
            self._add_relation_evidence("scope_issue", owner, relation_key)

    # -- zone walk -------------------------------------------------------
    def _build_zone(self, zone: ZoneRule, ordinal: int) -> None:
        self._zone_issue_count = 0
        zone_id = zone.zone_id
        root_rows = list(
            self.repo.execute(
                "SELECT * FROM roots WHERE source_id=? AND global_id=? ORDER BY step_id",
                (zone.root_source_id, zone.root_global_id),
            )
        )
        root_key: str | None = None
        root_ok = False
        if not root_rows:
            self._issue(
                zone_id,
                ISSUE_ROOT_UNRESOLVED,
                {
                    "source_id": zone.root_source_id,
                    "global_id": zone.root_global_id,
                    "message": "no repository object carries this identity",
                },
            )
        elif len(root_rows) > 1:
            self._issue(
                zone_id,
                ISSUE_ROOT_AMBIGUOUS,
                {
                    "source_id": zone.root_source_id,
                    "global_id": zone.root_global_id,
                    "step_ids": [r["step_id"] for r in root_rows],
                    "keys": [r["key"] for r in root_rows],
                    "message": "the GlobalId is duplicated in this source",
                },
                evidence_steps=[(r["source_id"], r["step_id"]) for r in root_rows],
            )
        else:
            row = root_rows[0]
            root_key = row["key"]
            if row["conflict_state"] in IDENTITY_CONFLICT_STATES:
                self._issue(
                    zone_id,
                    ISSUE_ROOT_UNUSABLE,
                    {
                        "root_key": root_key,
                        "conflict_state": row["conflict_state"],
                        "message": "the root object is marked unusable in the repository",
                    },
                    object_key=root_key,
                    evidence_steps=[(row["source_id"], row["step_id"])],
                )
            elif not row["is_spatial"]:
                self._issue(
                    zone_id,
                    ISSUE_ROOT_NOT_SPATIAL,
                    {
                        "root_key": root_key,
                        "entity_type": row["entity_type"],
                        "message": "the zone root is not a spatial element",
                    },
                    object_key=root_key,
                    evidence_steps=[(row["source_id"], row["step_id"])],
                )
            else:
                root_ok = True

        device_count = 0
        matter_count = 0
        if root_ok and root_key is not None:
            device_count = self._walk_zone(zone, root_key)
            if device_count:
                matter_count = self._build_matters(zone)
        self._flush_all()
        self.work.execute(
            "INSERT INTO zones(zone_id, ordinal, root_source_id, root_global_id, root_key, "
            "scope_status, device_count, matter_count) VALUES(?,?,?,?,?,?,?,?)",
            (
                zone_id,
                ordinal,
                zone.root_source_id,
                zone.root_global_id,
                root_key,
                SCOPE_CONFIRMED if self._zone_issue_count == 0 else SCOPE_UNVERIFIED,
                device_count,
                matter_count,
            ),
        )

    # ------------------------------------------------------------------
    def _walk_zone(self, zone: ZoneRule, root_key: str) -> int:
        """BFS over valid hierarchy edges; record every invalid edge found.

        Membership rows are written with plain INSERT OR IGNORE so the
        ``rowcount`` doubles as the already-visited check; the per-zone
        working set lives in SQLite, not in Python memory.
        """

        zone_id = zone.zone_id
        self.work.execute(
            "INSERT OR IGNORE INTO zone_members(zone_id, object_key) VALUES(?,?)",
            (zone_id, root_key),
        )
        frontier = [root_key]
        while frontier:
            next_frontier: list[str] = []
            for key in frontier:
                for edge in self.repo.execute(
                    "SELECT * FROM relation_edges WHERE parent_key=? AND kind IN "
                    "('decomposition','containment') ORDER BY kind, ordinal, id",
                    (key,),
                ):
                    if edge["edge_state"] == "ok" and edge["child_key"] is not None:
                        cursor = self.work.execute(
                            "INSERT OR IGNORE INTO zone_members(zone_id, object_key) VALUES(?,?)",
                            (zone_id, edge["child_key"]),
                        )
                        if cursor.rowcount == 1:
                            next_frontier.append(edge["child_key"])
                        continue
                    # An invalid edge reachable from the root: keep its known
                    # endpoints as concrete scope evidence.
                    detail = {
                        "relation_key": edge["relation_key"],
                        "relation_type": edge["relation_type"],
                        "kind": edge["kind"],
                        "ordinal": edge["ordinal"],
                        "parent_key": edge["parent_key"],
                        "parent_step_id": edge["parent_step_id"],
                        "child_key": edge["child_key"],
                        "child_step_id": edge["child_step_id"],
                        "edge_state": edge["edge_state"],
                        "reason": edge["reason"],
                    }
                    self._issue(
                        zone_id,
                        ISSUE_EDGE_MISSING
                        if edge["edge_state"] == "missing"
                        else ISSUE_EDGE_CONFLICT,
                        detail,
                        object_key=edge["child_key"],
                        relation_key=edge["relation_key"],
                        evidence_relation=True,
                    )
            frontier = next_frontier
        return self._select_devices(zone)

    def _select_devices(self, zone: ZoneRule) -> int:
        """Determined devices: valid-path members with a clear identity.

        The member set lives in the work database and the objects in the
        repository; the two are never joined in SQL.  Each member is resolved
        by primary key, so the walk stays streaming on both sides.
        """

        device_types = set(zone.device_types)
        count = 0
        members = self.work.execute(
            "SELECT object_key FROM zone_members WHERE zone_id=? ORDER BY object_key",
            (zone.zone_id,),
        )
        for member in members:
            row = self.repo.execute(
                "SELECT * FROM roots WHERE key=?", (member["object_key"],)
            ).fetchone()
            if row is None or not row["is_object"] or row["entity_type"] not in device_types:
                continue
            identity_ok = (
                row["key_kind"] == "gid"
                and row["conflict_state"] not in IDENTITY_CONFLICT_STATES
            )
            if identity_ok:
                self.devices.add(
                    (
                        zone.zone_id,
                        row["key"],
                        row["source_id"],
                        row["global_id"],
                        row["step_id"],
                        row["entity_type"],
                        row["name"],
                    )
                )
                count += 1
                continue
            self._issue(
                zone.zone_id,
                ISSUE_DEVICE_IDENTITY_MISSING
                if row["key_kind"] != "gid"
                else ISSUE_DEVICE_IDENTITY_CONFLICT,
                {
                    "device_key": row["key"],
                    "source_id": row["source_id"],
                    "step_id": row["step_id"],
                    "global_id": row["global_id"],
                    "entity_type": row["entity_type"],
                    "key_kind": row["key_kind"],
                    "conflict_state": row["conflict_state"],
                    "message": (
                        "device has only a source-local STEP identity"
                        if row["key_kind"] != "gid"
                        else "device identity is conflicted in the repository"
                    ),
                },
                object_key=row["key"],
                evidence_steps=[(row["source_id"], row["step_id"])],
            )
        self._flush_all()
        return count

    # -- matters and coverage targets ------------------------------------
    def _build_matters(self, zone: ZoneRule) -> int:
        count = 0
        device_rows = self.work.execute(
            "SELECT * FROM zone_devices WHERE zone_id=? ORDER BY source_id, step_id",
            (zone.zone_id,),
        )
        for device in device_rows:
            for matter in zone.matters:
                record_id = hashlib.sha256(
                    canonical_json(
                        {
                            "zone_id": zone.zone_id,
                            "device_source_id": device["source_id"],
                            "device_global_id": device["global_id"],
                            "device_step_id": device["step_id"],
                            "device_entity_type": device["entity_type"],
                            "matter_code": matter.code,
                            "coverage_mode": matter.coverage,
                            "min_signatories": matter.min_signatories,
                        }
                    ).encode("utf-8")
                ).hexdigest()
                self.matters.add(
                    (
                        record_id,
                        self._ordinal,
                        zone.zone_id,
                        device["device_key"],
                        device["source_id"],
                        device["global_id"],
                        device["step_id"],
                        device["entity_type"],
                        device["name"],
                        matter.code,
                        matter.coverage,
                        matter.min_signatories,
                    )
                )
                self._ordinal += 1
                count += 1
                # Device entity STEP evidence for this matter.
                self._add_entity_evidence(
                    "matter", record_id, device["source_id"], device["step_id"]
                )
                # Coverage targets for this record.
                self.targets.add((record_id, device["device_key"], ASSOC_DEVICE, None))
                for target_key, assoc_kind, path in self._coverage_targets(
                    zone.zone_id, device["device_key"]
                ):
                    self.targets.add(
                        (
                            record_id,
                            target_key,
                            assoc_kind,
                            canonical_json(path) if path is not None else None,
                        )
                    )
        self._flush_all()
        return count

    def _coverage_targets(
        self, zone_id: str, device_key: str
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Spatial ancestors of the device, classified against the zone.

        One upward walk follows every hierarchy edge with a known parent
        (valid or not), preferring all-valid paths.  A spatial ancestor is:

        * ``space_valid`` -- reachable over valid edges and inside the zone;
        * ``space_beyond_root`` -- reachable over valid edges but outside the
          zone (a signoff there cannot cross the zone root);
        * ``space_candidate`` -- inside the zone but every path uses an
          invalid edge (coverage cannot be confirmed);
        * ``space_defective_outside`` -- outside the zone and only reachable
          over invalid edges.

        The walk is bounded by the device's own ancestor set, never by the
        repository size, and keeps one reproducible path per target.
        """

        # key -> (all_ok, path-of-edge-dicts from the device)
        best: dict[str, tuple[bool, list[dict[str, Any]]]] = {device_key: (True, [])}
        queue: deque[str] = deque([device_key])
        while queue:
            key = queue.popleft()
            all_ok, path = best[key]
            for edge in self.repo.execute(
                "SELECT * FROM relation_edges WHERE child_key=? AND kind IN "
                "('decomposition','containment') ORDER BY kind, relation_key, ordinal, id",
                (key,),
            ):
                parent = edge["parent_key"]
                if parent is None:
                    continue
                hop = {
                    "relation_key": edge["relation_key"],
                    "relation_type": edge["relation_type"],
                    "kind": edge["kind"],
                    "ordinal": edge["ordinal"],
                    "child_key": edge["child_key"],
                    "parent_key": edge["parent_key"],
                    "child_step_id": edge["child_step_id"],
                    "parent_step_id": edge["parent_step_id"],
                    "edge_state": edge["edge_state"],
                    "reason": edge["reason"],
                }
                new_ok = all_ok and edge["edge_state"] == "ok"
                if parent in best:
                    existing_ok, _ = best[parent]
                    if new_ok and not existing_ok:
                        # Upgrade to an all-valid path and re-expand so the
                        # improvement propagates upward.
                        best[parent] = (True, path + [hop])
                        queue.append(parent)
                    continue
                best[parent] = (new_ok, path + [hop])
                queue.append(parent)

        out: list[tuple[str, str, dict[str, Any]]] = []
        for key in sorted(best):
            if key == device_key:
                continue
            row = self.repo.execute(
                "SELECT source_id, global_id, step_id, entity_type, is_spatial "
                "FROM roots WHERE key=?",
                (key,),
            ).fetchone()
            if row is None or not row["is_spatial"]:
                continue
            in_zone = self.work.execute(
                "SELECT 1 FROM zone_members WHERE zone_id=? AND object_key=?",
                (zone_id, key),
            ).fetchone()
            all_ok, path = best[key]
            if all_ok and in_zone:
                assoc = ASSOC_SPACE_VALID
            elif all_ok:
                assoc = ASSOC_SPACE_BEYOND_ROOT
            elif in_zone:
                assoc = ASSOC_SPACE_CANDIDATE
            else:
                assoc = ASSOC_SPACE_DEFECTIVE_OUTSIDE
            payload = {
                "from": {"key": device_key},
                "to": {
                    "key": key,
                    "source_id": row["source_id"],
                    "global_id": row["global_id"],
                    "step_id": row["step_id"],
                    "entity_type": row["entity_type"],
                },
                "complete_ok": all_ok,
                "hops": path,
            }
            out.append((key, assoc, payload))
        return out
