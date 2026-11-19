"""The independently readable change dataset.

A diff writes a new SQLite database plus a JSONL export.  It never opens or
modifies either input repository.  Writes go to a hidden staging directory
inside the destination's parent and are atomically renamed into place only
after every row and the completion marker have been fsynced, so a failed run
can never leave a directory that looks like a complete change dataset.

The schema is deliberately self-describing: every change row carries both
sides' ``source_id``/SHA-256, the matched object identity, the change category,
before/after values or states, relation/material locators, declaration
provenance and the minimum ``source_entities`` evidence needed to trace each
side back to its originating STEP line.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from ..keys import canonical_json

DATASET_SCHEMA_VERSION = 1

# Result categories.  These are distinct outcomes and must never be collapsed:
# an uncertain/incomparable identity is not reported as remove+add.
CAT_ADDED = "added"
CAT_REMOVED = "removed"
CAT_SEMANTIC_CHANGE = "semantic_change"
CAT_PROVENANCE_CHANGE = "provenance_change"
CAT_INCOMPARABLE = "incomparable"

CHANGE_CATEGORIES = frozenset(
    {
        CAT_ADDED,
        CAT_REMOVED,
        CAT_SEMANTIC_CHANGE,
        CAT_PROVENANCE_CHANGE,
        CAT_INCOMPARABLE,
    }
)

# Semantic change aspects, used for stable per-aspect ordering and filtering.
ASPECT_ENTITY_TYPE = "entity_type"
ASPECT_PROPERTY = "property"
ASPECT_SPATIAL = "spatial_relation"
ASPECT_TYPE_ASSIGNMENT = "type_assignment"
ASPECT_MATERIAL = "material"

DATASET_SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE side_sources (
    side TEXT NOT NULL,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (side, source_id)
);

CREATE TABLE identity_mappings (
    ordinal INTEGER PRIMARY KEY,
    left_source_id TEXT NOT NULL,
    left_global_id TEXT NOT NULL,
    right_source_id TEXT NOT NULL,
    right_global_id TEXT NOT NULL,
    rationale TEXT NOT NULL,
    mapping_json TEXT NOT NULL
);

CREATE TABLE changes (
    change_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL,
    category TEXT NOT NULL,
    aspect TEXT NOT NULL,
    left_source_id TEXT,
    left_sha256 TEXT,
    right_source_id TEXT,
    right_sha256 TEXT,
    identity_kind TEXT NOT NULL,
    left_global_id TEXT,
    right_global_id TEXT,
    mapping_ordinal INTEGER,
    mapping_rationale TEXT,
    subject TEXT NOT NULL,
    locator TEXT,
    before_json TEXT,
    after_json TEXT,
    before_provenance_json TEXT,
    after_provenance_json TEXT,
    reason TEXT
);
CREATE INDEX changes_category ON changes(category);
CREATE INDEX changes_aspect ON changes(aspect);
CREATE INDEX changes_identity ON changes(identity_kind, left_global_id, right_global_id);
CREATE INDEX changes_left_gid ON changes(left_global_id);
CREATE INDEX changes_right_gid ON changes(right_global_id);
CREATE INDEX changes_subject ON changes(subject);

CREATE TABLE evidence (
    change_id TEXT NOT NULL,
    side TEXT NOT NULL,
    source_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    entity_type TEXT,
    role TEXT,
    line_start INTEGER,
    line_end INTEGER,
    raw_text TEXT,
    PRIMARY KEY (change_id, side, source_id, step_id, role)
);
CREATE INDEX evidence_change ON evidence(change_id);

CREATE TABLE object_summary (
    pair_key TEXT PRIMARY KEY,
    identity_kind TEXT NOT NULL,
    left_source_id TEXT,
    left_global_id TEXT,
    right_source_id TEXT,
    right_global_id TEXT,
    categories_json TEXT NOT NULL
);
"""


class DatasetError(RuntimeError):
    pass


@dataclass
class ChangeRecord:
    category: str
    aspect: str
    subject: str
    identity_kind: str
    left_source_id: str | None = None
    left_sha256: str | None = None
    right_source_id: str | None = None
    right_sha256: str | None = None
    left_global_id: str | None = None
    right_global_id: str | None = None
    mapping_ordinal: int | None = None
    mapping_rationale: str | None = None
    locator: dict[str, Any] | None = None
    before: Any = None
    after: Any = None
    before_provenance: Any = None
    after_provenance: Any = None
    reason: str | None = None
    evidence: list[dict[str, Any]] | None = None

    def stable_hash_input(self) -> str:
        payload = {
            "category": self.category,
            "aspect": self.aspect,
            "subject": self.subject,
            "identity_kind": self.identity_kind,
            "left_source_id": self.left_source_id,
            "left_sha256": self.left_sha256,
            "right_source_id": self.right_source_id,
            "right_sha256": self.right_sha256,
            "left_global_id": self.left_global_id,
            "right_global_id": self.right_global_id,
            "mapping_ordinal": self.mapping_ordinal,
            "locator": self.locator,
            "before": self.before,
            "after": self.after,
            "before_provenance": self.before_provenance,
            "after_provenance": self.after_provenance,
            "reason": self.reason,
            "evidence": sorted(
                canonical_json(_evidence_key(item))
                for item in (self.evidence or [])
            ),
        }
        return canonical_json(payload)


def _evidence_key(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "side": item.get("side"),
        "source_id": item.get("source_id"),
        "step_id": item.get("step_id"),
        "role": item.get("role"),
    }


class ChangeDatasetWriter:
    """Streams change rows into a staging database with bounded memory."""

    def __init__(self, staging_dir: str):
        self.staging_dir = staging_dir
        self.db_path = os.path.join(staging_dir, "changes.sqlite")
        self.jsonl_path = os.path.join(staging_dir, "changes.jsonl")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DATASET_SCHEMA_SQL)
        self.conn.execute("BEGIN")
        self._jsonl = open(self.jsonl_path, "w", encoding="utf-8", newline="\n")
        self._buffer: list[ChangeRecord] = []
        self._ordinal = 0
        self._pair_categories: dict[str, dict[str, Any]] = {}
        self._closed = False

    # -- metadata -------------------------------------------------------
    def write_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            (key, value if isinstance(value, str) else canonical_json(value)),
        )

    def write_side_sources(self, side: str, sources: Sequence[Any]) -> None:
        for ref in sources:
            self.conn.execute(
                "INSERT INTO side_sources(side, source_id, sha256, status) "
                "VALUES(?, ?, ?, ?)",
                (side, ref.source_id, ref.sha256, ref.status),
            )

    def write_mappings(self, mappings: Sequence[dict[str, Any]]) -> None:
        for ordinal, row in enumerate(mappings):
            self.conn.execute(
                "INSERT INTO identity_mappings(ordinal, left_source_id, left_global_id, "
                "right_source_id, right_global_id, rationale, mapping_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    ordinal,
                    row["left"]["source_id"],
                    row["left"]["global_id"],
                    row["right"]["source_id"],
                    row["right"]["global_id"],
                    row["rationale"],
                    canonical_json(row),
                ),
            )

    # -- rows -----------------------------------------------------------
    def add(self, record: ChangeRecord) -> None:
        self._buffer.append(record)
        if len(self._buffer) >= 100:
            self._flush()

    def note_pair(
        self,
        pair_key: str,
        *,
        identity_kind: str,
        left_source_id: str | None,
        left_global_id: str | None,
        right_source_id: str | None,
        right_global_id: str | None,
        category: str,
    ) -> None:
        entry = self._pair_categories.setdefault(
            pair_key,
            {
                "identity_kind": identity_kind,
                "left_source_id": left_source_id,
                "left_global_id": left_global_id,
                "right_source_id": right_source_id,
                "right_global_id": right_global_id,
                "categories": set(),
            },
        )
        entry["categories"].add(category)

    def _flush(self) -> None:
        if not self._buffer:
            return
        for record in self._buffer:
            self._write_one(record)
        self._buffer.clear()

    def _write_one(self, record: ChangeRecord) -> None:
        import hashlib

        change_id = hashlib.sha256(
            record.stable_hash_input().encode("utf-8")
        ).hexdigest()
        ordinal = self._ordinal
        self._ordinal += 1
        self.conn.execute(
            "INSERT INTO changes(change_id, ordinal, category, aspect, left_source_id, "
            "left_sha256, right_source_id, right_sha256, identity_kind, left_global_id, "
            "right_global_id, mapping_ordinal, mapping_rationale, subject, locator, "
            "before_json, after_json, before_provenance_json, after_provenance_json, reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                change_id,
                ordinal,
                record.category,
                record.aspect,
                record.left_source_id,
                record.left_sha256,
                record.right_source_id,
                record.right_sha256,
                record.identity_kind,
                record.left_global_id,
                record.right_global_id,
                record.mapping_ordinal,
                record.mapping_rationale,
                record.subject,
                canonical_json(record.locator) if record.locator is not None else None,
                canonical_json(record.before) if record.before is not None else None,
                canonical_json(record.after) if record.after is not None else None,
                canonical_json(record.before_provenance)
                if record.before_provenance is not None
                else None,
                canonical_json(record.after_provenance)
                if record.after_provenance is not None
                else None,
                record.reason,
            ),
        )
        for item in record.evidence or []:
            self.conn.execute(
                "INSERT OR REPLACE INTO evidence(change_id, side, source_id, sha256, "
                "step_id, entity_type, role, line_start, line_end, raw_text) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    change_id,
                    item["side"],
                    item["source_id"],
                    item["sha256"],
                    item["step_id"],
                    item.get("entity_type"),
                    item.get("role"),
                    item.get("line_start"),
                    item.get("line_end"),
                    item.get("raw_text"),
                ),
            )
        self._jsonl.write(
            canonical_json(self._jsonl_payload(change_id, ordinal, record)) + "\n"
        )

    def _jsonl_payload(self, change_id: str, ordinal: int, record: ChangeRecord) -> dict[str, Any]:
        return {
            "change_id": change_id,
            "ordinal": ordinal,
            "category": record.category,
            "aspect": record.aspect,
            "left": _side_ref(record.left_source_id, record.left_sha256),
            "right": _side_ref(record.right_source_id, record.right_sha256),
            "identity": {
                "kind": record.identity_kind,
                "left_global_id": record.left_global_id,
                "right_global_id": record.right_global_id,
                "mapping": None
                if record.mapping_ordinal is None
                else {
                    "ordinal": record.mapping_ordinal,
                    "rationale": record.mapping_rationale,
                },
            },
            "subject": record.subject,
            "locator": record.locator,
            "before": record.before,
            "after": record.after,
            "before_provenance": record.before_provenance,
            "after_provenance": record.after_provenance,
            "reason": record.reason,
            "evidence": record.evidence or [],
        }

    # -- finalisation ---------------------------------------------------
    def finish(self, *, manifest: dict[str, Any]) -> None:
        self._flush()
        for pair_key, entry in sorted(self._pair_categories.items()):
            self.conn.execute(
                "INSERT INTO object_summary(pair_key, identity_kind, left_source_id, "
                "left_global_id, right_source_id, right_global_id, categories_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    pair_key,
                    entry["identity_kind"],
                    entry["left_source_id"],
                    entry["left_global_id"],
                    entry["right_source_id"],
                    entry["right_global_id"],
                    canonical_json(sorted(entry["categories"])),
                ),
            )
        counts = self.category_counts()
        manifest = {**manifest, "category_counts": counts, "change_count": self._ordinal}
        self.write_meta("manifest", manifest)
        self.write_meta("complete", {"complete": True})
        self.conn.commit()
        self.conn.close()
        self._jsonl.flush()
        os.fsync(self._jsonl.fileno())
        self._jsonl.close()
        self._closed = True

    def category_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT category, COUNT(*) c FROM changes GROUP BY category"
        )
        return {row["category"]: int(row["c"]) for row in rows}

    def abort(self) -> None:
        if self._closed:
            return
        try:
            self.conn.rollback()
            self.conn.close()
        except sqlite3.Error:
            pass
        try:
            if not self._jsonl.closed:
                self._jsonl.close()
        except OSError:
            pass


def _side_ref(source_id: str | None, sha256: str | None) -> dict[str, Any] | None:
    if source_id is None and sha256 is None:
        return None
    return {"source_id": source_id, "sha256": sha256}


def publish_dataset(staging_dir: str, dest: str) -> None:
    """Atomically move the staging dataset into its final location."""

    dest = os.path.abspath(dest)
    parent = os.path.dirname(dest) or "."
    os.makedirs(parent, exist_ok=True)
    _fsync_tree(staging_dir)
    if os.path.exists(dest):
        if not is_complete_dataset(dest):
            raise DatasetError(
                f"destination {dest} exists and is not a complete change dataset"
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


def is_complete_dataset(path: str) -> bool:
    db_path = os.path.join(path, "changes.sqlite")
    if not os.path.isdir(path) or not os.path.isfile(db_path):
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='complete'"
            ).fetchone()
            return row is not None and json.loads(row[0]).get("complete") is True
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


class ChangeDataset:
    """Read-only view over a finished change dataset, independent of inputs."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        db_path = os.path.join(self.path, "changes.sqlite")
        if not is_complete_dataset(self.path):
            raise DatasetError(
                f"not a complete change dataset: {self.path}"
            )
        self.db_path = db_path
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ChangeDataset":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def manifest(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
        return {} if row is None else json.loads(row[0])

    def query(
        self,
        *,
        category: str | None = None,
        aspect: str | None = None,
        global_id: str | None = None,
        subject: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if category:
            clauses.append("category = ?")
            params.append(category)
        if aspect:
            clauses.append("aspect = ?")
            params.append(aspect)
        if global_id:
            clauses.append("(left_global_id = ? OR right_global_id = ?)")
            params.extend((global_id, global_id))
        if subject:
            clauses.append("subject = ?")
            params.append(subject)
        sql = "SELECT * FROM changes"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ordinal"
        return [self._change_row(row) for row in self.conn.execute(sql, params)]

    def get(self, change_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM changes WHERE change_id=?", (change_id,)
        ).fetchone()
        return None if row is None else self._change_row(row)

    def evidence(self, change_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM evidence WHERE change_id=? ORDER BY side, source_id, step_id",
            (change_id,),
        )
        return [dict(r) for r in rows]

    def object_summary(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM object_summary ORDER BY pair_key")
        out = []
        for row in rows:
            data = dict(row)
            data["categories"] = json.loads(data.pop("categories_json"))
            out.append(data)
        return out

    def summary(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT category, COUNT(*) c FROM changes GROUP BY category ORDER BY category"
        )
        counts = {row["category"]: int(row["c"]) for row in rows}
        aspect_rows = self.conn.execute(
            "SELECT aspect, COUNT(*) c FROM changes GROUP BY aspect ORDER BY aspect"
        )
        aspects = {row["aspect"]: int(row["c"]) for row in aspect_rows}
        return {
            "change_count": int(
                self.conn.execute("SELECT COUNT(*) c FROM changes").fetchone()["c"]
            ),
            "categories": counts,
            "aspects": aspects,
            "object_pairs": int(
                self.conn.execute("SELECT COUNT(*) c FROM object_summary").fetchone()["c"]
            ),
        }

    def mappings(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT mapping_json FROM identity_mappings ORDER BY ordinal"
        )
        return [json.loads(row["mapping_json"]) for row in rows]

    def side_sources(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM side_sources ORDER BY side, source_id"
        )
        return [dict(r) for r in rows]

    def iter_jsonl(self) -> Iterator[dict[str, Any]]:
        with open(os.path.join(self.path, "changes.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def _change_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in (
            "locator",
            "before_json",
            "after_json",
            "before_provenance_json",
            "after_provenance_json",
        ):
            value = data.pop(key)
            out_key = key[:-5] if key.endswith("_json") else key
            data[out_key] = json.loads(value) if value is not None else None
        data["left"] = _side_ref(data.pop("left_source_id"), data.pop("left_sha256"))
        data["right"] = _side_ref(data.pop("right_source_id"), data.pop("right_sha256"))
        mapping_ordinal = data.pop("mapping_ordinal")
        mapping_rationale = data.pop("mapping_rationale")
        data["identity"] = {
            "kind": data.pop("identity_kind"),
            "left_global_id": data.pop("left_global_id"),
            "right_global_id": data.pop("right_global_id"),
            "mapping": None
            if mapping_ordinal is None
            else {"ordinal": mapping_ordinal, "rationale": mapping_rationale},
        }
        data["evidence"] = self.evidence(data["change_id"])
        return data


def make_staging(parent: str) -> str:
    return tempfile.mkdtemp(prefix=".ifc-diff-", dir=parent)
