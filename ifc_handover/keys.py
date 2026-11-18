"""Identity keys and canonical JSON helpers.

An IfcRoot object's business identity is ``(source_id, GlobalId)``.  Only when
a node has no GlobalId is a source-scoped STEP identity used:
``(source_id, file_sha256, step_id)``.  A key is unique inside a repository
because the source id is part of it.  STEP numbers never merge objects and no
GlobalId is invented.
"""

from __future__ import annotations

import json
import re
from typing import Any


def step_scoped_key(source_id: str, sha256: str, step_id: int) -> str:
    return f"step:{source_id}:{sha256}:{step_id}"


def root_key(source_id: str, global_id: str | None, sha256: str, step_id: int) -> str:
    if global_id:
        return f"gid:{source_id}:{global_id}"
    return step_scoped_key(source_id, sha256, step_id)


def conflicting_root_key(source_id: str, global_id: str, step_id: int) -> str:
    """Storage key for a duplicate-GlobalId member.

    The business identity is still the (source, GlobalId) pair; the suffix only
    keeps storage unique so both conflicting entities stay inspectable.
    """

    return f"gid:{source_id}:{global_id}@{step_id}"


def key_kind(key: str) -> str:
    if key.startswith("gid:"):
        return "gid"
    return "step"


def is_conflict_key(key: str) -> bool:
    return key.startswith("gid:") and "@" in key


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def safe_file_component(text: str, *, fallback: str = "source") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return cleaned or fallback
