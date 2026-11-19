"""Cross-version comparison engine.

The engine merge-joins the two read-only repositories by IfcRoot GlobalId
using sorted SQLite streams, so memory stays bounded by one object's semantics
plus a small write buffer rather than by the total object count.  The only
identity inputs are GlobalIds and, optionally, a validated one-to-one
correspondence table; source ids, file paths and STEP numbers are never used as
cross-version identity.

Outcomes are kept separate: added, removed, semantic change, provenance-only
change and incomparable.  An object that cannot be compared (duplicate
GlobalId, conflicted relation, unusable mapped endpoint) is reported as
incomparable with evidence, never disguised as a remove+add or as a confident
edit.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterator, Sequence

from . import fingerprints as fp
from .dataset import (
    ASPECT_ENTITY_TYPE,
    ASPECT_MATERIAL,
    ASPECT_PROPERTY,
    ASPECT_SPATIAL,
    ASPECT_TYPE_ASSIGNMENT,
    CAT_ADDED,
    CAT_INCOMPARABLE,
    CAT_PROVENANCE_CHANGE,
    CAT_REMOVED,
    CAT_SEMANTIC_CHANGE,
    ChangeDatasetWriter,
    ChangeRecord,
)
from .identity import IdentityMapping
from .snapshot import RepoSnapShot

# Object-level incomparable records (identity itself is unusable).
ASPECT_IDENTITY = "identity"

# How often (in processed gid groups) to re-verify the input fingerprints.
_MODIFICATION_CHECK_EVERY = 256


class ComparisonError(RuntimeError):
    pass


def compare_repositories(
    left: RepoSnapShot,
    right: RepoSnapShot,
    writer: ChangeDatasetWriter,
    mapping: IdentityMapping,
) -> dict[str, Any]:
    """Run the full comparison, streaming rows into ``writer``."""

    engine = _Engine(left, right, writer, mapping)
    return engine.run()


class _Engine:
    def __init__(
        self,
        left: RepoSnapShot,
        right: RepoSnapShot,
        writer: ChangeDatasetWriter,
        mapping: IdentityMapping,
    ):
        self.left = left
        self.right = right
        self.writer = writer
        self.mapping = mapping
        self.consumed_left: set[tuple[str, str]] = set()
        self.consumed_right: set[tuple[str, str]] = set()
        self.processed = 0

    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        self._process_mapped_pairs()
        self._merge_auto_pairs()
        self._process_unglobalized()
        self.left.assert_unchanged()
        self.right.assert_unchanged()
        return {"processed_identities": self.processed}

    def _tick(self) -> None:
        self.processed += 1
        if self.processed % _MODIFICATION_CHECK_EVERY == 0:
            self.left.assert_unchanged()
            self.right.assert_unchanged()

    # -- mapped pairs ---------------------------------------------------
    def _process_mapped_pairs(self) -> None:
        for pair in sorted(self.mapping.pairs, key=lambda p: p.index):
            self.consumed_left.add((pair.left_source_id, pair.left_global_id))
            self.consumed_right.add((pair.right_source_id, pair.right_global_id))
            left_rows = self._rows_for_snapshot(
                self.left, pair.left_source_id, pair.left_global_id
            )
            right_rows = self._rows_for_snapshot(
                self.right, pair.right_source_id, pair.right_global_id
            )
            pair_key = (
                f"map:{pair.left_source_id}:{pair.left_global_id}"
                f"->{pair.right_source_id}:{pair.right_global_id}"
            )
            self._compare_endpoint_pair(
                left_rows,
                right_rows,
                pair_key=pair_key,
                identity_kind="explicit_mapping",
                mapping=pair,
            )
            self._tick()

    @staticmethod
    def _rows_for_snapshot(
        snapshot: RepoSnapShot, source_id: str, global_id: str
    ) -> list[sqlite3.Row]:
        return list(
            snapshot.conn.execute(
                "SELECT * FROM roots WHERE source_id=? AND global_id=? AND key_kind='gid' "
                "ORDER BY step_id",
                (source_id, global_id),
            )
        )

    # -- auto merge -----------------------------------------------------
    def _merge_auto_pairs(self) -> None:
        left_groups = self._gid_stream(self.left, self.consumed_left)
        right_groups = self._gid_stream(self.right, self.consumed_right)
        left_group = next(left_groups, None)
        right_group = next(right_groups, None)
        while left_group is not None or right_group is not None:
            if right_group is None or (
                left_group is not None and left_group[0] < right_group[0]
            ):
                self._auto_one_sided(left_group, side="left")
                left_group = next(left_groups, None)
            elif left_group is None or (
                right_group is not None and right_group[0] < left_group[0]
            ):
                self._auto_one_sided(right_group, side="right")
                right_group = next(right_groups, None)
            else:
                # Same GlobalId on both sides.
                assert left_group is not None and right_group is not None
                self._auto_matched(left_group[1], right_group[1])
                left_group = next(left_groups, None)
                right_group = next(right_groups, None)
            self._tick()

    @staticmethod
    def _is_business_root(row: sqlite3.Row) -> bool:
        """Only IfcObject instances and IfcTypeObject definitions are compared.

        Projects, relationship entities and property sets are IfcRoot rows but
        are not reviewable equipment/objects; they are evidence for the business
        objects rather than comparison subjects.
        """

        return bool(row["is_object"]) or bool(row["is_type"])

    def _gid_stream(
        self, snapshot: RepoSnapShot, consumed: set[tuple[str, str]]
    ) -> Iterator[tuple[str, list[sqlite3.Row]]]:
        """Yield (global_id, rows) across sources, dropping mapped endpoints."""

        current_gid: str | None = None
        batch: list[sqlite3.Row] = []
        for group in snapshot.stream_root_groups():
            for row in group:
                if not self._is_business_root(row):
                    continue
                gid = row["global_id"]
                if (row["source_id"], gid) in consumed:
                    continue
                if current_gid is not None and gid != current_gid:
                    yield current_gid, batch
                    batch = []
                current_gid = gid
                batch.append(row)
        if batch and current_gid is not None:
            yield current_gid, batch

    def _auto_one_sided(
        self, group: tuple[str, list[sqlite3.Row]], *, side: str
    ) -> None:
        gid, rows = group
        snapshot = self.left if side == "left" else self.right
        pair_key = f"{side}:gid:{gid}"
        # A duplicated GlobalId on the only side that has it is still unusable:
        # report incomparable rather than a confident add/remove.
        if self._group_is_duplicate(rows):
            self._emit_identity_incomparable(
                rows,
                opposite_rows=[],
                pair_key=pair_key,
                reason="duplicate GlobalId on the only side carrying this identity",
                mapping=None,
            )
            return
        usable = [r for r in rows if r["usable"]]
        if len(usable) != 1:
            self._emit_identity_incomparable(
                rows,
                opposite_rows=[],
                pair_key=pair_key,
                reason="no single usable root for this identity",
                mapping=None,
            )
            return
        # Multiple distinct sources offering the same gid means distinct
        # objects; choosing one would be a guess.
        if len({r["source_id"] for r in usable}) > 1:
            self._emit_identity_incomparable(
                rows,
                opposite_rows=[],
                pair_key=pair_key,
                reason="GlobalId occurs in multiple sources; cross-source merge refused",
                mapping=None,
            )
            return
        row = usable[0]
        category = CAT_REMOVED if side == "left" else CAT_ADDED
        self._emit_added_removed(row, snapshot, category, pair_key, gid)

    def _auto_matched(
        self, left_rows: list[sqlite3.Row], right_rows: list[sqlite3.Row]
    ) -> None:
        gid = left_rows[0]["global_id"]
        pair_key = f"gid:{gid}"
        self._compare_endpoint_pair(
            left_rows,
            right_rows,
            pair_key=pair_key,
            identity_kind="global_id",
            mapping=None,
        )

    # -- shared pair resolution -----------------------------------------
    def _compare_endpoint_pair(
        self,
        left_rows: list[sqlite3.Row],
        right_rows: list[sqlite3.Row],
        *,
        pair_key: str,
        identity_kind: str,
        mapping: Any,
    ) -> None:
        left_duplicate = self._group_is_duplicate(left_rows)
        right_duplicate = self._group_is_duplicate(right_rows)
        left_usable = [r for r in left_rows if r["usable"]]
        right_usable = [r for r in right_rows if r["usable"]]
        left_multi_source = len({r["source_id"] for r in left_usable}) > 1
        right_multi_source = len({r["source_id"] for r in right_usable}) > 1

        if (
            left_duplicate
            or right_duplicate
            or left_multi_source
            or right_multi_source
            or len(left_usable) != 1
            or len(right_usable) != 1
        ):
            reasons = []
            if left_duplicate:
                reasons.append("left duplicate GlobalId")
            if right_duplicate:
                reasons.append("right duplicate GlobalId")
            if left_multi_source or right_multi_source:
                reasons.append("GlobalId present in multiple sources on one side")
            if len(left_usable) != 1 and not left_duplicate and not left_multi_source:
                reasons.append("left identity has no single usable root")
            if len(right_usable) != 1 and not right_duplicate and not right_multi_source:
                reasons.append("right identity has no single usable root")
            self._emit_identity_incomparable(
                left_rows,
                right_rows,
                pair_key=pair_key,
                reason="; ".join(reasons) or "identity not uniquely usable",
                mapping=mapping,
            )
            return

        left_row = left_usable[0]
        right_row = right_usable[0]
        ctx = _PairContext(self, left_row, right_row, identity_kind, mapping, pair_key)
        ctx.compare()

    @staticmethod
    def _group_is_duplicate(rows: Sequence[sqlite3.Row]) -> bool:
        by_source: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_source.setdefault(row["source_id"], []).append(row)
        return any(len(group) > 1 for group in by_source.values())

    # -- no-GlobalId roots ----------------------------------------------
    def _process_unglobalized(self) -> None:
        # STEP-scoped roots can never be matched across versions: their only id
        # is source-local provenance.  List them separately as removed/added.
        for side, snapshot, category in (
            ("left", self.left, CAT_REMOVED),
            ("right", self.right, CAT_ADDED),
        ):
            cursor = snapshot.conn.execute(
                "SELECT * FROM roots WHERE key_kind='step' AND (is_object=1 OR is_type=1) "
                "ORDER BY source_id, step_id"
            )
            while True:
                rows = cursor.fetchmany(256)
                if not rows:
                    break
                for row in rows:
                    pair_key = f"{side}:step:{row['source_id']}:{row['step_id']}"
                    self._emit_added_removed(
                        row, snapshot, category, pair_key, global_id=None
                    )
                    self._tick()

    # ------------------------------------------------------------------
    # Record builders
    # ------------------------------------------------------------------
    def _emit_added_removed(
        self,
        row: sqlite3.Row,
        snapshot: RepoSnapShot,
        category: str,
        pair_key: str,
        global_id: str | None,
    ) -> None:
        side = "left" if category == CAT_REMOVED else "right"
        record = ChangeRecord(
            category=category,
            aspect=ASPECT_IDENTITY,
            subject=f"root:{row['key']}",
            identity_kind="global_id" if global_id is not None else "step_scoped",
            left_source_id=row["source_id"] if side == "left" else None,
            left_sha256=row["sha256"] if side == "left" else None,
            right_source_id=row["source_id"] if side == "right" else None,
            right_sha256=row["sha256"] if side == "right" else None,
            left_global_id=global_id if side == "left" else None,
            right_global_id=global_id if side == "right" else None,
            before={"entity_type": row["entity_type"], "name": row["name"]}
            if side == "left"
            else None,
            after={"entity_type": row["entity_type"], "name": row["name"]}
            if side == "right"
            else None,
            evidence=self._evidence_for_rows([row] if side == "left" else [],
                                              [row] if side == "right" else []),
        )
        self.writer.add(record)
        self.writer.note_pair(
            pair_key,
            identity_kind=record.identity_kind,
            left_source_id=record.left_source_id,
            left_global_id=record.left_global_id,
            right_source_id=record.right_source_id,
            right_global_id=record.right_global_id,
            category=category,
        )

    def _emit_identity_incomparable(
        self,
        left_rows: Sequence[sqlite3.Row],
        right_rows: Sequence[sqlite3.Row],
        *,
        pair_key: str,
        reason: str,
        mapping: Any,
    ) -> None:
        left_gid = next((r["global_id"] for r in left_rows), None)
        right_gid = next((r["global_id"] for r in right_rows), None)
        left_source = next((r["source_id"] for r in left_rows), None)
        right_source = next((r["source_id"] for r in right_rows), None)
        record = ChangeRecord(
            category=CAT_INCOMPARABLE,
            aspect=ASPECT_IDENTITY,
            subject=f"identity:{left_gid or right_gid}",
            identity_kind="explicit_mapping" if mapping is not None else "global_id",
            left_source_id=left_source,
            left_sha256=next((r["sha256"] for r in left_rows), None),
            right_source_id=right_source,
            right_sha256=next((r["sha256"] for r in right_rows), None),
            left_global_id=left_gid,
            right_global_id=right_gid,
            mapping_ordinal=None if mapping is None else mapping.index,
            mapping_rationale=None if mapping is None else mapping.rationale,
            before={"rows": [self._root_descriptor(r) for r in left_rows]}
            if left_rows
            else None,
            after={"rows": [self._root_descriptor(r) for r in right_rows]}
            if right_rows
            else None,
            reason=reason,
            evidence=self._evidence_for_rows(list(left_rows), list(right_rows)),
        )
        self.writer.add(record)
        self.writer.note_pair(
            pair_key,
            identity_kind=record.identity_kind,
            left_source_id=left_source,
            left_global_id=left_gid,
            right_source_id=right_source,
            right_global_id=right_gid,
            category=CAT_INCOMPARABLE,
        )

    @staticmethod
    def _root_descriptor(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "key": row["key"],
            "source_id": row["source_id"],
            "global_id": row["global_id"],
            "step_id": row["step_id"],
            "entity_type": row["entity_type"],
            "usable": bool(row["usable"]),
            "conflict_state": row["conflict_state"],
        }

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------
    def _evidence_for_rows(
        self,
        left_rows: Sequence[sqlite3.Row],
        right_rows: Sequence[sqlite3.Row],
        *,
        extra_left: Sequence[tuple[str, int]] = (),
        extra_right: Sequence[tuple[str, int]] = (),
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int, str]] = set()
        self._collect_evidence(self.left, "left", left_rows, out, seen)
        self._collect_evidence(self.right, "right", right_rows, out, seen)
        for source_id, step_id in extra_left:
            self._collect_one(self.left, "left", source_id, step_id, out, seen)
        for source_id, step_id in extra_right:
            self._collect_one(self.right, "right", source_id, step_id, out, seen)
        return out

    def _collect_evidence(
        self,
        snapshot: RepoSnapShot,
        side: str,
        rows: Sequence[sqlite3.Row],
        out: list[dict[str, Any]],
        seen: set[tuple[str, str, int, str]],
    ) -> None:
        for row in rows:
            self._collect_one(
                snapshot, side, row["source_id"], row["step_id"], out, seen
            )

    def _collect_one(
        self,
        snapshot: RepoSnapShot,
        side: str,
        source_id: str,
        step_id: int,
        out: list[dict[str, Any]],
        seen: set[tuple[str, str, int, str]],
    ) -> None:
        entity = snapshot.source_entity(source_id, step_id)
        role = entity["role"] if entity is not None else "root"
        token = (side, source_id, step_id, role)
        if token in seen:
            return
        seen.add(token)
        if entity is None:
            return
        out.append(
            {
                "side": side,
                "source_id": source_id,
                "sha256": entity["sha256"],
                "step_id": step_id,
                "entity_type": entity["entity_type"],
                "role": role,
                "line_start": entity["line_start"],
                "line_end": entity["line_end"],
                "raw_text": entity["raw_text"],
            }
        )


class _PairContext:
    """Comparison of one uniquely matched left/right root pair."""

    def __init__(
        self,
        engine: _Engine,
        left_row: sqlite3.Row,
        right_row: sqlite3.Row,
        identity_kind: str,
        mapping: Any,
        pair_key: str,
    ):
        self.engine = engine
        self.left_row = left_row
        self.right_row = right_row
        self.identity_kind = identity_kind
        self.mapping = mapping
        self.pair_key = pair_key
        self.categories: set[str] = set()

    # -- identity fields shared by every record of the pair -------------
    def _base_kwargs(self, aspect: str) -> dict[str, Any]:
        return {
            "aspect": aspect,
            "identity_kind": self.identity_kind,
            "left_source_id": self.left_row["source_id"],
            "left_sha256": self.left_row["sha256"],
            "right_source_id": self.right_row["source_id"],
            "right_sha256": self.right_row["sha256"],
            "left_global_id": self.left_row["global_id"],
            "right_global_id": self.right_row["global_id"],
            "mapping_ordinal": None if self.mapping is None else self.mapping.index,
            "mapping_rationale": None if self.mapping is None else self.mapping.rationale,
        }

    def _emit(self, record: ChangeRecord) -> None:
        self.engine.writer.add(record)
        self.categories.add(record.category)

    def compare(self) -> None:
        self._compare_entity_type()
        # Types contribute through their instances: their property sets are
        # reviewed as the instances' type-scope declarations, and type material
        # trees as the instances' inherited assignments.  Diffing a type's own
        # resolved rows would duplicate every instance-level change.
        is_type = bool(self.left_row["is_type"]) or bool(self.right_row["is_type"])
        if not is_type:
            self._compare_properties()
            self._compare_spatial()
        self._compare_type_assignment()
        self._compare_materials()
        if self.categories:
            for category in sorted(self.categories):
                self.engine.writer.note_pair(
                    self.pair_key,
                    identity_kind=self.identity_kind,
                    left_source_id=self.left_row["source_id"],
                    left_global_id=self.left_row["global_id"],
                    right_source_id=self.right_row["source_id"],
                    right_global_id=self.right_row["global_id"],
                    category=category,
                )

    # -- entity type ----------------------------------------------------
    def _compare_entity_type(self) -> None:
        if self.left_row["entity_type"] == self.right_row["entity_type"]:
            return
        record = ChangeRecord(
            category=CAT_SEMANTIC_CHANGE,
            subject="entity_type",
            before={"entity_type": self.left_row["entity_type"]},
            after={"entity_type": self.right_row["entity_type"]},
            evidence=self.engine._evidence_for_rows(
                [self.left_row], [self.right_row]
            ),
            **self._base_kwargs(ASPECT_ENTITY_TYPE),
        )
        self._emit(record)

    # -- properties -----------------------------------------------------
    def _compare_properties(self) -> None:
        left_rows = list(
            self.engine.left.stream_resolved([self.left_row["key"]])
        )
        right_rows = list(
            self.engine.right.stream_resolved([self.right_row["key"]])
        )
        left_by_id = {r["identity"]: r for r in left_rows}
        right_by_id = {r["identity"]: r for r in right_rows}
        for identity in sorted(set(left_by_id) | set(right_by_id)):
            lr = left_by_id.get(identity)
            rr = right_by_id.get(identity)
            self._compare_one_property(identity, lr, rr)

    def _compare_one_property(
        self, identity: str, left_row: Any, right_row: Any
    ) -> None:
        subject = f"property:{identity}"
        if left_row is None:
            self._property_semantic(identity, subject, None, right_row, appeared=True)
            return
        if right_row is None:
            self._property_semantic(identity, subject, left_row, None, disappeared=True)
            return
        if (
            left_row["state"] in fp.NON_COMPARABLE_STATES
            or right_row["state"] in fp.NON_COMPARABLE_STATES
        ):
            self._emit_property_incomparable(identity, subject, left_row, right_row)
            return
        left_fp = fp.property_business_fingerprint(left_row)
        right_fp = fp.property_business_fingerprint(right_row)
        left_prov = fp.property_provenance(left_row)
        right_prov = fp.property_provenance(right_row)
        evidence = self._property_evidence(left_row, right_row)
        if left_fp != right_fp:
            record = ChangeRecord(
                category=CAT_SEMANTIC_CHANGE,
                subject=subject,
                locator={"property_identity": identity},
                before=left_fp,
                after=right_fp,
                before_provenance=left_prov,
                after_provenance=right_prov,
                evidence=evidence,
                **self._base_kwargs(ASPECT_PROPERTY),
            )
            self._emit(record)
            return
        if self._provenance_changed(left_prov, right_prov):
            record = ChangeRecord(
                category=CAT_PROVENANCE_CHANGE,
                subject=subject,
                locator={"property_identity": identity},
                before=left_fp,
                after=right_fp,
                before_provenance=left_prov,
                after_provenance=right_prov,
                reason=(
                    "effective value and state are unchanged; the declaration "
                    "location changed"
                ),
                evidence=evidence,
                **self._base_kwargs(ASPECT_PROPERTY),
            )
            self._emit(record)

    @staticmethod
    def _provenance_changed(left_prov: dict[str, Any], right_prov: dict[str, Any]) -> bool:
        key_left = (
            left_prov["scope_used"],
            left_prov["had_type_declaration_masked"],
        )
        key_right = (
            right_prov["scope_used"],
            right_prov["had_type_declaration_masked"],
        )
        return key_left != key_right

    def _property_semantic(
        self,
        identity: str,
        subject: str,
        left_row: Any,
        right_row: Any,
        *,
        appeared: bool = False,
        disappeared: bool = False,
    ) -> None:
        absent = {"state": fp.STATE_ABSENT}
        before = absent if appeared else fp.property_business_fingerprint(left_row)
        after = absent if disappeared else fp.property_business_fingerprint(right_row)
        before_prov = (
            None if appeared else fp.property_provenance(left_row)
        )
        after_prov = (
            None if disappeared else fp.property_provenance(right_row)
        )
        evidence = self._property_evidence(left_row, right_row)
        record = ChangeRecord(
            category=CAT_SEMANTIC_CHANGE,
            subject=subject,
            locator={"property_identity": identity},
            before=before,
            after=after,
            before_provenance=before_prov,
            after_provenance=after_prov,
            evidence=evidence,
            **self._base_kwargs(ASPECT_PROPERTY),
        )
        self._emit(record)

    def _emit_property_incomparable(
        self, identity: str, subject: str, left_row: Any, right_row: Any
    ) -> None:
        record = ChangeRecord(
            category=CAT_INCOMPARABLE,
            subject=subject,
            locator={"property_identity": identity},
            before={"state": left_row["state"], "value_status": left_row["value_status"]},
            after={"state": right_row["state"], "value_status": right_row["value_status"]},
            reason="property is in a conflict state on at least one side",
            evidence=self._property_evidence(left_row, right_row),
            **self._base_kwargs(ASPECT_PROPERTY),
        )
        self._emit(record)

    def _property_evidence(self, left_row: Any, right_row: Any) -> list[dict[str, Any]]:
        extra_left: list[tuple[str, int]] = []
        extra_right: list[tuple[str, int]] = []
        if left_row is not None:
            prov = fp.property_provenance(left_row)
            if prov["prop_step_id"] is not None:
                extra_left.append((left_row["source_id"], prov["prop_step_id"]))
            if prov["pset_step_id"] is not None:
                extra_left.append((left_row["source_id"], prov["pset_step_id"]))
        if right_row is not None:
            prov = fp.property_provenance(right_row)
            if prov["prop_step_id"] is not None:
                extra_right.append((right_row["source_id"], prov["prop_step_id"]))
            if prov["pset_step_id"] is not None:
                extra_right.append((right_row["source_id"], prov["pset_step_id"]))
        return self.engine._evidence_for_rows(
            [self.left_row],
            [self.right_row],
            extra_left=extra_left,
            extra_right=extra_right,
        )

    # -- spatial relations ----------------------------------------------
    def _compare_spatial(self) -> None:
        # Only contained objects move between spaces.  The spatial structure
        # entities themselves (site/building/storey/space) define the
        # containers; diffing their own decomposition chains would turn one
        # equipment move into a cascade of structural records.
        if bool(self.left_row["is_spatial"]) or bool(self.right_row["is_spatial"]):
            return
        left_edges = self._direct_spatial_edges(
            self.engine.left, self.left_row
        )
        right_edges = self._direct_spatial_edges(
            self.engine.right, self.right_row
        )
        # An edge whose relation exists but whose parent endpoint could not be
        # resolved is stored with a null parent_key.  It is not a usable spatial
        # edge and must not be turned into a confident move to "unresolved".
        bad_left = [
            e for e in left_edges if e["edge_state"] != "ok" or e["parent_key"] is None
        ]
        bad_right = [
            e for e in right_edges if e["edge_state"] != "ok" or e["parent_key"] is None
        ]
        if bad_left or bad_right:
            self._emit_spatial_incomparable(left_edges, right_edges, bad_left, bad_right)
            return
        # The semantic parent is identified cross-version by the parent's
        # GlobalId and edge kind.  relation_key/ordinal carry per-source SHA
        # fragments and STEP numbering; they are locators, never identity.
        left_targets = self._spatial_target_map(self.engine.left, left_edges)
        right_targets = self._spatial_target_map(self.engine.right, right_edges)
        if set(left_targets) == set(right_targets):
            return
        for token in sorted(set(left_targets) - set(right_targets)):
            target, edge = left_targets[token]
            self._emit_spatial_move(target, edge, None, left_edges, right_edges)
        for token in sorted(set(right_targets) - set(left_targets)):
            target, edge = right_targets[token]
            self._emit_spatial_move(target, None, edge, left_edges, right_edges)

    def _spatial_target_map(
        self, snapshot: RepoSnapShot, edges: Sequence[sqlite3.Row]
    ) -> dict[tuple[str, str], tuple[dict[str, Any], sqlite3.Row]]:
        out: dict[tuple[str, str], tuple[dict[str, Any], sqlite3.Row]] = {}
        for edge in edges:
            target = self._edge_target(snapshot, edge)
            token = (edge["kind"], target.get("global_id") or target.get("parent_key") or "")
            out.setdefault(token, (target, edge))
        return out

    @staticmethod
    def _direct_spatial_edges(
        snapshot: RepoSnapShot, row: sqlite3.Row
    ) -> list[sqlite3.Row]:
        """Direct containment/decomposition edges whose child is this object."""

        edges = [
            e
            for e in snapshot.stream_spatial_edges([row["key"]])
            if e["child_key"] == row["key"]
        ]
        # Deduplicate by (kind, parent) so a re-issued relation cannot double
        # count the same effective spatial parent.
        unique: dict[tuple[str, Any], sqlite3.Row] = {}
        for edge in edges:
            token = (edge["kind"], edge["parent_key"], edge["edge_state"])
            unique.setdefault(token, edge)
        return list(unique.values())

    def _edge_target(self, snapshot: RepoSnapShot, edge: sqlite3.Row) -> dict[str, Any]:
        parent_key = edge["parent_key"]
        if parent_key is None:
            return {"kind": "unresolved", "relation_type": edge["relation_type"]}
        parent = snapshot.root_by_key(parent_key)
        if parent is None:
            return {"kind": "unresolved", "parent_key": parent_key}
        return {
            "kind": "global_id" if parent["global_id"] else "step_scoped",
            "source_id": parent["source_id"],
            "global_id": parent["global_id"],
            "entity_type": parent["entity_type"],
            "name": parent["name"],
        }

    def _emit_spatial_move(
        self,
        target: dict[str, Any],
        left_edge: sqlite3.Row | None,
        right_edge: sqlite3.Row | None,
        all_left: Sequence[sqlite3.Row],
        all_right: Sequence[sqlite3.Row],
    ) -> None:
        anchor = left_edge or right_edge
        before = (
            {**self._edge_locator(self.engine.left, left_edge), "parent": target}
            if left_edge is not None
            else None
        )
        after = (
            {**self._edge_locator(self.engine.right, right_edge), "parent": target}
            if right_edge is not None
            else None
        )
        extra_left = [
            (e["source_id"], e["child_step_id"]) for e in all_left
        ] + [(e["source_id"], self._relation_step(self.engine.left, e)) for e in all_left]
        extra_right = [
            (e["source_id"], e["child_step_id"]) for e in all_right
        ] + [(e["source_id"], self._relation_step(self.engine.right, e)) for e in all_right]
        record = ChangeRecord(
            category=CAT_SEMANTIC_CHANGE,
            subject=f"spatial_parent:{target.get('global_id') or target.get('parent_key') or 'unresolved'}",
            locator={
                "edge_kind": anchor["kind"],
                "relation_type": anchor["relation_type"],
                "direction": "parent_removed" if right_edge is None else "parent_added",
            },
            before=before,
            after=after,
            evidence=self.engine._evidence_for_rows(
                [self.left_row],
                [self.right_row],
                extra_left=[t for t in extra_left if t[1] is not None],
                extra_right=[t for t in extra_right if t[1] is not None],
            ),
            **self._base_kwargs(ASPECT_SPATIAL),
        )
        self._emit(record)

    def _emit_spatial_incomparable(
        self,
        left_edges: Sequence[sqlite3.Row],
        right_edges: Sequence[sqlite3.Row],
        bad_left: Sequence[sqlite3.Row],
        bad_right: Sequence[sqlite3.Row],
    ) -> None:
        def describe(edges: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
            return [
                {
                    "relation_type": e["relation_type"],
                    "edge_state": e["edge_state"],
                    "parent_key": e["parent_key"],
                    "reason": e["reason"],
                    "relation_key": e["relation_key"],
                }
                for e in edges
                if e["edge_state"] != "ok" or e["parent_key"] is None
            ]

        extra_left = [
            (e["source_id"], self._relation_step(self.engine.left, e))
            for e in bad_left
        ]
        extra_right = [
            (e["source_id"], self._relation_step(self.engine.right, e))
            for e in bad_right
        ]
        record = ChangeRecord(
            category=CAT_INCOMPARABLE,
            subject="spatial_parent",
            locator={"bad_edges_left": len(bad_left), "bad_edges_right": len(bad_right)},
            before={"bad_edges": describe(left_edges)},
            after={"bad_edges": describe(right_edges)},
            reason="a spatial edge is missing or conflicted; movement cannot be determined",
            evidence=self.engine._evidence_for_rows(
                [self.left_row],
                [self.right_row],
                extra_left=[t for t in extra_left if t[1] is not None],
                extra_right=[t for t in extra_right if t[1] is not None],
            ),
            **self._base_kwargs(ASPECT_SPATIAL),
        )
        self._emit(record)

    @staticmethod
    def _relation_step(snapshot: RepoSnapShot, edge: sqlite3.Row) -> int | None:
        row = snapshot.conn.execute(
            "SELECT step_id FROM relations WHERE relation_key=?",
            (edge["relation_key"],),
        ).fetchone()
        return None if row is None else int(row["step_id"])

    @staticmethod
    def _edge_locator(snapshot: RepoSnapShot, edge: sqlite3.Row) -> dict[str, Any]:
        parent = snapshot.root_by_key(edge["parent_key"]) if edge["parent_key"] else None
        return {
            "kind": edge["kind"],
            "relation_type": edge["relation_type"],
            "relation_key": edge["relation_key"],
            "ordinal": edge["ordinal"],
            "parent_global_id": None if parent is None else parent["global_id"],
            "parent_name": None if parent is None else parent["name"],
            "parent_entity_type": None if parent is None else parent["entity_type"],
        }

    # -- type assignment ------------------------------------------------
    def _compare_type_assignment(self) -> None:
        left_type = self._type_descriptor(self.engine.left, self.left_row)
        right_type = self._type_descriptor(self.engine.right, self.right_row)
        if left_type == right_type:
            return
        # If either side's type cannot be resolved to a GlobalId, that is
        # incomparable evidence rather than a confident type change.
        if left_type is not None and left_type.get("kind") == "unresolved":
            self._emit_type_incomparable(left_type, right_type)
            return
        if right_type is not None and right_type.get("kind") == "unresolved":
            self._emit_type_incomparable(left_type, right_type)
            return
        record = ChangeRecord(
            category=CAT_SEMANTIC_CHANGE,
            subject="type_assignment",
            before=left_type,
            after=right_type,
            evidence=self.engine._evidence_for_rows(
                [self.left_row], [self.right_row]
            ),
            **self._base_kwargs(ASPECT_TYPE_ASSIGNMENT),
        )
        self._emit(record)

    def _type_descriptor(
        self, snapshot: RepoSnapShot, row: sqlite3.Row
    ) -> dict[str, Any] | None:
        type_key = row["type_key"]
        if not type_key:
            return None
        type_row = snapshot.root_by_key(type_key)
        if type_row is None:
            return {"kind": "unresolved", "type_key": type_key}
        # Cross-version identity is the type's GlobalId.  The per-side
        # source_id is provenance, kept out of the comparison payload.
        return {
            "kind": "global_id" if type_row["global_id"] else "step_scoped",
            "global_id": type_row["global_id"],
            "entity_type": type_row["entity_type"],
            "name": type_row["name"],
        }

    def _emit_type_incomparable(
        self, left_type: Any, right_type: Any
    ) -> None:
        record = ChangeRecord(
            category=CAT_INCOMPARABLE,
            subject="type_assignment",
            before=left_type,
            after=right_type,
            reason="type assignment target cannot be resolved to a cross-version identity",
            evidence=self.engine._evidence_for_rows(
                [self.left_row], [self.right_row]
            ),
            **self._base_kwargs(ASPECT_TYPE_ASSIGNMENT),
        )
        self._emit(record)

    # -- materials ------------------------------------------------------
    def _compare_materials(self) -> None:
        # A type's material tree is inherited by every instance.  Comparing it
        # once on the instance (as the effective assignment) avoids emitting
        # the same layer change once for the type and again for each instance.
        if bool(self.left_row["is_type"]) or bool(self.right_row["is_type"]):
            return
        left_assignments = self._effective_material_assignments(
            self.engine.left, self.left_row
        )
        right_assignments = self._effective_material_assignments(
            self.engine.right, self.right_row
        )
        left_bad = [a for a in left_assignments if a["state"] != "ok"]
        right_bad = [a for a in right_assignments if a["state"] != "ok"]
        if left_bad or right_bad:
            self._emit_material_incomparable(left_assignments, right_assignments)
            return
        if not left_assignments and not right_assignments:
            return
        if not left_assignments or not right_assignments:
            self._emit_material_added_removed(left_assignments, right_assignments)
            return
        left_tree = self._material_fingerprint(self.engine.left, left_assignments[0])
        right_tree = self._material_fingerprint(self.engine.right, right_assignments[0])
        if left_tree == right_tree:
            return
        record = ChangeRecord(
            category=CAT_SEMANTIC_CHANGE,
            subject="material_structure",
            locator={
                "left_assignment": left_assignments[0]["relation_key"],
                "right_assignment": right_assignments[0]["relation_key"],
                "left_inherited_from_type": bool(
                    (left_assignments[0].get("tree") or {}).get("inherited_from_type")
                ),
                "right_inherited_from_type": bool(
                    (right_assignments[0].get("tree") or {}).get("inherited_from_type")
                ),
            },
            before=left_tree,
            after=right_tree,
            evidence=self._material_evidence(left_assignments, right_assignments),
            **self._base_kwargs(ASPECT_MATERIAL),
        )
        self._emit(record)

    def _effective_material_assignments(
        self, snapshot: RepoSnapShot, row: sqlite3.Row
    ) -> list[dict[str, Any]]:
        assignments = self._material_dicts(
            snapshot.material_assignments([row["key"]])
        )
        direct = [
            a
            for a in assignments
            if not (a.get("tree") or {}).get("inherited_from_type")
        ]
        return direct if direct else assignments

    @staticmethod
    def _material_dicts(rows: Any) -> list[dict[str, Any]]:
        return [dict(r) if isinstance(r, sqlite3.Row) else r for r in rows]

    def _material_fingerprint(
        self, snapshot: RepoSnapShot, assignment: dict[str, Any]
    ) -> dict[str, Any]:
        root_key = assignment.get("root_key")
        if not root_key:
            return {"state": assignment["state"], "layers": []}
        subtree = snapshot.material_subtree([root_key])
        return fp.material_tree_fingerprint(subtree["nodes"], subtree["edges"])

    def _emit_material_added_removed(
        self, left_assignments: Sequence[dict[str, Any]],
        right_assignments: Sequence[dict[str, Any]],
    ) -> None:
        record = ChangeRecord(
            category=CAT_SEMANTIC_CHANGE,
            subject="material_assignment",
            before={"assignments": len(left_assignments)} if left_assignments else None,
            after={"assignments": len(right_assignments)} if right_assignments else None,
            evidence=self._material_evidence(left_assignments, right_assignments),
            **self._base_kwargs(ASPECT_MATERIAL),
        )
        self._emit(record)

    def _emit_material_incomparable(
        self,
        left_assignments: Sequence[dict[str, Any]],
        right_assignments: Sequence[dict[str, Any]],
    ) -> None:
        record = ChangeRecord(
            category=CAT_INCOMPARABLE,
            subject="material_structure",
            before={"states": [a["state"] for a in left_assignments]},
            after={"states": [a["state"] for a in right_assignments]},
            reason="a material assignment is unresolved or conflicted on one side",
            evidence=self._material_evidence(left_assignments, right_assignments),
            **self._base_kwargs(ASPECT_MATERIAL),
        )
        self._emit(record)

    def _material_evidence(
        self,
        left_assignments: Sequence[dict[str, Any]],
        right_assignments: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        extra_left: list[tuple[str, int]] = []
        extra_right: list[tuple[str, int]] = []
        for assignment in left_assignments:
            step = self._assignment_relation_step(self.engine.left, assignment)
            if step is not None:
                extra_left.append((assignment["source_id"], step))
            if assignment.get("material_step_id") is not None:
                extra_left.append(
                    (assignment["source_id"], assignment["material_step_id"])
                )
        for assignment in right_assignments:
            step = self._assignment_relation_step(self.engine.right, assignment)
            if step is not None:
                extra_right.append((assignment["source_id"], step))
            if assignment.get("material_step_id") is not None:
                extra_right.append(
                    (assignment["source_id"], assignment["material_step_id"])
                )
        return self.engine._evidence_for_rows(
            [self.left_row],
            [self.right_row],
            extra_left=extra_left,
            extra_right=extra_right,
        )

    @staticmethod
    def _assignment_relation_step(
        snapshot: RepoSnapShot, assignment: dict[str, Any]
    ) -> int | None:
        relation_key = assignment.get("relation_key")
        if not relation_key:
            return None
        row = snapshot.conn.execute(
            "SELECT step_id FROM relations WHERE relation_key=?", (relation_key,)
        ).fetchone()
        return None if row is None else int(row["step_id"])
