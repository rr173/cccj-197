"""Public entry point for spatial quantity take-off.

One run reads one ingested object repository strictly read-only and one
UTF-8 JSON rules file, and writes an independently re-openable take-off
dataset.  The repository is fingerprinted when the run starts and
re-verified before publication; the rules file is re-hashed at the same
point.  Any change raises :class:`InputModifiedError` and the previous
successful dataset at the destination is preserved.

Object-level bad quantities are isolated into their own line state and the
remaining objects continue; structural rule errors and an input that changes
mid-run reject the whole build.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from typing import Any

from ..diff.snapshot import InputModifiedError, RepoSnapShot
from ..keys import canonical_json
from .dataset import (
    DATASET_SCHEMA_SQL,
    DATASET_SCHEMA_VERSION,
    INTERNAL_TABLES,
    README_TEXT,
    TakeoffDataset,
    make_staging,
    publish_dataset,
)
from .engine import TakeoffEngine
from .inputs import TakeoffRules, load_rules, sha256_file
from .scope import ScopeBuilder

__all__ = [
    "InputModifiedError",
    "TakeoffDataset",
    "build_takeoff",
]


def build_takeoff(
    repository: str,
    rules_path: str,
    dest: str,
    *,
    label: str | None = None,
) -> dict[str, Any]:
    """Build the spatial quantity take-off dataset at ``dest``.

    Returns the dataset manifest.  Raises :class:`RulesError`,
    :class:`TakeoffError` or :class:`InputModifiedError` without publishing
    anything on failure; a previously completed dataset at ``dest`` is left
    untouched.
    """

    dest = os.path.abspath(dest)
    parent = os.path.dirname(dest) or "."
    os.makedirs(parent, exist_ok=True)
    staging = make_staging(parent)
    snapshot: RepoSnapShot | None = None
    work: sqlite3.Connection | None = None
    try:
        snapshot = RepoSnapShot(repository, label="repository")
        rules: TakeoffRules = load_rules(rules_path)

        work_path = os.path.join(staging, "takeoff.sqlite")
        work = sqlite3.connect(work_path)
        work.row_factory = sqlite3.Row
        work.execute("PRAGMA synchronous=NORMAL")
        work.execute("PRAGMA temp_store=FILE")
        work.executescript(DATASET_SCHEMA_SQL)
        work.execute("BEGIN")

        _write_rules(work, rules)
        _write_sources(work, snapshot)

        # Scope walks, then per-object quantity lines and exact summaries.
        ScopeBuilder(snapshot.conn, work).build(rules.rules)
        work.commit()
        TakeoffEngine(snapshot.conn, work).run(rules.rules)

        for table in INTERNAL_TABLES:
            work.execute(f"DROP TABLE IF EXISTS {table}")
        work.commit()

        # Input integrity re-verification before publication.
        snapshot.assert_unchanged()
        _assert_file_unchanged(rules.path, rules.sha256, "rules")

        run_summary = _run_summary(work)
        manifest = {
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "kind": "ifc_handover.takeoff_dataset",
            "label": label,
            "repository": {
                "path": os.path.abspath(repository),
                "fingerprint": snapshot.start_fingerprint,
                "sources": [s.as_json() for s in snapshot.sources()],
            },
            "rules": {"path": rules.path, "sha256": rules.sha256},
            "run": run_summary,
            "numeric_note": (
                "all canonical values and totals are exact rational "
                "Fraction arithmetic; no binary float accumulation occurs"
            ),
            "determinism_note": (
                "membership and lines are emitted in a stable order; the same "
                "fixed inputs reproduce the same business records"
            ),
        }
        work.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            ("manifest", canonical_json(manifest)),
        )
        work.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            ("rules", canonical_json(rules.raw)),
        )
        work.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            ("complete", canonical_json({"complete": True})),
        )
        work.commit()
        work.close()
        work = None
        snapshot.close()
        snapshot = None

        _write_jsonl(work_path, os.path.join(staging, "takeoff.jsonl"))
        with open(os.path.join(staging, "README.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(README_TEXT)

        publish_dataset(staging, dest)
        staging = ""
        with TakeoffDataset(dest) as dataset:
            return dataset.manifest()
    finally:
        if work is not None:
            work.close()
        if snapshot is not None:
            snapshot.close()
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def _write_rules(work: sqlite3.Connection, rules: TakeoffRules) -> None:
    for ordinal, rule in enumerate(rules.rules):
        work.execute(
            "INSERT INTO rules(rule_id, ordinal, root_source_id, root_global_id, root_key, "
            "entity_types_json, quantity_identities_json, raw_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                rule.rule_id,
                ordinal,
                rule.root_source_id,
                rule.root_global_id,
                None,
                canonical_json(list(rule.entity_types)),
                canonical_json([q.as_json() for q in rule.quantities]),
                canonical_json(_raw_rule(rule)),
            ),
        )
        for entity_type in rule.entity_types:
            work.execute(
                "INSERT OR IGNORE INTO rule_entities(rule_id, entity_type) VALUES(?,?)",
                (rule.rule_id, entity_type),
            )
        # The per-entity-type binding is stored alongside each identity.
        for entity_type, bound in rule.quantities_by_type.items():
            for q in bound:
                work.execute(
                    "INSERT INTO rule_quantity_identities(rule_id, entity_type, "
                    "qset_name, q_name, q_kind, identity, dimension, canonical_unit) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        rule.rule_id,
                        entity_type,
                        q.quantity_set,
                        q.name,
                        q.declared_type,
                        q.identity,
                        q.dimension,
                        q.canonical_unit,
                    ),
                )
    # Fill root_key once the scope rows exist.
    work.execute(
        "UPDATE rules SET root_key=(SELECT root_key FROM root_scopes "
        "WHERE root_scopes.rule_id=rules.rule_id)"
    )


def _raw_rule(rule: Any) -> dict[str, Any]:
    raw_identities = []
    for entity_type, bound in rule.quantities_by_type.items():
        for q in bound:
            raw_identities.append(
                {
                    "entity_type": entity_type,
                    "quantity_set": q.quantity_set,
                    "name": q.name,
                    "declared_type": q.declared_type,
                }
            )
    return {
        "rule_id": rule.rule_id,
        "root": {
            "source_id": rule.root_source_id,
            "global_id": rule.root_global_id,
        },
        "entity_types": list(rule.entity_types),
        "quantity_identities": raw_identities,
    }


def _write_sources(work: sqlite3.Connection, snapshot: RepoSnapShot) -> None:
    for source in snapshot.sources():
        object_count = snapshot.conn.execute(
            "SELECT COUNT(*) c FROM roots WHERE source_id=? AND is_object=1",
            (source.source_id,),
        ).fetchone()["c"]
        work.execute(
            "INSERT INTO sources(source_id, sha256, status, object_count) VALUES(?,?,?,?)",
            (source.source_id, source.sha256, source.status, int(object_count)),
        )


def _assert_file_unchanged(path: str, expected_sha256: str, label: str) -> None:
    current = sha256_file(path)
    if current != expected_sha256:
        raise InputModifiedError(
            f"{label} file {path} changed during the take-off run; "
            "refusing to publish a mixed-version take-off dataset"
        )


def _run_summary(work: sqlite3.Connection) -> dict[str, Any]:
    line_states = {}
    for row in work.execute(
        "SELECT line_state, COUNT(*) c FROM object_details GROUP BY line_state"
    ):
        line_states[row["line_state"]] = int(row["c"])
    return {
        "rules": int(work.execute("SELECT COUNT(*) c FROM rules").fetchone()["c"]),
        "included_objects": int(
            work.execute(
                "SELECT COUNT(*) c FROM scope_membership WHERE membership='valid'"
            ).fetchone()["c"]
        ),
        "empty_scopes": int(
            work.execute(
                "SELECT COUNT(*) c FROM root_scopes WHERE included_objects=0"
            ).fetchone()["c"]
        ),
        "detail_lines": int(
            work.execute("SELECT COUNT(*) c FROM object_details").fetchone()["c"]
        ),
        "summaries": int(
            work.execute("SELECT COUNT(*) c FROM quantity_summaries").fetchone()["c"]
        ),
        "anomalies": int(
            work.execute("SELECT COUNT(*) c FROM scope_anomalies").fetchone()["c"]
        ),
        "line_states": line_states,
    }


def _write_jsonl(db_path: str, jsonl_path: str) -> None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        with open(jsonl_path, "w", encoding="utf-8", newline="\n") as fh:
            for row in conn.execute(
                "SELECT * FROM object_details ORDER BY rule_id, entity_type, "
                "source_id, step_id, identity"
            ):
                fh.write(
                    canonical_json(
                        {
                            "kind": "object_detail",
                            **{k: row[k] for k in row.keys() if k != "declarations_json"},
                            "declarations": json.loads(row["declarations_json"]),
                        }
                    )
                    + "\n"
                )
            for row in conn.execute(
                "SELECT * FROM quantity_summaries ORDER BY rule_id, entity_type, identity"
            ):
                fh.write(
                    canonical_json(
                        {"kind": "quantity_summary", **dict(row)}
                    )
                    + "\n"
                )
            for row in conn.execute("SELECT * FROM scope_anomalies ORDER BY id"):
                payload = dict(row)
                payload["detail"] = json.loads(payload.pop("detail_json"))
                fh.write(
                    canonical_json({"kind": "scope_anomaly", **payload}) + "\n"
                )
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        conn.close()
