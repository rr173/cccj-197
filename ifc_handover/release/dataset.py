"""The independently re-openable release verification dataset.

One verification run writes a destination directory containing::

    release.sqlite   # every table below; the queryable release detail store
    release.jsonl    # zone decisions and matter results, one JSON object per line
    README.md        # human-readable format summary

The directory is self-contained: it records the fingerprints of the fixed
inputs (object repository, rules, ledger), the parsed rules, the ledger
revisions that were visible at any checkpoint, the file-hash check results and
the minimum STEP evidence excerpts needed to trace each decision.  It never
contains the original repository or the signoff attachments, and after being
copied elsewhere it reopens read-only without access to them.

Writes go to a hidden staging directory and are atomically renamed into place
only after the completion marker is committed, so a failed run can never
replace or mimic a complete previous report.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from typing import Any, Iterator

DATASET_SCHEMA_VERSION = 1

# Zone scope states.
SCOPE_CONFIRMED = "confirmed"
SCOPE_UNVERIFIED = "unverified"

# Matter statuses.
STATUS_READY = "ready"
STATUS_UNDETERMINED = "undetermined"
STATUS_BLOCKED = "blocked"
MATTER_STATUSES = (STATUS_READY, STATUS_UNDETERMINED, STATUS_BLOCKED)

# Zone decisions.
DECISION_RELEASED = "released"
DECISION_HELD = "held"
DECISION_EMPTY_SCOPE = "empty_scope"
DECISION_SCOPE_UNVERIFIED = "scope_unverified"

# Signoff classifications.
CLASS_COUNTED = "counted"
CLASS_PENDING = "pending"
CLASS_EXCLUDED = "excluded"

# Excluded reasons (rejection reasons, queryable per checkpoint).
REASON_REJECTED = "rejected"
REASON_WITHDRAWN = "withdrawn"
REASON_OUT_OF_VALIDITY = "out_of_validity"
REASON_INVALID_TARGET = "invalid_target"
REASON_OUT_OF_COVERAGE = "out_of_coverage"

# Pending reasons.
REASON_FILE_MISSING = "file_missing"
REASON_FILE_HASH_MISMATCH = "file_hash_mismatch"
REASON_COVERAGE_UNVERIFIED = "coverage_unverified"

# Anomaly reasons (no concrete candidate association with any matter).
ANOMALY_INVALID_TARGET = "invalid_target"
ANOMALY_AMBIGUOUS_TARGET = "ambiguous_target"
ANOMALY_NO_ASSOCIATION = "no_candidate_association"

DATASET_SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE checkpoints (
    idx INTEGER PRIMARY KEY,
    checkpoint_id TEXT NOT NULL UNIQUE,
    effective_at TEXT NOT NULL,
    known_at TEXT NOT NULL
);

CREATE TABLE zones (
    zone_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL,
    root_source_id TEXT NOT NULL,
    root_global_id TEXT NOT NULL,
    root_key TEXT,
    scope_status TEXT NOT NULL,
    device_count INTEGER NOT NULL,
    matter_count INTEGER NOT NULL
);

CREATE TABLE zone_scope_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL,
    issue_kind TEXT NOT NULL,
    object_key TEXT,
    relation_key TEXT,
    detail_json TEXT NOT NULL
);
CREATE INDEX scope_issues_zone ON zone_scope_issues(zone_id);

CREATE TABLE matters (
    record_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL,
    zone_id TEXT NOT NULL,
    device_key TEXT NOT NULL,
    device_source_id TEXT NOT NULL,
    device_global_id TEXT NOT NULL,
    device_step_id INTEGER NOT NULL,
    device_entity_type TEXT NOT NULL,
    device_name TEXT,
    matter_code TEXT NOT NULL,
    coverage_mode TEXT NOT NULL,
    min_signatories INTEGER NOT NULL
);
CREATE INDEX matters_zone ON matters(zone_id);
CREATE INDEX matters_device_gid ON matters(device_global_id);

CREATE TABLE matter_results (
    checkpoint_idx INTEGER NOT NULL,
    record_id TEXT NOT NULL,
    status TEXT NOT NULL,
    verified_count INTEGER NOT NULL,
    pending_count INTEGER NOT NULL,
    verified_signatories_json TEXT NOT NULL,
    pending_signatories_json TEXT NOT NULL,
    PRIMARY KEY (checkpoint_idx, record_id)
);
CREATE INDEX results_record ON matter_results(record_id);
CREATE INDEX results_status ON matter_results(status);

CREATE TABLE matter_signoffs (
    checkpoint_idx INTEGER NOT NULL,
    record_id TEXT NOT NULL,
    signoff_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    signatory TEXT NOT NULL,
    decision TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    matter_code TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_source_id TEXT NOT NULL,
    target_global_id TEXT NOT NULL,
    classification TEXT NOT NULL,
    reason TEXT,
    reason_detail TEXT,
    doc_path TEXT,
    doc_declared_sha256 TEXT,
    doc_computed_sha256 TEXT,
    doc_status TEXT,
    coverage_path_json TEXT,
    PRIMARY KEY (checkpoint_idx, record_id, signoff_id)
);
CREATE INDEX signoffs_record ON matter_signoffs(record_id);
CREATE INDEX signoffs_id ON matter_signoffs(signoff_id);
CREATE INDEX signoffs_class ON matter_signoffs(classification, reason);

CREATE TABLE zone_decisions (
    checkpoint_idx INTEGER NOT NULL,
    zone_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    PRIMARY KEY (checkpoint_idx, zone_id)
);

CREATE TABLE anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_idx INTEGER NOT NULL,
    signoff_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    matter_code TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_source_id TEXT NOT NULL,
    target_global_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX anomalies_cp ON anomalies(checkpoint_idx);

CREATE TABLE file_checks (
    path TEXT NOT NULL,
    declared_sha256 TEXT NOT NULL,
    computed_sha256 TEXT,
    status TEXT NOT NULL,
    PRIMARY KEY (path, declared_sha256)
);

CREATE TABLE evidence (
    owner_kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    entity_type TEXT,
    role TEXT,
    line_start INTEGER,
    line_end INTEGER,
    raw_text TEXT,
    PRIMARY KEY (owner_kind, owner_id, source_id, step_id, role)
);
CREATE INDEX evidence_owner ON evidence(owner_kind, owner_id);

CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    object_count INTEGER
);

CREATE TABLE ledger_revisions (
    signoff_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    decision TEXT NOT NULL,
    signatory TEXT NOT NULL,
    matter_code TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_source_id TEXT NOT NULL,
    target_global_id TEXT NOT NULL,
    doc_path TEXT,
    doc_sha256 TEXT,
    line_no INTEGER NOT NULL,
    PRIMARY KEY (signoff_id, revision)
);

-- Internal working tables, dropped before the dataset is published.
CREATE TABLE zone_members (
    zone_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    PRIMARY KEY (zone_id, object_key)
);

CREATE TABLE zone_devices (
    zone_id TEXT NOT NULL,
    device_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    global_id TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    name TEXT,
    PRIMARY KEY (zone_id, device_key)
);

CREATE TABLE record_targets (
    record_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    assoc_kind TEXT NOT NULL,
    path_json TEXT,
    PRIMARY KEY (record_id, object_key)
);
CREATE INDEX record_targets_key ON record_targets(object_key);

CREATE TABLE visible (
    signoff_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    decision TEXT NOT NULL,
    signatory TEXT NOT NULL,
    matter_code TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_source_id TEXT NOT NULL,
    target_global_id TEXT NOT NULL,
    doc_path TEXT,
    doc_sha256 TEXT,
    line_no INTEGER NOT NULL
);

CREATE TABLE assoc (
    record_id TEXT NOT NULL,
    signoff_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    assoc_kind TEXT NOT NULL,
    path_json TEXT,
    target_key TEXT NOT NULL,
    target_step_id INTEGER,
    target_entity_type TEXT,
    target_is_spatial INTEGER,
    target_row_count INTEGER NOT NULL,
    PRIMARY KEY (record_id, signoff_id, target_key)
);
CREATE INDEX assoc_signoff ON assoc(signoff_id);
"""

INTERNAL_TABLES = (
    "assoc",
    "visible",
    "record_targets",
    "zone_devices",
    "zone_members",
)

README_TEXT = """\
# IFC handover release verification dataset

This directory is the self-contained result of one offline release
verification run (`python3 -m ifc_handover release-verify`).  It reopens
read-only without the original object repository, rules file, signoff ledger
or signoff attachments:

* `release.sqlite` -- zone scope results, determined matters, per-checkpoint
  matter results and zone decisions, every classified signoff revision with
  its ledger source line, the anomaly list, file-hash check results, the
  repository source summary and STEP evidence excerpts.
* `release.jsonl` -- one JSON object per zone decision and per matter result.
* Input binding: the `manifest` meta entry records the SHA-256/fingerprints of
  the exact repository, rules, ledger and checkpoint inputs these decisions
  were computed from, plus the timestamp precision rule.

Query it with `python3 -m ifc_handover release-report --dataset <dir>` and
`python3 -m ifc_handover release-query --dataset <dir> ...`.
"""


class DatasetError(RuntimeError):
    pass


def make_staging(parent: str) -> str:
    return tempfile.mkdtemp(prefix=".ifc-release-", dir=parent)


def is_complete_dataset(path: str) -> bool:
    db_path = os.path.join(path, "release.sqlite")
    if not os.path.isdir(path) or not os.path.isfile(db_path):
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='complete'").fetchone()
            return row is not None and json.loads(row[0]).get("complete") is True
        finally:
            conn.close()
    except (sqlite3.DatabaseError, json.JSONDecodeError):
        return False


def publish_dataset(staging_dir: str, dest: str) -> None:
    """Atomically move the staging dataset into its final location.

    An existing *complete* dataset at ``dest`` is replaced only after the new
    one is fully written and fsynced; anything else at ``dest`` is refused so
    a failed or foreign directory is never clobbered.
    """

    dest = os.path.abspath(dest)
    parent = os.path.dirname(dest) or "."
    os.makedirs(parent, exist_ok=True)
    _fsync_tree(staging_dir)
    if os.path.exists(dest):
        if not is_complete_dataset(dest):
            raise DatasetError(
                f"destination {dest} exists and is not a complete release dataset"
            )
        old = dest + ".old"
        os.replace(dest, old)
        try:
            os.replace(staging_dir, dest)
        except BaseException:
            os.replace(old, dest)
            raise
        shutil.rmtree(old, ignore_errors=True)
    else:
        os.replace(staging_dir, dest)


def _fsync_tree(root: str) -> None:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                os.fsync(fh.fileno())
        try:
            fd = os.open(dirpath, os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        except OSError:
            pass


class ReleaseDataset:
    """Read-only view over a finished release dataset, independent of inputs."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        db_path = os.path.join(self.path, "release.sqlite")
        if not is_complete_dataset(self.path):
            raise DatasetError(f"not a complete release dataset: {self.path}")
        self.db_path = db_path
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ReleaseDataset":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- metadata -------------------------------------------------------
    def _meta(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else json.loads(row["value"])

    def manifest(self) -> dict[str, Any]:
        return self._meta("manifest") or {}

    def rules(self) -> dict[str, Any]:
        return self._meta("rules") or {}

    def checkpoints(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM checkpoints ORDER BY idx"
        ).fetchall()
        return [dict(r) for r in rows]

    def checkpoint_idx(self, checkpoint: str) -> int | None:
        row = self.conn.execute(
            "SELECT idx FROM checkpoints WHERE checkpoint_id=?", (checkpoint,)
        ).fetchone()
        return None if row is None else int(row["idx"])

    def sources(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM sources ORDER BY source_id")]

    # -- zones ----------------------------------------------------------
    def zones(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM zones ORDER BY ordinal")]

    def scope_issues(self, zone: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM zone_scope_issues"
        params: list[Any] = []
        if zone:
            sql += " WHERE zone_id=?"
            params.append(zone)
        sql += " ORDER BY id"
        out = []
        for row in self.conn.execute(sql, params):
            data = dict(row)
            data["detail"] = json.loads(data.pop("detail_json"))
            out.append(data)
        return out

    def zone_decisions(
        self, *, checkpoint: str | None = None, zone: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if checkpoint is not None:
            idx = self.checkpoint_idx(checkpoint)
            if idx is None:
                return []
            clauses.append("checkpoint_idx=?")
            params.append(idx)
        if zone is not None:
            clauses.append("zone_id=?")
            params.append(zone)
        sql = "SELECT * FROM zone_decisions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY checkpoint_idx, zone_id"
        out = []
        for row in self.conn.execute(sql, params):
            data = dict(row)
            data["reasons"] = json.loads(data.pop("reasons_json"))
            data["counts"] = json.loads(data.pop("counts_json"))
            out.append(data)
        return out

    # -- matters --------------------------------------------------------
    def matters(
        self,
        *,
        checkpoint: str | None = None,
        zone: str | None = None,
        device_global_id: str | None = None,
        status: str | None = None,
        matter_code: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        select = "SELECT m.*"
        join = ""
        if checkpoint is not None:
            idx = self.checkpoint_idx(checkpoint)
            if idx is None:
                return []
            select += ", r.status, r.verified_count, r.pending_count, r.verified_signatories_json, r.pending_signatories_json"
            join = "JOIN matter_results r ON r.record_id=m.record_id AND r.checkpoint_idx=?"
            params.append(idx)
        if zone is not None:
            clauses.append("m.zone_id=?")
            params.append(zone)
        if device_global_id is not None:
            clauses.append("m.device_global_id=?")
            params.append(device_global_id)
        if matter_code is not None:
            clauses.append("m.matter_code=?")
            params.append(matter_code)
        if status is not None:
            if checkpoint is None:
                raise DatasetError("status filter requires a checkpoint")
            clauses.append("r.status=?")
            params.append(status)
        sql = select + " FROM matters m " + join
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY m.ordinal"
        out = []
        for row in self.conn.execute(sql, params):
            data = dict(row)
            if checkpoint is not None:
                data["verified_signatories"] = json.loads(data.pop("verified_signatories_json"))
                data["pending_signatories"] = json.loads(data.pop("pending_signatories_json"))
            out.append(data)
        return out

    def signoffs(
        self,
        *,
        record_id: str | None = None,
        checkpoint: str | None = None,
        signoff_id: str | None = None,
        classification: str | None = None,
        zone: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        join = ""
        if record_id is not None:
            clauses.append("s.record_id=?")
            params.append(record_id)
        if checkpoint is not None:
            idx = self.checkpoint_idx(checkpoint)
            if idx is None:
                return []
            clauses.append("s.checkpoint_idx=?")
            params.append(idx)
        if signoff_id is not None:
            clauses.append("s.signoff_id=?")
            params.append(signoff_id)
        if classification is not None:
            clauses.append("s.classification=?")
            params.append(classification)
        if zone is not None:
            join = "JOIN matters m ON m.record_id=s.record_id"
            clauses.append("m.zone_id=?")
            params.append(zone)
        sql = "SELECT s.* FROM matter_signoffs s " + join
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY s.checkpoint_idx, s.record_id, s.signoff_id"
        out = []
        for row in self.conn.execute(sql, params):
            data = dict(row)
            if data.get("coverage_path_json") is not None:
                data["coverage_path"] = json.loads(data.pop("coverage_path_json"))
            else:
                data.pop("coverage_path_json")
                data["coverage_path"] = None
            out.append(data)
        return out

    def anomalies(self, *, checkpoint: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if checkpoint is not None:
            idx = self.checkpoint_idx(checkpoint)
            if idx is None:
                return []
            clauses.append("checkpoint_idx=?")
            params.append(idx)
        sql = "SELECT * FROM anomalies"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        out = []
        for row in self.conn.execute(sql, params):
            data = dict(row)
            data["detail"] = json.loads(data.pop("detail_json"))
            out.append(data)
        return out

    def file_checks(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM file_checks ORDER BY path, declared_sha256")
        ]

    def evidence(self, *, owner_kind: str | None = None, owner_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if owner_kind is not None:
            clauses.append("owner_kind=?")
            params.append(owner_kind)
        if owner_id is not None:
            clauses.append("owner_id=?")
            params.append(owner_id)
        sql = "SELECT * FROM evidence"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY owner_kind, owner_id, source_id, step_id, role"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def revisions(self, *, signoff_id: str | None = None) -> list[dict[str, Any]]:
        if signoff_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM ledger_revisions WHERE signoff_id=? ORDER BY revision",
                (signoff_id,),
            )
        else:
            rows = self.conn.execute(
                "SELECT * FROM ledger_revisions ORDER BY signoff_id, revision"
            )
        return [dict(r) for r in rows]

    def summary(self) -> dict[str, Any]:
        decisions = {}
        for row in self.conn.execute(
            "SELECT checkpoint_idx, decision, COUNT(*) c FROM zone_decisions "
            "GROUP BY checkpoint_idx, decision ORDER BY checkpoint_idx, decision"
        ):
            decisions.setdefault(int(row["checkpoint_idx"]), {})[row["decision"]] = int(row["c"])
        statuses = {}
        for row in self.conn.execute(
            "SELECT checkpoint_idx, status, COUNT(*) c FROM matter_results "
            "GROUP BY checkpoint_idx, status ORDER BY checkpoint_idx, status"
        ):
            statuses.setdefault(int(row["checkpoint_idx"]), {})[row["status"]] = int(row["c"])
        checkpoints = {cp["idx"]: cp["checkpoint_id"] for cp in self.checkpoints()}
        return {
            "zones": int(self.conn.execute("SELECT COUNT(*) c FROM zones").fetchone()["c"]),
            "matters": int(self.conn.execute("SELECT COUNT(*) c FROM matters").fetchone()["c"]),
            "signoff_rows": int(
                self.conn.execute("SELECT COUNT(*) c FROM matter_signoffs").fetchone()["c"]
            ),
            "anomalies": int(self.conn.execute("SELECT COUNT(*) c FROM anomalies").fetchone()["c"]),
            "zone_decisions": {
                checkpoints[idx]: counts for idx, counts in decisions.items()
            },
            "matter_statuses": {
                checkpoints[idx]: counts for idx, counts in statuses.items()
            },
        }

    def iter_jsonl(self) -> Iterator[dict[str, Any]]:
        with open(os.path.join(self.path, "release.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
