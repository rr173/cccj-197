"""Signoff revision ledger: JSONL parsing, integrity checks, on-disk index.

The ledger is a UTF-8 JSONL file.  Each line is one *complete* revision of a
signoff record::

    {"signoff_id": "SIG-1", "revision": 2,
     "recorded_at": "2026-03-15T10:00:00Z",
     "valid_from": "2026-03-01T00:00:00Z", "valid_to": null,
     "decision": "accepted", "signatory": "party-alpha",
     "matter_code": "COMMISSION",
     "target_kind": "device",
     "target": {"source_id": "model", "global_id": "..."},
     "document": {"path": "docs/sig-1-r2.pdf", "sha256": "...64 hex..."}}

Integrity rules (any violation rejects the whole run and locates the line):

* every line is a JSON object with exactly the known fields;
* ``revision`` is a positive integer; timestamps follow the documented
  whole-second UTC ``Z`` format; ``valid_to`` (optional) is strictly after
  ``valid_from`` -- the interval is ``[valid_from, valid_to)``;
* ``decision`` is ``accepted``/``rejected``/``withdrawn``; an ``accepted``
  revision must carry a ``document`` with a ledger-relative path and SHA-256;
* the same (``signoff_id``, ``revision``) may repeat only with byte-identical
  normalised content (a replay); conflicting content is an error;
* within one ``signoff_id``, ``recorded_at`` must be non-decreasing as the
  revision number grows (several revisions may share one recording instant;
  the revision number is then the order key).

The parsed revisions are stored in a small SQLite index on disk so the
verification never holds the whole ledger in memory and never rescans the
JSONL per checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..keys import canonical_json
from .errors import LedgerError
from .inputs import (
    DECISION_ACCEPTED,
    DECISIONS,
    TARGET_KINDS,
    _positive_int,
    _sha256_text,
    parse_utc_timestamp,
)

LEDGER_SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE revisions (
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
    content_hash TEXT NOT NULL,
    PRIMARY KEY (signoff_id, revision)
);
CREATE INDEX revisions_recorded ON revisions(recorded_at, revision);
CREATE INDEX revisions_target ON revisions(target_source_id, target_global_id);
CREATE INDEX revisions_matter ON revisions(matter_code);
"""

LEDGER_FIELDS = frozenset(
    {
        "signoff_id",
        "revision",
        "recorded_at",
        "valid_from",
        "valid_to",
        "decision",
        "signatory",
        "matter_code",
        "target_kind",
        "target",
        "document",
    }
)

REQUIRED_FIELDS = frozenset(
    {
        "signoff_id",
        "revision",
        "recorded_at",
        "valid_from",
        "decision",
        "signatory",
        "matter_code",
        "target_kind",
        "target",
    }
)


@dataclass
class LedgerInfo:
    path: str
    sha256: str
    line_count: int
    revision_count: int
    replay_count: int


def _check_document_path(path: Any, *, line_no: int) -> str:
    if not isinstance(path, str) or not path:
        raise LedgerError("document.path must be a non-empty relative path", line_no)
    if os.path.isabs(path) or path.startswith("/"):
        raise LedgerError(f"document.path must be relative to the ledger directory: {path!r}", line_no)
    parts = path.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise LedgerError(
            f"document.path must not contain empty, '.' or '..' segments: {path!r}", line_no
        )
    return "/".join(parts)


def _parse_line(obj: Any, line_no: int) -> dict[str, Any]:
    def _err(message: str) -> LedgerError:
        return LedgerError(message, line_no)

    if not isinstance(obj, dict):
        raise LedgerError("line is not a JSON object", line_no)
    unknown = set(obj) - LEDGER_FIELDS
    if unknown:
        raise LedgerError(f"unknown fields {sorted(unknown)}", line_no)
    missing = REQUIRED_FIELDS - set(obj)
    if missing:
        raise LedgerError(f"missing fields {sorted(missing)}", line_no)

    signoff_id = obj["signoff_id"]
    if not isinstance(signoff_id, str) or not signoff_id:
        raise LedgerError("signoff_id must be a non-empty string", line_no)
    revision = _positive_int(obj["revision"], field="revision", error=_err, context="entry")
    recorded_at = parse_utc_timestamp(obj["recorded_at"], field="recorded_at", error=_err, context="entry")
    valid_from = parse_utc_timestamp(obj["valid_from"], field="valid_from", error=_err, context="entry")
    valid_to = obj.get("valid_to")
    if valid_to is not None:
        valid_to = parse_utc_timestamp(valid_to, field="valid_to", error=_err, context="entry")
        if valid_to <= valid_from:
            raise LedgerError(
                f"validity interval is empty: valid_to {valid_to} <= valid_from {valid_from}",
                line_no,
            )
    decision = obj["decision"]
    if decision not in DECISIONS:
        raise LedgerError(f"decision must be one of {sorted(DECISIONS)}, got {decision!r}", line_no)
    signatory = obj["signatory"]
    if not isinstance(signatory, str) or not signatory:
        raise LedgerError("signatory must be a non-empty string", line_no)
    matter_code = obj["matter_code"]
    if not isinstance(matter_code, str) or not matter_code:
        raise LedgerError("matter_code must be a non-empty string", line_no)
    target_kind = obj["target_kind"]
    if target_kind not in TARGET_KINDS:
        raise LedgerError(f"target_kind must be one of {sorted(TARGET_KINDS)}, got {target_kind!r}", line_no)
    target = obj["target"]
    if not isinstance(target, dict):
        raise LedgerError("target must be an object", line_no)
    unknown_target = set(target) - {"source_id", "global_id"}
    if unknown_target:
        raise LedgerError(f"target has unknown fields {sorted(unknown_target)}", line_no)
    target_source = target.get("source_id")
    target_gid = target.get("global_id")
    if not isinstance(target_source, str) or not target_source:
        raise LedgerError("target.source_id must be a non-empty string", line_no)
    if not isinstance(target_gid, str) or not target_gid:
        raise LedgerError("target.global_id must be a non-empty string", line_no)

    document = obj.get("document")
    doc_path = None
    doc_sha256 = None
    if document is not None:
        if not isinstance(document, dict):
            raise LedgerError("document must be an object", line_no)
        unknown_doc = set(document) - {"path", "sha256"}
        if unknown_doc:
            raise LedgerError(f"document has unknown fields {sorted(unknown_doc)}", line_no)
        doc_path = _check_document_path(document.get("path"), line_no=line_no)
        doc_sha256 = _sha256_text(
            document.get("sha256"), field="document.sha256", error=_err, context="entry"
        )
    if decision == DECISION_ACCEPTED and document is None:
        raise LedgerError("an accepted revision must carry a document with path and sha256", line_no)

    row = {
        "signoff_id": signoff_id,
        "revision": revision,
        "recorded_at": recorded_at,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "decision": decision,
        "signatory": signatory,
        "matter_code": matter_code,
        "target_kind": target_kind,
        "target_source_id": target_source,
        "target_global_id": target_gid,
        "doc_path": doc_path,
        "doc_sha256": doc_sha256,
    }
    row["content_hash"] = hashlib.sha256(canonical_json(row).encode("utf-8")).hexdigest()
    return row


def build_ledger_index(ledger_path: str, db_path: str) -> LedgerInfo:
    """Parse and validate the JSONL ledger into an on-disk SQLite index.

    Raises :class:`LedgerError` on the first structural or integrity
    violation; nothing is left behind for the caller to clean up beyond the
    throw-away staging database.
    """

    ledger_path = os.path.abspath(ledger_path)
    if not os.path.isfile(ledger_path):
        raise LedgerError(f"ledger not found: {ledger_path}")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(LEDGER_SCHEMA_SQL)
        conn.execute("BEGIN")
        file_hash = hashlib.sha256()
        line_count = 0
        revision_count = 0
        replay_count = 0
        with open(ledger_path, "rb") as fh:
            for raw_line in fh:
                file_hash.update(raw_line)
                line_count += 1
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    text = stripped.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise LedgerError(f"line is not valid UTF-8: {exc}", line_count) from exc
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise LedgerError(f"invalid JSON: {exc}", line_count) from exc
                row = _parse_line(obj, line_count)
                try:
                    conn.execute(
                        "INSERT INTO revisions(signoff_id, revision, recorded_at, valid_from, "
                        "valid_to, decision, signatory, matter_code, target_kind, "
                        "target_source_id, target_global_id, doc_path, doc_sha256, line_no, "
                        "content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                            line_count,
                            row["content_hash"],
                        ),
                    )
                    revision_count += 1
                except sqlite3.IntegrityError:
                    existing = conn.execute(
                        "SELECT content_hash, line_no FROM revisions WHERE signoff_id=? AND revision=?",
                        (row["signoff_id"], row["revision"]),
                    ).fetchone()
                    if existing is not None and existing[0] == row["content_hash"]:
                        # Same signoff, same revision, same content: a replay.
                        replay_count += 1
                        continue
                    first_line = existing[1] if existing is not None else "?"
                    raise LedgerError(
                        f"conflicting content for signoff {row['signoff_id']!r} "
                        f"revision {row['revision']} (first seen at line {first_line})",
                        line_count,
                    ) from None
        # Revision-number order must agree with recorded_at order per signoff.
        previous: dict[str, tuple[str, int, int]] = {}
        cursor = conn.execute(
            "SELECT signoff_id, revision, recorded_at, line_no FROM revisions "
            "ORDER BY signoff_id, revision"
        )
        for signoff_id, revision, recorded_at, line_no in cursor:
            prior = previous.get(signoff_id)
            if prior is not None and recorded_at < prior[0]:
                raise LedgerError(
                    f"signoff {signoff_id!r}: revision {revision} was recorded at "
                    f"{recorded_at}, before revision {prior[1]} recorded at {prior[0]} "
                    f"(line {prior[2]}); revision order and recorded order disagree",
                    line_no,
                )
            previous[signoff_id] = (recorded_at, revision, line_no)
        info = LedgerInfo(
            path=ledger_path,
            sha256=file_hash.hexdigest(),
            line_count=line_count,
            revision_count=revision_count,
            replay_count=replay_count,
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            ("ledger", canonical_json(info.__dict__)),
        )
        conn.commit()
        return info
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
