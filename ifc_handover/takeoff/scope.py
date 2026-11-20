"""Spatial scope walks for the quantity take-off.

For every rule this walks the repository's spatial ``decomposition`` and
``containment`` edges downward from the rule's spatial root.  Type and
material relations never participate, and a type object is never treated as
an equipment instance.  The walk follows *valid* edges only, but every
invalid edge reachable from the root (missing endpoint, conflicted edge) is
recorded with its known endpoints as a scope anomaly -- scope damage is never
hidden by looking at valid edges only.

An object reached over repeated edges inside one rule is counted once: the
member set lives in an on-disk SQLite table with a primary key, never in a
Python list.  The same object reached from two distinct roots is inserted
once per rule and therefore attributed (and summarised) independently under
each root.

Damage is strictly per rule: a broken edge that is not reachable from a
rule's root does not affect that rule, and an object-level bad quantity is
isolated later without disturbing other roots.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..keys import canonical_json
from .dataset import (
    ANOMALY_EDGE_CONFLICT,
    ANOMALY_EDGE_MISSING,
    ANOMALY_OBJECT_IDENTITY_CONFLICT,
    ANOMALY_OBJECT_IDENTITY_MISSING,
    ANOMALY_ROOT_AMBIGUOUS,
    ANOMALY_ROOT_NOT_SPATIAL,
    ANOMALY_ROOT_UNRESOLVED,
    ANOMALY_ROOT_UNUSABLE,
    MEMBER_EXCLUDED_IDENTITY,
    MEMBER_VALID,
    SCOPE_CONFIRMED,
    SCOPE_UNVERIFIED,
)
from .inputs import TakeoffRule

HIERARCHY_KINDS = ("decomposition", "containment")
IDENTITY_CONFLICT_STATES = ("duplicate_global_id", "spatial_cycle")
_FLUSH = 200


class _BufferedInsert:
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
    """Compute per-rule member sets and scope anomalies into the work DB."""

    def __init__(self, repo_conn: sqlite3.Connection, work_conn: sqlite3.Connection):
        self.repo = repo_conn
        self.work = work_conn
        self.membership = _BufferedInsert(
            work_conn,
            "INSERT INTO scope_membership(rule_id, object_key, source_id, global_id, "
            "step_id, entity_type, name, membership, depth) VALUES(?,?,?,?,?,?,?,?,?)",
        )
        self.evidence = _BufferedInsert(
            work_conn,
            "INSERT OR IGNORE INTO evidence(owner_kind, owner_id, source_id, sha256, "
            "step_id, entity_type, role, line_start, line_end, raw_text) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
        )
        self._anomaly_count = 0

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

    def _add_relation_evidence(
        self, owner_kind: str, owner_id: str, relation_key: str
    ) -> None:
        row = self.repo.execute(
            "SELECT source_id, step_id FROM relations WHERE relation_key=?",
            (relation_key,),
        ).fetchone()
        if row is not None:
            self._add_entity_evidence(owner_kind, owner_id, row["source_id"], row["step_id"])

    def _anomaly(
        self,
        rule_id: str,
        kind: str,
        detail: dict[str, Any],
        *,
        source_id: str | None = None,
        object_key: str | None = None,
        global_id: str | None = None,
        step_id: int | None = None,
        relation_key: str | None = None,
        evidence_steps: tuple[tuple[str, int], ...] = (),
        evidence_relation: bool = False,
    ) -> None:
        self.work.execute(
            "INSERT INTO scope_anomalies(rule_id, anomaly_kind, source_id, object_key, "
            "global_id, step_id, relation_key, detail_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                rule_id,
                kind,
                source_id,
                object_key,
                global_id,
                step_id,
                relation_key,
                canonical_json(detail),
            ),
        )
        self._anomaly_count += 1
        cursor = self.work.execute("SELECT last_insert_rowid()").fetchone()
        owner = f"scope_anomaly:{cursor[0]}"
        for ev_source, ev_step in evidence_steps:
            self._add_entity_evidence("scope_anomaly", owner, ev_source, ev_step)
        if evidence_relation and relation_key is not None:
            self._add_relation_evidence("scope_anomaly", owner, relation_key)

    # -- per-rule build --------------------------------------------------
    def build(self, rules: tuple[TakeoffRule, ...]) -> None:
        for ordinal, rule in enumerate(rules):
            self._anomaly_count = 0
            root_rows = list(
                self.repo.execute(
                    "SELECT * FROM roots WHERE source_id=? AND global_id=? ORDER BY step_id",
                    (rule.root_source_id, rule.root_global_id),
                )
            )
            root_key: str | None = None
            root_ok = False
            if not root_rows:
                self._anomaly(
                    rule.rule_id,
                    ANOMALY_ROOT_UNRESOLVED,
                    {
                        "source_id": rule.root_source_id,
                        "global_id": rule.root_global_id,
                        "message": "no repository object carries this identity",
                    },
                    source_id=rule.root_source_id,
                    global_id=rule.root_global_id,
                )
            elif len(root_rows) > 1:
                self._anomaly(
                    rule.rule_id,
                    ANOMALY_ROOT_AMBIGUOUS,
                    {
                        "source_id": rule.root_source_id,
                        "global_id": rule.root_global_id,
                        "step_ids": [r["step_id"] for r in root_rows],
                        "keys": [r["key"] for r in root_rows],
                        "message": "the GlobalId is duplicated in this source",
                    },
                    source_id=rule.root_source_id,
                    global_id=rule.root_global_id,
                    evidence_steps=tuple(
                        (r["source_id"], r["step_id"]) for r in root_rows
                    ),
                )
            else:
                row = root_rows[0]
                root_key = row["key"]
                if row["conflict_state"] in IDENTITY_CONFLICT_STATES:
                    self._anomaly(
                        rule.rule_id,
                        ANOMALY_ROOT_UNUSABLE,
                        {
                            "root_key": root_key,
                            "conflict_state": row["conflict_state"],
                            "message": "the root object is marked unusable in the repository",
                        },
                        source_id=row["source_id"],
                        object_key=root_key,
                        global_id=row["global_id"],
                        step_id=row["step_id"],
                        evidence_steps=((row["source_id"], row["step_id"]),),
                    )
                elif not row["is_spatial"]:
                    self._anomaly(
                        rule.rule_id,
                        ANOMALY_ROOT_NOT_SPATIAL,
                        {
                            "root_key": root_key,
                            "entity_type": row["entity_type"],
                            "message": "the rule root is not a spatial element",
                        },
                        source_id=row["source_id"],
                        object_key=root_key,
                        global_id=row["global_id"],
                        step_id=row["step_id"],
                        evidence_steps=((row["source_id"], row["step_id"]),),
                    )
                else:
                    root_ok = True
            included = 0
            if root_ok and root_key is not None:
                included = self._walk(rule, root_key)
            self.membership.flush()
            self.evidence.flush()
            self.work.execute(
                "INSERT INTO root_scopes(rule_id, root_key, scope_status, "
                "included_objects, anomaly_count) VALUES(?,?,?,?,?)",
                (
                    rule.rule_id,
                    root_key,
                    SCOPE_CONFIRMED if self._anomaly_count == 0 else SCOPE_UNVERIFIED,
                    included,
                    self._anomaly_count,
                ),
            )

    def _walk(self, rule: TakeoffRule, root_key: str) -> int:
        """BFS over valid hierarchy edges, recording every invalid edge."""

        rule_id = rule.rule_id
        self.work.execute(
            "INSERT OR IGNORE INTO work_members(rule_id, object_key, depth) VALUES(?,?,0)",
            (rule_id, root_key),
        )
        frontier = [(root_key, 0)]
        entity_types = set(rule.entity_types)
        included = 0
        while frontier:
            next_frontier: list[tuple[str, int]] = []
            for key, depth in frontier:
                for edge in self.repo.execute(
                    "SELECT * FROM relation_edges WHERE parent_key=? AND kind IN "
                    "('decomposition','containment') ORDER BY kind, ordinal, id",
                    (key,),
                ):
                    if edge["edge_state"] == "ok" and edge["child_key"] is not None:
                        cursor = self.work.execute(
                            "INSERT OR IGNORE INTO work_members(rule_id, object_key, depth) "
                            "VALUES(?,?,?)",
                            (rule_id, edge["child_key"], depth + 1),
                        )
                        if cursor.rowcount == 1:
                            next_frontier.append((edge["child_key"], depth + 1))
                        continue
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
                    self._anomaly(
                        rule_id,
                        ANOMALY_EDGE_MISSING
                        if edge["edge_state"] == "missing"
                        else ANOMALY_EDGE_CONFLICT,
                        detail,
                        source_id=edge["source_id"],
                        object_key=edge["child_key"],
                        step_id=edge["child_step_id"],
                        relation_key=edge["relation_key"],
                        evidence_relation=True,
                    )
            frontier = next_frontier
        # Materialise the durable membership rows, classifying identity.
        for member in self.work.execute(
            "SELECT object_key, depth FROM work_members WHERE rule_id=? ORDER BY object_key",
            (rule_id,),
        ):
            row = self.repo.execute(
                "SELECT * FROM roots WHERE key=?", (member["object_key"],)
            ).fetchone()
            if row is None:
                continue
            if not row["is_object"] or row["entity_type"] not in entity_types:
                continue
            identity_ok = (
                row["key_kind"] == "gid"
                and row["conflict_state"] not in IDENTITY_CONFLICT_STATES
            )
            if identity_ok:
                membership = MEMBER_VALID
                included += 1
            else:
                membership = MEMBER_EXCLUDED_IDENTITY
                self._anomaly(
                    rule_id,
                    ANOMALY_OBJECT_IDENTITY_MISSING
                    if row["key_kind"] != "gid"
                    else ANOMALY_OBJECT_IDENTITY_CONFLICT,
                    {
                        "object_key": row["key"],
                        "source_id": row["source_id"],
                        "step_id": row["step_id"],
                        "global_id": row["global_id"],
                        "entity_type": row["entity_type"],
                        "key_kind": row["key_kind"],
                        "conflict_state": row["conflict_state"],
                        "message": (
                            "object has only a source-local STEP identity"
                            if row["key_kind"] != "gid"
                            else "object identity is conflicted in the repository"
                        ),
                    },
                    source_id=row["source_id"],
                    object_key=row["key"],
                    global_id=row["global_id"],
                    step_id=row["step_id"],
                    evidence_steps=((row["source_id"], row["step_id"]),),
                )
            self.membership.add(
                (
                    rule_id,
                    row["key"],
                    row["source_id"],
                    row["global_id"],
                    row["step_id"],
                    row["entity_type"],
                    row["name"],
                    membership,
                    member["depth"],
                )
            )
        self.membership.flush()
        self.evidence.flush()
        return included
