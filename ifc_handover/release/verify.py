"""Public entry point for offline handover release verification.

One run reads one ingested object repository, one acceptance rules file, one
signoff revision ledger and one checkpoint set, and writes an independently
re-openable release dataset.  All inputs stay read-only; the repository is
fingerprinted at the start and re-verified before publication, and the
rules/ledger/checkpoint files plus every checked document are re-hashed before
the dataset is published.  Any change raises :class:`InputModifiedError` and
the previous successful report at the destination is preserved.
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
    ReleaseDataset,
    is_complete_dataset,
    make_staging,
    publish_dataset,
)
from .engine import ReleaseEngine
from .errors import CheckpointError, LedgerError, ReleaseVerificationError, RulesError
from .inputs import (
    TIMESTAMP_PRECISION_NOTE,
    CheckpointSet,
    load_checkpoints,
    load_rules,
    sha256_file,
)
from .ledger import build_ledger_index
from .scope import ScopeBuilder

__all__ = [
    "CheckpointError",
    "InputModifiedError",
    "LedgerError",
    "ReleaseDataset",
    "ReleaseVerificationError",
    "RulesError",
    "build_release_verification",
    "is_complete_dataset",
]


def build_release_verification(
    repository: str,
    rules_path: str,
    ledger_path: str,
    checkpoints_path: str,
    dest: str,
    *,
    label: str | None = None,
) -> dict[str, Any]:
    """Verify zone release conditions and write a release dataset at ``dest``.

    Returns the dataset manifest.  Raises :class:`RulesError`,
    :class:`CheckpointError`, :class:`LedgerError`,
    :class:`ReleaseVerificationError` or :class:`InputModifiedError` without
    publishing anything on failure; a previously completed dataset at ``dest``
    is left untouched.
    """

    dest = os.path.abspath(dest)
    parent = os.path.dirname(dest) or "."
    os.makedirs(parent, exist_ok=True)
    staging = make_staging(parent)
    snapshot: RepoSnapShot | None = None
    work: sqlite3.Connection | None = None
    ledger_conn: sqlite3.Connection | None = None
    try:
        # -- fixed inputs, all read-only --------------------------------
        snapshot = RepoSnapShot(repository, label="repository")
        rules = load_rules(rules_path)
        checkpoint_set = load_checkpoints(checkpoints_path)
        ledger_db = os.path.join(staging, "ledger-index.sqlite")
        ledger_info = build_ledger_index(ledger_path, ledger_db)
        ledger_conn = sqlite3.connect(ledger_db)
        ledger_conn.row_factory = sqlite3.Row

        # -- output database in staging ----------------------------------
        work_path = os.path.join(staging, "release.sqlite")
        work = sqlite3.connect(work_path)
        work.row_factory = sqlite3.Row
        work.execute("PRAGMA synchronous=NORMAL")
        work.execute("PRAGMA temp_store=FILE")
        work.executescript(DATASET_SCHEMA_SQL)
        work.execute("BEGIN")

        # -- zone scopes, determined matters, coverage targets ----------
        ScopeBuilder(snapshot.conn, work).build(rules)
        work.commit()

        # -- static tables -----------------------------------------------
        for checkpoint in checkpoint_set.checkpoints:
            work.execute(
                "INSERT INTO checkpoints(idx, checkpoint_id, effective_at, known_at) "
                "VALUES(?,?,?,?)",
                (
                    checkpoint.idx,
                    checkpoint.checkpoint_id,
                    checkpoint.effective_at,
                    checkpoint.known_at,
                ),
            )
        for source in snapshot.sources():
            object_count = snapshot.conn.execute(
                "SELECT COUNT(*) c FROM roots WHERE source_id=? AND is_object=1",
                (source.source_id,),
            ).fetchone()["c"]
            work.execute(
                "INSERT INTO sources(source_id, sha256, status, object_count) VALUES(?,?,?,?)",
                (source.source_id, source.sha256, source.status, int(object_count)),
            )
        work.commit()

        # -- per-checkpoint evaluation ------------------------------------
        engine = ReleaseEngine(
            work,
            snapshot.conn,
            ledger_conn,
            os.path.dirname(os.path.abspath(ledger_path)),
            rules.matter_codes(),
        )
        engine.run(list(checkpoint_set.checkpoints))

        # -- bind the ledger content into the dataset ---------------------
        _copy_ledger_revisions(ledger_conn, work)
        for table in INTERNAL_TABLES:
            work.execute(f"DROP TABLE IF EXISTS {table}")
        work.commit()

        # -- input integrity re-verification ------------------------------
        snapshot.assert_unchanged()
        _assert_file_unchanged(rules.path, rules.sha256, "rules")
        _assert_file_unchanged(checkpoint_set.path, checkpoint_set.sha256, "checkpoints")
        _assert_file_unchanged(ledger_info.path, ledger_info.sha256, "ledger")
        _reverify_file_checks(work, os.path.dirname(os.path.abspath(ledger_path)))

        # -- manifest + completion marker ---------------------------------
        run_summary = _run_summary(work, checkpoint_set)
        manifest = {
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "kind": "ifc_handover.release_dataset",
            "label": label,
            "repository": {
                "path": os.path.abspath(repository),
                "fingerprint": snapshot.start_fingerprint,
                "sources": [s.as_json() for s in snapshot.sources()],
            },
            "rules": {"path": rules.path, "sha256": rules.sha256},
            "ledger": {
                "path": ledger_info.path,
                "sha256": ledger_info.sha256,
                "lines": ledger_info.line_count,
                "revisions": ledger_info.revision_count,
                "replays": ledger_info.replay_count,
            },
            "checkpoints_file": {
                "path": checkpoint_set.path,
                "sha256": checkpoint_set.sha256,
            },
            "timestamp_precision": TIMESTAMP_PRECISION_NOTE,
            "run": run_summary,
            "determinism_note": (
                "matters are keyed by content hash and every table is emitted "
                "in a stable order; the same fixed inputs reproduce the same "
                "business records"
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
        ledger_conn.close()
        ledger_conn = None
        snapshot.close()
        snapshot = None
        os.unlink(ledger_db)

        _write_jsonl(work_path, os.path.join(staging, "release.jsonl"))
        with open(os.path.join(staging, "README.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(README_TEXT)

        publish_dataset(staging, dest)
        staging = ""
        with ReleaseDataset(dest) as dataset:
            return dataset.manifest()
    finally:
        if work is not None:
            work.close()
        if ledger_conn is not None:
            ledger_conn.close()
        if snapshot is not None:
            snapshot.close()
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def _copy_ledger_revisions(ledger_conn: sqlite3.Connection, work: sqlite3.Connection) -> None:
    rows = ledger_conn.execute(
        "SELECT signoff_id, revision, recorded_at, valid_from, valid_to, decision, "
        "signatory, matter_code, target_kind, target_source_id, target_global_id, "
        "doc_path, doc_sha256, line_no FROM revisions ORDER BY signoff_id, revision"
    )
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(tuple(row))
        if len(batch) >= 500:
            work.executemany(
                "INSERT INTO ledger_revisions(signoff_id, revision, recorded_at, valid_from, "
                "valid_to, decision, signatory, matter_code, target_kind, target_source_id, "
                "target_global_id, doc_path, doc_sha256, line_no) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch,
            )
            batch = []
    if batch:
        work.executemany(
            "INSERT INTO ledger_revisions(signoff_id, revision, recorded_at, valid_from, "
            "valid_to, decision, signatory, matter_code, target_kind, target_source_id, "
            "target_global_id, doc_path, doc_sha256, line_no) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch,
        )


def _assert_file_unchanged(path: str, expected_sha256: str, label: str) -> None:
    current = sha256_file(path)
    if current != expected_sha256:
        raise InputModifiedError(
            f"{label} file {path} changed during the verification run; "
            "refusing to publish a mixed-version release dataset"
        )


def _reverify_file_checks(work: sqlite3.Connection, ledger_dir: str) -> None:
    """Re-hash every document that was checked, guarding the binding."""

    rows = work.execute(
        "SELECT path, declared_sha256, computed_sha256, status FROM file_checks"
    ).fetchall()
    for row in rows:
        full = os.path.join(ledger_dir, *row["path"].split("/"))
        try:
            computed = sha256_file(full)
        except OSError:
            computed = None
        if row["status"] == "ok" and computed != row["declared_sha256"]:
            raise InputModifiedError(
                f"signoff document {row['path']} changed during the verification run"
            )
        if row["status"] == "hash_mismatch" and computed != row["computed_sha256"]:
            raise InputModifiedError(
                f"signoff document {row['path']} changed during the verification run"
            )
        if row["status"] == "missing" and computed is not None:
            raise InputModifiedError(
                f"signoff document {row['path']} appeared during the verification run"
            )


def _run_summary(work: sqlite3.Connection, checkpoint_set: CheckpointSet) -> dict[str, Any]:
    decisions = {}
    for row in work.execute(
        "SELECT checkpoint_idx, decision, COUNT(*) c FROM zone_decisions "
        "GROUP BY checkpoint_idx, decision"
    ):
        checkpoint_id = checkpoint_set.checkpoints[row["checkpoint_idx"]].checkpoint_id
        decisions.setdefault(checkpoint_id, {})[row["decision"]] = int(row["c"])
    statuses = {}
    for row in work.execute(
        "SELECT checkpoint_idx, status, COUNT(*) c FROM matter_results "
        "GROUP BY checkpoint_idx, status"
    ):
        checkpoint_id = checkpoint_set.checkpoints[row["checkpoint_idx"]].checkpoint_id
        statuses.setdefault(checkpoint_id, {})[row["status"]] = int(row["c"])
    return {
        "zones": int(work.execute("SELECT COUNT(*) c FROM zones").fetchone()["c"]),
        "matters": int(work.execute("SELECT COUNT(*) c FROM matters").fetchone()["c"]),
        "checkpoints": len(checkpoint_set.checkpoints),
        "zone_decisions": decisions,
        "matter_statuses": statuses,
        "anomalies": int(work.execute("SELECT COUNT(*) c FROM anomalies").fetchone()["c"]),
        "file_checks": int(work.execute("SELECT COUNT(*) c FROM file_checks").fetchone()["c"]),
    }


def _write_jsonl(db_path: str, jsonl_path: str) -> None:
    """Emit zone decisions and matter results in a stable order."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        checkpoints = {
            row["idx"]: row["checkpoint_id"]
            for row in conn.execute("SELECT idx, checkpoint_id FROM checkpoints")
        }
        with open(jsonl_path, "w", encoding="utf-8", newline="\n") as fh:
            for row in conn.execute(
                "SELECT * FROM zone_decisions ORDER BY checkpoint_idx, zone_id"
            ):
                fh.write(
                    canonical_json(
                        {
                            "kind": "zone_decision",
                            "checkpoint_id": checkpoints[row["checkpoint_idx"]],
                            "zone_id": row["zone_id"],
                            "decision": row["decision"],
                            "counts": json.loads(row["counts_json"]),
                            "reasons": json.loads(row["reasons_json"]),
                        }
                    )
                    + "\n"
                )
            for row in conn.execute(
                "SELECT m.*, r.status, r.verified_count, r.pending_count, "
                "r.verified_signatories_json, r.pending_signatories_json, r.checkpoint_idx "
                "FROM matter_results r JOIN matters m ON m.record_id=r.record_id "
                "ORDER BY r.checkpoint_idx, m.ordinal"
            ):
                fh.write(
                    canonical_json(
                        {
                            "kind": "matter",
                            "checkpoint_id": checkpoints[row["checkpoint_idx"]],
                            "zone_id": row["zone_id"],
                            "record_id": row["record_id"],
                            "device": {
                                "source_id": row["device_source_id"],
                                "global_id": row["device_global_id"],
                                "step_id": row["device_step_id"],
                                "entity_type": row["device_entity_type"],
                                "name": row["device_name"],
                            },
                            "matter_code": row["matter_code"],
                            "coverage_mode": row["coverage_mode"],
                            "min_signatories": row["min_signatories"],
                            "status": row["status"],
                            "verified_signatories": json.loads(row["verified_signatories_json"]),
                            "pending_signatories": json.loads(row["pending_signatories_json"]),
                        }
                    )
                    + "\n"
                )
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        conn.close()
