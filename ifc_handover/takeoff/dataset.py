"""The independently re-openable spatial quantity take-off dataset.

One take-off run writes a destination directory containing::

    takeoff.sqlite   # every table below; the queryable take-off detail store
    takeoff.jsonl    # object detail lines and grouped summaries, one JSON/line
    README.md        # human-readable format summary

The directory is self-contained.  It binds the fingerprints of the fixed
object repository and the rules file, carries the parsed rules, the scope
anomalies with in-repository source coordinates, every object's quantity
detail with all declarations and STEP source coordinates, and the minimum
STEP evidence excerpts needed to re-check a line.  It never contains the
original repository or IFC files, and after being copied elsewhere it reopens
read-only without access to them.

Writes go to a hidden staging directory and are atomically renamed into place
only after the completion marker is committed, so a failed run can never
replace or mimic a complete previous dataset.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from typing import Any, Iterator

DATASET_SCHEMA_VERSION = 1

# Scope states.
SCOPE_CONFIRMED = "confirmed"
SCOPE_UNVERIFIED = "unverified"

# The six mutually exclusive line states.  They are deliberately distinct and
# a missing/invalid line is never turned into zero in a summary.
LINE_VALID = "valid"
LINE_MISSING = "missing"
LINE_CONFLICT = "conflict"
LINE_UNSUPPORTED = "unsupported"
LINE_UNIT_UNAVAILABLE = "unit_unavailable"
LINE_SCOPE_UNCERTAIN = "scope_uncertain"

# value_status values carried alongside the line state.
STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_CONFLICT = "conflict"
STATUS_UNSUPPORTED = "unsupported_type"
STATUS_MISSING_UNIT = "missing_unit"
STATUS_DIMENSION_MISMATCH = "dimension_mismatch"
STATUS_UNIT_CYCLE = "unit_cycle"
STATUS_UNRESOLVED_UNIT = "unresolved_unit"
STATUS_NON_FINITE = "non_finite_value"
STATUS_EXPLICIT_NULL = "explicit_null"
STATUS_SCOPE_UNCERTAIN = "scope_uncertain"

# Unit-source vocabulary.
UNIT_SOURCE_EXPLICIT = "explicit"
UNIT_SOURCE_PROJECT = "project"
UNIT_SOURCE_NONE = "none"

# Scope anomaly kinds.
ANOMALY_ROOT_UNRESOLVED = "root_unresolved"
ANOMALY_ROOT_AMBIGUOUS = "root_ambiguous"
ANOMALY_ROOT_NOT_SPATIAL = "root_not_spatial"
ANOMALY_ROOT_UNUSABLE = "root_unusable"
ANOMALY_EDGE_MISSING = "edge_missing_endpoint"
ANOMALY_EDGE_CONFLICT = "edge_conflict"
ANOMALY_OBJECT_IDENTITY_MISSING = "object_identity_missing"
ANOMALY_OBJECT_IDENTITY_CONFLICT = "object_identity_conflict"

# Membership kinds.
MEMBER_VALID = "valid"
MEMBER_EXCLUDED_IDENTITY = "excluded_identity"

DATASET_SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    object_count INTEGER
);

CREATE TABLE rules (
    rule_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL,
    root_source_id TEXT NOT NULL,
    root_global_id TEXT NOT NULL,
    root_key TEXT,
    entity_types_json TEXT NOT NULL,
    quantity_identities_json TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE rule_entities (
    rule_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    PRIMARY KEY (rule_id, entity_type)
);
CREATE INDEX rule_entities_type ON rule_entities(entity_type);

CREATE TABLE rule_quantity_identities (
    rule_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    qset_name TEXT NOT NULL,
    q_name TEXT NOT NULL,
    q_kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    dimension TEXT,
    canonical_unit TEXT,
    PRIMARY KEY (rule_id, entity_type, identity)
);
CREATE INDEX rqi_identity ON rule_quantity_identities(identity);

CREATE TABLE root_scopes (
    rule_id TEXT PRIMARY KEY,
    root_key TEXT,
    scope_status TEXT NOT NULL,
    included_objects INTEGER NOT NULL,
    anomaly_count INTEGER NOT NULL
);

CREATE TABLE scope_anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    anomaly_kind TEXT NOT NULL,
    source_id TEXT,
    object_key TEXT,
    global_id TEXT,
    step_id INTEGER,
    relation_key TEXT,
    detail_json TEXT NOT NULL
);
CREATE INDEX anomalies_rule ON scope_anomalies(rule_id);
CREATE INDEX anomalies_kind ON scope_anomalies(anomaly_kind);

CREATE TABLE scope_membership (
    rule_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    global_id TEXT,
    step_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    name TEXT,
    membership TEXT NOT NULL,
    depth INTEGER NOT NULL,
    PRIMARY KEY (rule_id, object_key)
);
CREATE INDEX membership_object ON scope_membership(object_key);
CREATE INDEX membership_rule ON scope_membership(rule_id);

CREATE TABLE object_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    root_source_id TEXT NOT NULL,
    root_global_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    global_id TEXT,
    step_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    name TEXT,
    type_key TEXT,
    qset_name TEXT NOT NULL,
    q_name TEXT NOT NULL,
    q_kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    line_state TEXT NOT NULL,
    value_status TEXT,
    scope_used TEXT,
    dimension TEXT,
    canonical_unit TEXT,
    canonical_num TEXT,
    canonical_den TEXT,
    canonical_decimal TEXT,
    canonical_decimal_exact INTEGER,
    original_raw TEXT,
    unit_source TEXT,
    reason TEXT,
    declarations_json TEXT NOT NULL,
    UNIQUE(rule_id, object_key, identity)
);
CREATE INDEX details_rule ON object_details(rule_id);
CREATE INDEX details_object ON object_details(object_key);
CREATE INDEX details_state ON object_details(line_state);
CREATE INDEX details_identity ON object_details(identity);
CREATE INDEX details_entity ON object_details(entity_type);

CREATE TABLE quantity_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    root_source_id TEXT NOT NULL,
    root_global_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    qset_name TEXT NOT NULL,
    q_name TEXT NOT NULL,
    q_kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    canonical_unit TEXT,
    included_objects INTEGER NOT NULL,
    valid_count INTEGER NOT NULL,
    missing_count INTEGER NOT NULL,
    conflict_count INTEGER NOT NULL,
    unsupported_count INTEGER NOT NULL,
    unit_unavailable_count INTEGER NOT NULL,
    scope_uncertain_count INTEGER NOT NULL,
    total_num TEXT,
    total_den TEXT,
    total_decimal TEXT,
    total_decimal_exact INTEGER,
    UNIQUE(rule_id, entity_type, identity)
);
CREATE INDEX summaries_rule ON quantity_summaries(rule_id);
CREATE INDEX summaries_identity ON quantity_summaries(identity);

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

-- Internal working tables, dropped before the dataset is published.
CREATE TABLE work_members (
    rule_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    depth INTEGER NOT NULL,
    PRIMARY KEY (rule_id, object_key)
);
CREATE INDEX work_members_rule ON work_members(rule_id);
"""

INTERNAL_TABLES = ("work_members",)

README_TEXT = """\
# IFC handover spatial quantity take-off dataset

This directory is the self-contained result of one offline spatial quantity
take-off run (`python3 -m ifc_handover takeoff-build`).  It reopens read-only
without the original object repository, the IFC files or any old path:

* `takeoff.sqlite` -- rule inputs, root scopes, scope anomalies with in-
  repository source coordinates, per-object quantity detail (every declaration
  and STEP source coordinate), grouped exact totals and the minimum STEP
  evidence excerpts.
* `takeoff.jsonl` -- one JSON object per object detail line and per summary.
* Input binding: the `manifest` meta entry records the SHA-256 fingerprint of
  the exact repository and the SHA-256 of the rules these numbers came from.

Totals are exact rational sums of declared quantities only; nothing is
computed from geometry or derived from dimensions, and a missing, conflicted,
unsupported or unit-unavailable line never contributes zero silently.

Query it with `python3 -m ifc_handover takeoff-report --dataset <dir>`,
`takeoff-query`, `takeoff-object` and `takeoff-export-jsonl`.
"""


class TakeoffDatasetError(RuntimeError):
    pass


def make_staging(parent: str) -> str:
    return tempfile.mkdtemp(prefix=".ifc-takeoff-", dir=parent)


def is_complete_dataset(path: str) -> bool:
    db_path = os.path.join(path, "takeoff.sqlite")
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
    """Atomically move the staging dataset into its final location."""

    dest = os.path.abspath(dest)
    parent = os.path.dirname(dest) or "."
    os.makedirs(parent, exist_ok=True)
    _fsync_tree(staging_dir)
    if os.path.exists(dest):
        if not is_complete_dataset(dest):
            raise TakeoffDatasetError(
                f"destination {dest} exists and is not a complete take-off dataset"
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


class TakeoffDataset:
    """Read-only view over a finished take-off dataset, independent of inputs."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        db_path = os.path.join(self.path, "takeoff.sqlite")
        if not is_complete_dataset(self.path):
            raise TakeoffDatasetError(f"not a complete take-off dataset: {self.path}")
        self.db_path = db_path
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TakeoffDataset":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- metadata -------------------------------------------------------
    def _meta(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def manifest(self) -> dict[str, Any]:
        return self._meta("manifest") or {}

    def rules_raw(self) -> Any:
        return self._meta("rules")

    # -- rules / scopes -------------------------------------------------
    def rules(self) -> list[dict[str, Any]]:
        out = []
        for row in self.conn.execute("SELECT * FROM rules ORDER BY ordinal"):
            data = dict(row)
            data["entity_types"] = json.loads(data.pop("entity_types_json"))
            data["quantity_identities"] = json.loads(data.pop("quantity_identities_json"))
            data["raw"] = json.loads(data.pop("raw_json"))
            out.append(data)
        return out

    def root_scopes(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM root_scopes ORDER BY rule_id")
        ]

    def anomalies(self, *, rule_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM scope_anomalies"
        params: list[Any] = []
        if rule_id is not None:
            sql += " WHERE rule_id=?"
            params.append(rule_id)
        sql += " ORDER BY id"
        out = []
        for row in self.conn.execute(sql, params):
            data = dict(row)
            data["detail"] = json.loads(data.pop("detail_json"))
            out.append(data)
        return out

    # -- details --------------------------------------------------------
    def details(
        self,
        *,
        rule_id: str | None = None,
        root_global_id: str | None = None,
        entity_type: str | None = None,
        identity: str | None = None,
        line_state: str | None = None,
        global_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if rule_id is not None:
            clauses.append("rule_id=?")
            params.append(rule_id)
        if root_global_id is not None:
            clauses.append("root_global_id=?")
            params.append(root_global_id)
        if entity_type is not None:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if identity is not None:
            clauses.append("identity=?")
            params.append(identity)
        if line_state is not None:
            clauses.append("line_state=?")
            params.append(line_state)
        if global_id is not None:
            clauses.append("global_id=?")
            params.append(global_id)
        sql = "SELECT * FROM object_details"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rule_id, entity_type, source_id, step_id, identity"
        out = []
        for row in self.conn.execute(sql, params):
            data = dict(row)
            data["canonical_decimal_exact"] = bool(data["canonical_decimal_exact"])
            data["declarations"] = json.loads(data.pop("declarations_json"))
            out.append(data)
        return out

    def object_detail_by_key(self, object_key: str) -> list[dict[str, Any]]:
        return self._object_detail_rows("object_key=?", (object_key,))

    def object_detail_by_identity(self, source_id: str, global_id: str) -> list[dict[str, Any]]:
        return self._object_detail_rows(
            "source_id=? AND global_id=?", (source_id, global_id)
        )

    def _object_detail_rows(self, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        out = []
        for row in self.conn.execute(
            f"SELECT * FROM object_details WHERE {where} ORDER BY rule_id, identity",
            params,
        ):
            data = dict(row)
            data["canonical_decimal_exact"] = bool(data["canonical_decimal_exact"])
            data["declarations"] = json.loads(data.pop("declarations_json"))
            out.append(data)
        return out

    # -- summaries ------------------------------------------------------
    def summaries(
        self,
        *,
        rule_id: str | None = None,
        entity_type: str | None = None,
        identity: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if rule_id is not None:
            clauses.append("rule_id=?")
            params.append(rule_id)
        if entity_type is not None:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if identity is not None:
            clauses.append("identity=?")
            params.append(identity)
        sql = "SELECT * FROM quantity_summaries"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rule_id, entity_type, identity"
        out = []
        for row in self.conn.execute(sql, params):
            data = dict(row)
            data["total_decimal_exact"] = bool(data["total_decimal_exact"])
            out.append(data)
        return out

    def evidence(
        self, *, owner_kind: str | None = None, owner_id: str | None = None
    ) -> list[dict[str, Any]]:
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

    def sources(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM sources ORDER BY source_id")
        ]

    def summary(self) -> dict[str, Any]:
        line_states = {}
        for row in self.conn.execute(
            "SELECT line_state, COUNT(*) c FROM object_details GROUP BY line_state"
        ):
            line_states[row["line_state"]] = int(row["c"])
        scopes = {}
        for row in self.conn.execute(
            "SELECT scope_status, COUNT(*) c FROM root_scopes GROUP BY scope_status"
        ):
            scopes[row["scope_status"]] = int(row["c"])
        empty = int(
            self.conn.execute(
                "SELECT COUNT(*) c FROM root_scopes WHERE included_objects=0"
            ).fetchone()["c"]
        )
        return {
            "rules": int(self.conn.execute("SELECT COUNT(*) c FROM rules").fetchone()["c"]),
            "root_scopes": scopes,
            "empty_scopes": empty,
            "detail_lines": int(
                self.conn.execute("SELECT COUNT(*) c FROM object_details").fetchone()["c"]
            ),
            "summaries": int(
                self.conn.execute("SELECT COUNT(*) c FROM quantity_summaries").fetchone()["c"]
            ),
            "anomalies": int(
                self.conn.execute("SELECT COUNT(*) c FROM scope_anomalies").fetchone()["c"]
            ),
            "line_states": line_states,
        }

    def iter_jsonl(self) -> Iterator[dict[str, Any]]:
        with open(os.path.join(self.path, "takeoff.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
