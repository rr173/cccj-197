"""Acceptance rules and verification checkpoint inputs.

Both files are plain JSON and are loaded strictly: unknown keys, wrong types
and malformed timestamps reject the whole run before any output is produced.

Timestamp precision
-------------------
All instants (ledger ``recorded_at``/``valid_from``/``valid_to`` and checkpoint
``effective_at``/``known_at``) are whole-second UTC instants written as
``YYYY-MM-DDTHH:MM:SSZ`` with a mandatory ``Z`` suffix.  No other offset is
accepted and fractional seconds are rejected.  Because the format is fixed and
zero-padded, lexicographic string comparison is chronological comparison; the
canonical strings are stored and compared directly.  Validity intervals are
half-open: ``[valid_from, valid_to)``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .errors import CheckpointError, RulesError

COVERAGE_DEVICE = "device"
COVERAGE_DEVICE_AND_SPACE = "device_and_space"
COVERAGE_MODES = frozenset({COVERAGE_DEVICE, COVERAGE_DEVICE_AND_SPACE})

DECISION_ACCEPTED = "accepted"
DECISION_REJECTED = "rejected"
DECISION_WITHDRAWN = "withdrawn"
DECISIONS = frozenset({DECISION_ACCEPTED, DECISION_REJECTED, DECISION_WITHDRAWN})

TARGET_DEVICE = "device"
TARGET_SPACE = "space"
TARGET_KINDS = frozenset({TARGET_DEVICE, TARGET_SPACE})

TIMESTAMP_PRECISION_NOTE = (
    "all instants are whole-second UTC timestamps with a mandatory 'Z' suffix "
    "(YYYY-MM-DDTHH:MM:SSZ); fractional seconds and numeric offsets are "
    "rejected; validity intervals are half-open [valid_from, valid_to)"
)

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def parse_utc_timestamp(value: Any, *, field: str, error: type[Exception], context: str) -> str:
    """Validate and normalise a whole-second UTC timestamp.

    Returns the canonical string (the input already, since the format is
    fixed).  Raises ``error`` for anything else.
    """

    if not isinstance(value, str) or not _TS_RE.match(value):
        raise error(
            f"{context}: {field} must be a whole-second UTC timestamp "
            f"'YYYY-MM-DDTHH:MM:SSZ', got {value!r}"
        )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise error(f"{context}: {field} is not a real calendar time: {value!r}") from exc
    return value


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_keys(obj: dict[str, Any], allowed: set[str], required: set[str], *, error: type[Exception], context: str) -> None:
    unknown = set(obj) - allowed
    if unknown:
        raise error(f"{context}: unknown keys {sorted(unknown)}")
    missing = required - set(obj)
    if missing:
        raise error(f"{context}: missing keys {sorted(missing)}")


def _non_empty_str(value: Any, *, field: str, error: type[Exception], context: str) -> str:
    if not isinstance(value, str) or not value:
        raise error(f"{context}: {field} must be a non-empty string")
    return value


def _positive_int(value: Any, *, field: str, error: type[Exception], context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise error(f"{context}: {field} must be a positive integer")
    return value


def _sha256_text(value: Any, *, field: str, error: type[Exception], context: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise error(f"{context}: {field} must be 64 lowercase hex characters")
    return value


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatterRule:
    code: str
    coverage: str
    min_signatories: int


@dataclass(frozen=True)
class ZoneRule:
    zone_id: str
    root_source_id: str
    root_global_id: str
    device_types: tuple[str, ...]
    matters: tuple[MatterRule, ...]


@dataclass(frozen=True)
class Rules:
    zones: tuple[ZoneRule, ...]
    raw: Any
    sha256: str
    path: str

    def matter_codes(self) -> set[str]:
        return {matter.code for zone in self.zones for matter in zone.matters}


def load_rules(path: str) -> Rules:
    """Load and validate the acceptance rules file."""

    context = "rules"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RulesError(f"rules: cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RulesError("rules: top level must be a JSON object")
    _require_keys(
        raw,
        allowed={"rules_version", "zones"},
        required={"zones"},
        error=RulesError,
        context=context,
    )
    if "rules_version" in raw and raw["rules_version"] != 1:
        raise RulesError(f"rules: unsupported rules_version {raw['rules_version']!r}")
    zones_raw = raw["zones"]
    if not isinstance(zones_raw, list) or not zones_raw:
        raise RulesError("rules: 'zones' must be a non-empty list")
    zones: list[ZoneRule] = []
    seen_ids: set[str] = set()
    for index, zone_raw in enumerate(zones_raw):
        zctx = f"rules zone #{index}"
        if not isinstance(zone_raw, dict):
            raise RulesError(f"{zctx}: must be an object")
        _require_keys(
            zone_raw,
            allowed={"zone_id", "root", "device_types", "matters"},
            required={"zone_id", "root", "device_types", "matters"},
            error=RulesError,
            context=zctx,
        )
        zone_id = _non_empty_str(zone_raw["zone_id"], field="zone_id", error=RulesError, context=zctx)
        if zone_id in seen_ids:
            raise RulesError(f"{zctx}: duplicate zone_id {zone_id!r}")
        seen_ids.add(zone_id)
        root = zone_raw["root"]
        if not isinstance(root, dict):
            raise RulesError(f"{zctx}: 'root' must be an object")
        _require_keys(
            root,
            allowed={"source_id", "global_id"},
            required={"source_id", "global_id"},
            error=RulesError,
            context=f"{zctx} root",
        )
        root_source = _non_empty_str(root["source_id"], field="source_id", error=RulesError, context=f"{zctx} root")
        root_gid = _non_empty_str(root["global_id"], field="global_id", error=RulesError, context=f"{zctx} root")
        device_types = zone_raw["device_types"]
        if (
            not isinstance(device_types, list)
            or not device_types
            or any(not isinstance(t, str) or not t.startswith("Ifc") for t in device_types)
        ):
            raise RulesError(f"{zctx}: 'device_types' must be a non-empty list of IFC entity type names")
        matters_raw = zone_raw["matters"]
        if not isinstance(matters_raw, list) or not matters_raw:
            raise RulesError(f"{zctx}: 'matters' must be a non-empty list")
        matters: list[MatterRule] = []
        seen_codes: set[str] = set()
        for m_index, matter_raw in enumerate(matters_raw):
            mctx = f"{zctx} matter #{m_index}"
            if not isinstance(matter_raw, dict):
                raise RulesError(f"{mctx}: must be an object")
            _require_keys(
                matter_raw,
                allowed={"code", "coverage", "min_signatories"},
                required={"code", "coverage", "min_signatories"},
                error=RulesError,
                context=mctx,
            )
            code = _non_empty_str(matter_raw["code"], field="code", error=RulesError, context=mctx)
            if code in seen_codes:
                raise RulesError(f"{mctx}: duplicate matter code {code!r} in zone {zone_id!r}")
            seen_codes.add(code)
            coverage = matter_raw["coverage"]
            if coverage not in COVERAGE_MODES:
                raise RulesError(
                    f"{mctx}: coverage must be one of {sorted(COVERAGE_MODES)}, got {coverage!r}"
                )
            min_signatories = _positive_int(
                matter_raw["min_signatories"], field="min_signatories", error=RulesError, context=mctx
            )
            matters.append(MatterRule(code=code, coverage=coverage, min_signatories=min_signatories))
        zones.append(
            ZoneRule(
                zone_id=zone_id,
                root_source_id=root_source,
                root_global_id=root_gid,
                device_types=tuple(device_types),
                matters=tuple(matters),
            )
        )
    return Rules(zones=tuple(zones), raw=raw, sha256=sha256_file(path), path=os.path.abspath(path))


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Checkpoint:
    idx: int
    checkpoint_id: str
    effective_at: str
    known_at: str


@dataclass(frozen=True)
class CheckpointSet:
    checkpoints: tuple[Checkpoint, ...]
    sha256: str
    path: str


def load_checkpoints(path: str) -> CheckpointSet:
    """Load and validate the verification checkpoint set."""

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"checkpoints: cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CheckpointError("checkpoints: top level must be a JSON object")
    _require_keys(
        raw,
        allowed={"checkpoints"},
        required={"checkpoints"},
        error=CheckpointError,
        context="checkpoints",
    )
    items = raw["checkpoints"]
    if not isinstance(items, list) or not items:
        raise CheckpointError("checkpoints: 'checkpoints' must be a non-empty list")
    out: list[Checkpoint] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        ctx = f"checkpoints #{index}"
        if not isinstance(item, dict):
            raise CheckpointError(f"{ctx}: must be an object")
        _require_keys(
            item,
            allowed={"checkpoint_id", "effective_at", "known_at"},
            required={"effective_at", "known_at"},
            error=CheckpointError,
            context=ctx,
        )
        checkpoint_id = item.get("checkpoint_id", f"cp-{index + 1}")
        checkpoint_id = _non_empty_str(checkpoint_id, field="checkpoint_id", error=CheckpointError, context=ctx)
        if checkpoint_id in seen_ids:
            raise CheckpointError(f"{ctx}: duplicate checkpoint_id {checkpoint_id!r}")
        seen_ids.add(checkpoint_id)
        effective_at = parse_utc_timestamp(item["effective_at"], field="effective_at", error=CheckpointError, context=ctx)
        known_at = parse_utc_timestamp(item["known_at"], field="known_at", error=CheckpointError, context=ctx)
        out.append(
            Checkpoint(idx=index, checkpoint_id=checkpoint_id, effective_at=effective_at, known_at=known_at)
        )
    return CheckpointSet(checkpoints=tuple(out), sha256=sha256_file(path), path=os.path.abspath(path))
