"""Per-checkpoint evaluation: visibility sweep, coverage classification, decisions.

The ledger is swept exactly once.  Checkpoints are grouped by their visibility
cutoff ``known_at``; the groups are processed in ascending cutoff order while a
streaming cursor over the on-disk revision index advances the ``visible``
table, so the visible revision of every signoff (the newest revision recorded
at or before the cutoff) is maintained incrementally -- the ledger is never
re-scanned per checkpoint and never iterated per device.

For each checkpoint the selected revision of every associated signoff is
classified in a documented priority order:

1. ``rejected`` / ``withdrawn``              -> excluded
2. validity interval misses ``effective_at`` -> excluded (out_of_validity)
3. target identity is ambiguous              -> pending (coverage_unverified)
4. target kind inconsistent with the object  -> excluded (invalid_target)
5. coverage mode forbids space signoffs      -> excluded (out_of_coverage)
6. target beyond the zone root / outside     -> excluded (out_of_coverage)
7. candidate path uses invalid edges         -> pending (coverage_unverified)
8. coverage confirmed, document missing      -> pending (file_missing)
9. coverage confirmed, hash mismatch         -> pending (file_hash_mismatch)
10. otherwise                                -> counted

A signoff never resurrects: only the single visible revision is judged, so a
withdrawal, rejection or narrowed validity can never make an older revision
effective again, while a later-recorded revision still covers an earlier
business moment retroactively when its validity interval reaches back.

All aggregation runs as streaming merges over ordered SQLite cursors; Python
memory stays bounded by one matter record, not by the device or revision
totals.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Iterator

from ..keys import canonical_json
from .dataset import (
    ANOMALY_AMBIGUOUS_TARGET,
    ANOMALY_INVALID_TARGET,
    ANOMALY_NO_ASSOCIATION,
    CLASS_COUNTED,
    CLASS_EXCLUDED,
    CLASS_PENDING,
    DECISION_EMPTY_SCOPE,
    DECISION_HELD,
    DECISION_RELEASED,
    DECISION_SCOPE_UNVERIFIED,
    MATTER_STATUSES,
    REASON_COVERAGE_UNVERIFIED,
    REASON_FILE_HASH_MISMATCH,
    REASON_FILE_MISSING,
    REASON_INVALID_TARGET,
    REASON_OUT_OF_COVERAGE,
    REASON_OUT_OF_VALIDITY,
    REASON_REJECTED,
    REASON_WITHDRAWN,
    SCOPE_CONFIRMED,
    STATUS_BLOCKED,
    STATUS_READY,
    STATUS_UNDETERMINED,
)
from .inputs import (
    COVERAGE_DEVICE_AND_SPACE,
    DECISION_REJECTED,
    DECISION_WITHDRAWN,
    TARGET_DEVICE,
    Checkpoint,
)
from .scope import (
    ASSOC_DEVICE,
    ASSOC_SPACE_BEYOND_ROOT,
    ASSOC_SPACE_CANDIDATE,
    ASSOC_SPACE_DEFECTIVE_OUTSIDE,
    ASSOC_SPACE_VALID,
)

# Cap on the number of scope issues copied into a zone decision's reasons;
# the full list stays queryable in zone_scope_issues.
_MAX_REASON_ISSUES = 20

# Internal sentinel before a signoff becomes counted/pending via the file check.
_CONFIRMED = "confirmed"


class ReleaseEngine:
    def __init__(
        self,
        work_conn: sqlite3.Connection,
        repo_conn: sqlite3.Connection,
        ledger_conn: sqlite3.Connection,
        ledger_dir: str,
        rule_matter_codes: set[str],
    ):
        self.work = work_conn
        self.repo = repo_conn
        self.ledger = ledger_conn
        self.ledger_dir = os.path.abspath(ledger_dir)
        self.ledger_dir_real = os.path.realpath(self.ledger_dir)
        self.rule_matter_codes = rule_matter_codes
        self._evidence_buffer: list[tuple[Any, ...]] = []

    # ------------------------------------------------------------------
    def run(self, checkpoints: list[Checkpoint]) -> None:
        groups: dict[str, list[Checkpoint]] = {}
        for checkpoint in checkpoints:
            groups.setdefault(checkpoint.known_at, []).append(checkpoint)
        cursor = self.ledger.execute(
            "SELECT * FROM revisions ORDER BY recorded_at, revision"
        )
        pending_iter: Iterator[sqlite3.Row] = iter(cursor)
        pending: sqlite3.Row | None = None
        for known_at in sorted(groups):
            # Advance the visibility sweep to this cutoff.
            batch: list[tuple[Any, ...]] = []
            while True:
                if pending is None:
                    pending = next(pending_iter, None)
                if pending is None or pending["recorded_at"] > known_at:
                    break
                row = pending
                pending = None
                batch.append(
                    (
                        row["signoff_id"],
                        row["revision"],
                        row["recorded_at"],
                        row["valid_from"],
                        row["valid_to"],
                        row["decision"],
                        row["signatory"],
                        row["matter_code"],
                        row["target_kind"],
                        row["target_source_id"],
                        row["target_global_id"],
                        row["doc_path"],
                        row["doc_sha256"],
                        row["line_no"],
                    )
                )
                if len(batch) >= 500:
                    self._insert_visible(batch)
                    batch = []
            if batch:
                self._insert_visible(batch)
            self._build_assoc()
            for checkpoint in groups[known_at]:
                self._evaluate_checkpoint(checkpoint)
            # Anomalies are identical for every checkpoint of the group.
            self._write_anomalies(groups[known_at])
        self.work.commit()

    def _insert_visible(self, batch: list[tuple[Any, ...]]) -> None:
        self.work.executemany(
            "INSERT OR REPLACE INTO visible(signoff_id, revision, recorded_at, valid_from, "
            "valid_to, decision, signatory, matter_code, target_kind, target_source_id, "
            "target_global_id, doc_path, doc_sha256, line_no) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch,
        )

    # ------------------------------------------------------------------
    # Association: visible revisions x repository targets x matter records
    # ------------------------------------------------------------------
    def _build_assoc(self) -> None:
        self.work.execute("DELETE FROM assoc")
        rows: list[tuple[Any, ...]] = []
        for rev in self.work.execute("SELECT * FROM visible"):
            if rev["matter_code"] not in self.rule_matter_codes:
                continue
            targets = list(
                self.repo.execute(
                    "SELECT key, step_id, entity_type, is_spatial FROM roots "
                    "WHERE source_id=? AND global_id=? ORDER BY step_id",
                    (rev["target_source_id"], rev["target_global_id"]),
                )
            )
            if not targets:
                continue  # anomaly candidate, handled separately
            for target in targets:
                for rt in self.work.execute(
                    "SELECT rt.record_id, rt.assoc_kind, rt.path_json FROM record_targets rt "
                    "JOIN matters m ON m.record_id = rt.record_id "
                    "WHERE rt.object_key=? AND m.matter_code=?",
                    (target["key"], rev["matter_code"]),
                ):
                    rows.append(
                        (
                            rt["record_id"],
                            rev["signoff_id"],
                            rev["revision"],
                            rt["assoc_kind"],
                            rt["path_json"],
                            target["key"],
                            target["step_id"],
                            target["entity_type"],
                            1 if target["is_spatial"] else 0,
                            len(targets),
                        )
                    )
                    if len(rows) >= 500:
                        self._insert_assoc(rows)
                        rows = []
        if rows:
            self._insert_assoc(rows)

    def _insert_assoc(self, rows: list[tuple[Any, ...]]) -> None:
        self.work.executemany(
            "INSERT OR REPLACE INTO assoc(record_id, signoff_id, revision, assoc_kind, "
            "path_json, target_key, target_step_id, target_entity_type, target_is_spatial, "
            "target_row_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    # ------------------------------------------------------------------
    # Per-checkpoint evaluation
    # ------------------------------------------------------------------
    def _evaluate_checkpoint(self, checkpoint: Checkpoint) -> None:
        self._classify_associations(checkpoint)
        self._aggregate_matters(checkpoint)
        self._decide_zones(checkpoint)
        self._flush_evidence()
        self.work.commit()

    def _classify_associations(self, checkpoint: Checkpoint) -> None:
        sql = (
            "SELECT a.record_id, a.signoff_id, a.revision, a.assoc_kind, a.path_json, "
            "a.target_key, a.target_step_id, a.target_entity_type, a.target_is_spatial, "
            "a.target_row_count, v.decision, v.signatory, v.matter_code, v.target_kind, "
            "v.target_source_id, v.target_global_id, v.valid_from, v.valid_to, v.recorded_at, "
            "v.doc_path, v.doc_sha256, v.line_no, m.coverage_mode "
            "FROM assoc a "
            "JOIN visible v ON v.signoff_id=a.signoff_id AND v.revision=a.revision "
            "JOIN matters m ON m.record_id=a.record_id "
            "ORDER BY a.record_id, a.signoff_id, a.target_key"
        )
        out: list[tuple[Any, ...]] = []
        group: list[sqlite3.Row] = []
        current: tuple[str, str] | None = None
        for row in self.work.execute(sql):
            token = (row["record_id"], row["signoff_id"])
            if current is not None and token != current:
                out.append(self._classify_group(checkpoint, group))
                group = []
                if len(out) >= 200:
                    self._insert_signoffs(out)
                    out = []
            current = token
            group.append(row)
        if group:
            out.append(self._classify_group(checkpoint, group))
        if out:
            self._insert_signoffs(out)

    def _insert_signoffs(self, rows: list[tuple[Any, ...]]) -> None:
        self.work.executemany(
            "INSERT OR REPLACE INTO matter_signoffs(checkpoint_idx, record_id, signoff_id, "
            "revision, line_no, signatory, decision, recorded_at, valid_from, valid_to, "
            "matter_code, target_kind, target_source_id, target_global_id, classification, "
            "reason, reason_detail, doc_path, doc_declared_sha256, doc_computed_sha256, "
            "doc_status, coverage_path_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def _classify_group(self, checkpoint: Checkpoint, rows: list[sqlite3.Row]) -> tuple[Any, ...]:
        first = rows[0]
        record_id = first["record_id"]
        signoff_id = first["signoff_id"]
        classification: str
        reason: str | None = None
        detail: str | None = None
        doc_status: str | None = None
        doc_computed: str | None = None
        coverage_path: str | None = None

        decision = first["decision"]
        valid_from = first["valid_from"]
        valid_to = first["valid_to"]
        in_validity = valid_from <= checkpoint.effective_at and (
            valid_to is None or checkpoint.effective_at < valid_to
        )
        target_row_count = first["target_row_count"]
        target_kind = first["target_kind"]
        coverage_mode = first["coverage_mode"]

        if decision == DECISION_REJECTED:
            classification, reason = CLASS_EXCLUDED, REASON_REJECTED
        elif decision == DECISION_WITHDRAWN:
            classification, reason = CLASS_EXCLUDED, REASON_WITHDRAWN
        elif not in_validity:
            classification, reason = CLASS_EXCLUDED, REASON_OUT_OF_VALIDITY
            detail = (
                f"validity [{valid_from}, {valid_to or 'open'}) does not cover "
                f"effective_at {checkpoint.effective_at}"
            )
        elif target_row_count > 1:
            classification, reason = CLASS_PENDING, REASON_COVERAGE_UNVERIFIED
            detail = (
                f"target identity {first['target_source_id']}:"
                f"{first['target_global_id']} resolves to {target_row_count} "
                "repository objects; coverage cannot be confirmed"
            )
            coverage_path = first["path_json"]
            self._evidence_for_identity_conflict(record_id, signoff_id, first)
        elif (target_kind == TARGET_DEVICE and first["target_is_spatial"]) or (
            target_kind != TARGET_DEVICE and not first["target_is_spatial"]
        ):
            classification, reason = CLASS_EXCLUDED, REASON_INVALID_TARGET
            detail = (
                f"signoff target_kind is {target_kind!r} but the repository object "
                f"is {first['target_entity_type']}"
            )
        else:
            assoc_kind = first["assoc_kind"]
            if assoc_kind == ASSOC_DEVICE:
                classification = _CONFIRMED
            elif assoc_kind == ASSOC_SPACE_VALID:
                if coverage_mode == COVERAGE_DEVICE_AND_SPACE:
                    classification = _CONFIRMED
                    coverage_path = first["path_json"]
                else:
                    classification, reason = CLASS_EXCLUDED, REASON_OUT_OF_COVERAGE
                    detail = "coverage mode 'device' does not allow space signoffs"
            elif assoc_kind == ASSOC_SPACE_BEYOND_ROOT:
                classification, reason = CLASS_EXCLUDED, REASON_OUT_OF_COVERAGE
                detail = "target is a valid ancestor but lies beyond the zone root"
            elif assoc_kind == ASSOC_SPACE_CANDIDATE:
                classification, reason = CLASS_PENDING, REASON_COVERAGE_UNVERIFIED
                detail = "candidate path from the zone root to the device uses invalid edges"
                coverage_path = first["path_json"]
                self._evidence_for_path(record_id, signoff_id, first)
            else:  # ASSOC_SPACE_DEFECTIVE_OUTSIDE
                classification, reason = CLASS_EXCLUDED, REASON_OUT_OF_COVERAGE
                detail = "target is outside the zone and reachable only over invalid edges"

            if classification == _CONFIRMED:
                doc_status, doc_computed = self._check_file(
                    first["doc_path"], first["doc_sha256"]
                )
                if doc_status == "ok":
                    classification = CLASS_COUNTED
                    reason = None
                    if assoc_kind == ASSOC_SPACE_VALID:
                        self._evidence_for_path(record_id, signoff_id, first)
                    else:
                        self._evidence_for_target(record_id, signoff_id, first)
                elif doc_status == "missing":
                    classification, reason = CLASS_PENDING, REASON_FILE_MISSING
                    detail = f"document {first['doc_path']} is missing"
                else:
                    classification, reason = CLASS_PENDING, REASON_FILE_HASH_MISMATCH
                    detail = (
                        f"document {first['doc_path']} hashes to {doc_computed}, "
                        f"declared {first['doc_sha256']}"
                    )

        return (
            checkpoint.idx,
            record_id,
            signoff_id,
            first["revision"],
            first["line_no"],
            first["signatory"],
            decision,
            first["recorded_at"],
            valid_from,
            valid_to,
            first["matter_code"],
            target_kind,
            first["target_source_id"],
            first["target_global_id"],
            classification,
            reason,
            detail,
            first["doc_path"],
            first["doc_sha256"],
            doc_computed,
            doc_status,
            coverage_path,
        )

    # -- file checks ------------------------------------------------------
    def _check_file(self, doc_path: str | None, declared_sha256: str | None) -> tuple[str, str | None]:
        if doc_path is None or declared_sha256 is None:
            return "missing", None
        row = self.work.execute(
            "SELECT status, computed_sha256 FROM file_checks WHERE path=? AND declared_sha256=?",
            (doc_path, declared_sha256),
        ).fetchone()
        if row is not None:
            return row["status"], row["computed_sha256"]
        full = os.path.join(self.ledger_dir, *doc_path.split("/"))
        computed: str | None = None
        try:
            real = os.path.realpath(full)
            if os.path.commonpath([real, self.ledger_dir_real]) != self.ledger_dir_real:
                status = "missing"
            else:
                digest = hashlib.sha256()
                with open(real, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(chunk)
                computed = digest.hexdigest()
                status = "ok" if computed == declared_sha256 else "hash_mismatch"
        except (OSError, ValueError):
            # OSError: unreadable file; ValueError: commonpath on mixed drives.
            status = "missing"
            computed = None
        self.work.execute(
            "INSERT OR REPLACE INTO file_checks(path, declared_sha256, computed_sha256, status) "
            "VALUES(?,?,?,?)",
            (doc_path, declared_sha256, computed, status),
        )
        return status, computed

    # -- evidence ---------------------------------------------------------
    def _evidence_owner(self, record_id: str, signoff_id: str) -> str:
        return f"signoff:{record_id}:{signoff_id}"

    def _add_entity_evidence(self, owner: str, source_id: str, step_id: int) -> None:
        for row in self.repo.execute(
            "SELECT * FROM source_entities WHERE source_id=? AND step_id=?",
            (source_id, step_id),
        ):
            self._evidence_buffer.append(
                (
                    "signoff",
                    owner,
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
            if len(self._evidence_buffer) >= 200:
                self._flush_evidence()

    def _evidence_for_target(self, record_id: str, signoff_id: str, row: sqlite3.Row) -> None:
        self._add_entity_evidence(
            self._evidence_owner(record_id, signoff_id),
            row["target_source_id"],
            row["target_step_id"],
        )

    def _evidence_for_path(self, record_id: str, signoff_id: str, row: sqlite3.Row) -> None:
        owner = self._evidence_owner(record_id, signoff_id)
        self._add_entity_evidence(owner, row["target_source_id"], row["target_step_id"])
        path_json = row["path_json"]
        if not path_json:
            return
        path = json.loads(path_json)
        relation_keys = {hop["relation_key"] for hop in path.get("hops", [])}
        for relation_key in sorted(relation_keys):
            rel = self.repo.execute(
                "SELECT source_id, step_id FROM relations WHERE relation_key=?",
                (relation_key,),
            ).fetchone()
            if rel is not None:
                self._add_entity_evidence(owner, rel["source_id"], rel["step_id"])

    def _evidence_for_identity_conflict(
        self, record_id: str, signoff_id: str, row: sqlite3.Row
    ) -> None:
        owner = self._evidence_owner(record_id, signoff_id)
        for target in self.repo.execute(
            "SELECT step_id FROM roots WHERE source_id=? AND global_id=? ORDER BY step_id",
            (row["target_source_id"], row["target_global_id"]),
        ):
            self._add_entity_evidence(owner, row["target_source_id"], target["step_id"])

    def _flush_evidence(self) -> None:
        if not self._evidence_buffer:
            return
        self.work.executemany(
            "INSERT OR IGNORE INTO evidence(owner_kind, owner_id, source_id, sha256, step_id, "
            "entity_type, role, line_start, line_end, raw_text) VALUES(?,?,?,?,?,?,?,?,?,?)",
            self._evidence_buffer,
        )
        self._evidence_buffer.clear()

    # ------------------------------------------------------------------
    # Aggregation (streaming merges; nothing per-device accumulates in Python)
    # ------------------------------------------------------------------
    def _aggregate_matters(self, checkpoint: Checkpoint) -> None:
        counts_iter = iter(
            self.work.execute(
                "SELECT record_id, "
                "COUNT(DISTINCT CASE WHEN classification='counted' THEN signatory END) verified, "
                "COUNT(DISTINCT CASE WHEN classification IN ('counted','pending') THEN signatory END) reach "
                "FROM matter_signoffs WHERE checkpoint_idx=? GROUP BY record_id "
                "ORDER BY record_id",
                (checkpoint.idx,),
            )
        )
        lists_iter = iter(
            self.work.execute(
                "SELECT DISTINCT record_id, classification, signatory FROM matter_signoffs "
                "WHERE checkpoint_idx=? AND classification IN ('counted','pending') "
                "ORDER BY record_id, classification, signatory",
                (checkpoint.idx,),
            )
        )
        matters_iter = iter(
            self.work.execute(
                "SELECT record_id, min_signatories FROM matters ORDER BY record_id"
            )
        )
        count_row = next(counts_iter, None)
        list_row = next(lists_iter, None)
        out: list[tuple[Any, ...]] = []
        for matter in matters_iter:
            record_id = matter["record_id"]
            verified = reach = 0
            while count_row is not None and count_row["record_id"] < record_id:
                count_row = next(counts_iter, None)
            if count_row is not None and count_row["record_id"] == record_id:
                verified = int(count_row["verified"])
                reach = int(count_row["reach"])
                count_row = next(counts_iter, None)
            counted: list[str] = []
            pending: list[str] = []
            while list_row is not None and list_row["record_id"] == record_id:
                if list_row["classification"] == "counted":
                    counted.append(list_row["signatory"])
                else:
                    pending.append(list_row["signatory"])
                list_row = next(lists_iter, None)
            while list_row is not None and list_row["record_id"] < record_id:
                list_row = next(lists_iter, None)
            needed = matter["min_signatories"]
            if verified >= needed:
                status = STATUS_READY
            elif reach >= needed:
                status = STATUS_UNDETERMINED
            else:
                status = STATUS_BLOCKED
            pending_only = sorted(set(pending) - set(counted))
            out.append(
                (
                    checkpoint.idx,
                    record_id,
                    status,
                    verified,
                    len(pending_only),
                    canonical_json(sorted(set(counted))),
                    canonical_json(pending_only),
                )
            )
            if len(out) >= 500:
                self._insert_results(out)
                out = []
        if out:
            self._insert_results(out)

    def _insert_results(self, rows: list[tuple[Any, ...]]) -> None:
        self.work.executemany(
            "INSERT OR REPLACE INTO matter_results(checkpoint_idx, record_id, status, "
            "verified_count, pending_count, verified_signatories_json, "
            "pending_signatories_json) VALUES(?,?,?,?,?,?,?)",
            rows,
        )

    def _decide_zones(self, checkpoint: Checkpoint) -> None:
        counts: dict[str, dict[str, int]] = {}
        for row in self.work.execute(
            "SELECT m.zone_id, r.status, COUNT(*) c FROM matter_results r "
            "JOIN matters m ON m.record_id=r.record_id "
            "WHERE r.checkpoint_idx=? GROUP BY m.zone_id, r.status",
            (checkpoint.idx,),
        ):
            counts.setdefault(row["zone_id"], {})[row["status"]] = int(row["c"])

        zones = self.work.execute("SELECT * FROM zones ORDER BY ordinal").fetchall()
        for zone in zones:
            zone_id = zone["zone_id"]
            status_counts = counts.get(zone_id, {})
            total = sum(status_counts.values())
            counts_json = canonical_json(
                {**{status: status_counts.get(status, 0) for status in MATTER_STATUSES}, "total": total}
            )
            reasons: list[dict[str, Any]]
            if zone["scope_status"] != SCOPE_CONFIRMED:
                issues = self.work.execute(
                    "SELECT issue_kind, detail_json FROM zone_scope_issues WHERE zone_id=? "
                    "ORDER BY id",
                    (zone_id,),
                ).fetchall()
                reasons = [
                    {"issue_kind": r["issue_kind"], "detail": json.loads(r["detail_json"])}
                    for r in issues[:_MAX_REASON_ISSUES]
                ]
                if len(issues) > _MAX_REASON_ISSUES:
                    reasons.append(
                        {"issue_kind": "truncated", "detail": f"{len(issues)} issues in total"}
                    )
                decision = DECISION_SCOPE_UNVERIFIED
            elif total == 0:
                decision = DECISION_EMPTY_SCOPE
                reasons = []
            elif status_counts.get(STATUS_READY, 0) == total:
                decision = DECISION_RELEASED
                reasons = []
            else:
                decision = DECISION_HELD
                reasons = []
                for row in self.work.execute(
                    "SELECT m.record_id, m.device_global_id, m.matter_code, r.status, "
                    "r.verified_signatories_json, r.pending_signatories_json, "
                    "m.min_signatories FROM matter_results r "
                    "JOIN matters m ON m.record_id=r.record_id "
                    "WHERE r.checkpoint_idx=? AND m.zone_id=? AND r.status != 'ready' "
                    "ORDER BY m.ordinal",
                    (checkpoint.idx, zone_id),
                ):
                    reasons.append(
                        {
                            "record_id": row["record_id"],
                            "device_global_id": row["device_global_id"],
                            "matter_code": row["matter_code"],
                            "status": row["status"],
                            "verified_signatories": json.loads(row["verified_signatories_json"]),
                            "pending_signatories": json.loads(row["pending_signatories_json"]),
                            "min_signatories": row["min_signatories"],
                        }
                    )
            self.work.execute(
                "INSERT OR REPLACE INTO zone_decisions(checkpoint_idx, zone_id, decision, "
                "reasons_json, counts_json) VALUES(?,?,?,?,?)",
                (checkpoint.idx, zone_id, decision, canonical_json(reasons), counts_json),
            )

    # ------------------------------------------------------------------
    # Anomalies: visible revisions with no candidate association
    # ------------------------------------------------------------------
    def _write_anomalies(self, checkpoints: list[Checkpoint]) -> None:
        if not self.rule_matter_codes:
            return
        placeholders = ",".join("?" for _ in self.rule_matter_codes)
        rows = self.work.execute(
            f"SELECT * FROM visible v WHERE v.matter_code IN ({placeholders}) "
            "AND NOT EXISTS (SELECT 1 FROM assoc a WHERE a.signoff_id = v.signoff_id) "
            "ORDER BY v.signoff_id",
            tuple(sorted(self.rule_matter_codes)),
        ).fetchall()
        for rev in rows:
            count = int(
                self.repo.execute(
                    "SELECT COUNT(*) c FROM roots WHERE source_id=? AND global_id=?",
                    (rev["target_source_id"], rev["target_global_id"]),
                ).fetchone()["c"]
            )
            if count == 0:
                reason = ANOMALY_INVALID_TARGET
                detail = "no repository object carries the target identity"
            elif count > 1:
                reason = ANOMALY_AMBIGUOUS_TARGET
                detail = (
                    f"target identity resolves to {count} repository objects, "
                    "none associated with a determined matter"
                )
            else:
                reason = ANOMALY_NO_ASSOCIATION
                detail = "target resolves but is not associated with any determined matter"
            for checkpoint in checkpoints:
                self.work.execute(
                    "INSERT INTO anomalies(checkpoint_idx, signoff_id, revision, line_no, "
                    "matter_code, target_kind, target_source_id, target_global_id, reason, "
                    "detail_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        checkpoint.idx,
                        rev["signoff_id"],
                        rev["revision"],
                        rev["line_no"],
                        rev["matter_code"],
                        rev["target_kind"],
                        rev["target_source_id"],
                        rev["target_global_id"],
                        reason,
                        canonical_json({"message": detail, "target_row_count": count}),
                    ),
                )
