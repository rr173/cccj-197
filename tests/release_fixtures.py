"""Runtime-generated fixtures for the release verification drill.

The scenario (all expectations are hand-written at the bottom of this file and
asserted by ``tests/test_release.py``; nothing is computed by the verifier
itself):

* Device ``E`` sits in room ``B`` under storey ``A``.  Two zones cover it:
  ``zone-a`` rooted at storey ``A`` (also containing device ``F`` in room
  ``B2``) and ``zone-b`` rooted at room ``B``.  Both require the matter
  ``COMMISSION`` with two independent signatories, device + space coverage.
* Initially a storey signoff for ``A`` and a device signoff for ``E`` both
  belong to party alpha, so ``E`` still lacks a second party.
* A later-recorded storey signoff by party beta completes zone ``a`` at the
  new visibility cutoff, while the old cutoff still shows one missing party.
  Zone ``b`` keeps counting only the device signoff (the storey signoff may
  not cross the zone root).
* A revision of beta's signoff then moves its validity start past the
  business moment: at the new cutoff zone ``a`` loses the party again, and at
  the validity start moment it counts once more.
* A second ledger directory corrupts beta's document: zone ``a`` becomes
  undetermined instead of released.
* Source ``other`` has device ``E2`` sharing ``E``'s GlobalId; ``E``'s device
  signoff must not leak across sources.
* ``zone-d`` contains a broken containment edge and a device without a
  GlobalId (scope unverified) while the other zones conclude independently;
  ``zone-e`` is confirmable but empty.

No large generated file is committed; everything is regenerated at test time.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from fixtures import IfcBuilder, gid, sha256_bytes, _s, _write_manifest

# -- labels -----------------------------------------------------------------
STOREY_A = "rel-storey-a"
ROOM_B = "rel-room-b"
ROOM_B2 = "rel-room-b2"
STOREY_C = "rel-storey-c"
STOREY_D = "rel-storey-d"
ROOM_D1 = "rel-room-d1"
STOREY_E = "rel-storey-e"
DEVICE_E = "rel-device-e"
DEVICE_F = "rel-device-f"
DEVICE_G = "rel-device-g"
DEVICE_X = "rel-device-x"
GHOST = "rel-ghost-device"

MATTER = "COMMISSION"
PARTY_ALPHA = "party-alpha"
PARTY_BETA = "party-beta"
PARTY_GAMMA = "party-gamma"

SIG_FLOOR = "SIG-FLOOR"
SIG_DEV_E = "SIG-DEV-E"
SIG_WD = "SIG-WD"
SIG_REJ = "SIG-REJ"
SIG_STRAY = "SIG-STRAY"
SIG_GHOST = "SIG-GHOST"
SIG_FLOOR_B = "SIG-FLOOR-B"

# -- timeline ---------------------------------------------------------------
T = "2026-03-15T17:00:00Z"  # business moment (handover day, 17:00)
T2 = "2026-03-16T17:00:00Z"  # beta's moved validity start
R1 = "2026-03-15T10:00:00Z"
R2 = "2026-03-15T14:00:00Z"
R3 = "2026-03-15T16:00:00Z"
K1 = "2026-03-15T12:00:00Z"
K2 = "2026-03-15T15:00:00Z"
K3 = "2026-03-15T18:00:00Z"
VALID_FROM = "2026-03-01T00:00:00Z"

CP_OLD = "cp-old-cutoff"
CP_NEW = "cp-new-cutoff"
CP_NARROWED = "cp-narrowed"
CP_VALIDITY_START = "cp-validity-start"

DOC_FLOOR_A = "docs/floor-a-r1.pdf"
DOC_DEV_E = "docs/dev-e-r1.pdf"
DOC_WD = "docs/wd-r1.pdf"
DOC_STRAY = "docs/stray-r1.pdf"
DOC_GHOST = "docs/ghost-r1.pdf"
DOC_FLOOR_B1 = "docs/floor-b-r1.pdf"
DOC_FLOOR_B2 = "docs/floor-b-r2.pdf"


@dataclass
class ReleaseFixture:
    root: str
    repo_manifest: str
    rules_path: str
    ledger_path: str
    checkpoints_path: str
    corrupted_ledger_path: str


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def _spatial_chain(b: IfcBuilder, prefix: str) -> tuple[int, int]:
    mm = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", ".MILLI.", ".METRE.")
    units = b.entity("IFCUNITASSIGNMENT", f"(#{mm})")
    project = b.entity(
        "IFCPROJECT", _s(gid(f"{prefix}-project")), "$", _s(prefix), "$", "$", "$", "$",
        "$", f"#{units}",
    )
    site = b.entity(
        "IFCSITE", _s(gid(f"{prefix}-site")), "$", _s("Site"), "$", "$", "$", "$", "$",
        ".ELEMENT.", "$", "$", "$", "$", "$",
    )
    b.entity("IFCRELAGGREGATES", _s(gid(f"{prefix}-agg-ps")), "$", "$", "$",
             f"#{project}", f"(#{site})")
    return project, site


def _storey(b: IfcBuilder, label: str, name: str) -> int:
    return b.entity(
        "IFCBUILDINGSTOREY", _s(gid(label)), "$", _s(name), "$", "$", "$", "$", "$",
        ".ELEMENT.", "$",
    )


def _space(b: IfcBuilder, label: str, name: str) -> int:
    return b.entity(
        "IFCSPACE", _s(gid(label)), "$", _s(name), "$", "$", "$", "$", _s(name),
        ".ELEMENT.", ".SPACE.", "$",
    )


def _device(b: IfcBuilder, label: str | None, name: str) -> int:
    return b.entity(
        "IFCBUILDINGELEMENTPROXY",
        _s(gid(label)) if label is not None else "$",
        "$", _s(name), "$", "$", "$", "$", _s(name), ".NOTDEFINED.",
    )


def _aggregate(b: IfcBuilder, label: str, parent: int, children: list[int]) -> None:
    b.entity("IFCRELAGGREGATES", _s(gid(label)), "$", "$", "$", f"#{parent}",
             "(" + ",".join(f"#{c}" for c in children) + ")")


def _contain(b: IfcBuilder, label: str, elements: list[int], structure: int) -> None:
    b.entity(
        "IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid(label)), "$", "$", "$",
        "(" + ",".join(f"#{e}" for e in elements) + ")", f"#{structure}",
    )


def build_model(root: str) -> str:
    """Write model.ifc + other.ifc and their manifest; return the manifest path."""

    os.makedirs(root, exist_ok=True)
    b = IfcBuilder()
    _project, site = _spatial_chain(b, "rel-model")
    building = b.entity(
        "IFCBUILDING", _s(gid("rel-building")), "$", _s("Building"), "$", "$", "$", "$",
        "$", ".ELEMENT.", "$", "$", "$",
    )
    _aggregate(b, "rel-agg-sb", site, [building])
    storey_a = _storey(b, STOREY_A, "Storey A")
    storey_d = _storey(b, STOREY_D, "Storey D")
    storey_e = _storey(b, STOREY_E, "Storey E")
    _aggregate(b, "rel-agg-bld", building, [storey_a, storey_d, storey_e])
    room_b = _space(b, ROOM_B, "Room B")
    room_b2 = _space(b, ROOM_B2, "Room B2")
    _aggregate(b, "rel-agg-a", storey_a, [room_b, room_b2])
    room_d1 = _space(b, ROOM_D1, "Room D1")
    _aggregate(b, "rel-agg-d", storey_d, [room_d1])
    device_e = _device(b, DEVICE_E, "E")
    device_f = _device(b, DEVICE_F, "F")
    device_g = _device(b, DEVICE_G, "G")
    device_nh = _device(b, None, "NH")  # no GlobalId: source-local identity only
    device_x = _device(b, DEVICE_X, "X")
    # Two devices sharing one GlobalId: a conflicted identity, never determined.
    device_dup1 = _device(b, "rel-device-dup", "DH1")
    device_dup2 = _device(b, "rel-device-dup", "DH2")
    _contain(b, "rel-cont-b", [device_e], room_b)
    _contain(b, "rel-cont-b2", [device_f], room_b2)
    # E is also contained in room B2: a second valid path to the same device
    # inside zone-a must still yield exactly one matter record.
    _contain(b, "rel-cont-b2-e", [device_e], room_b2)
    _contain(b, "rel-cont-d1", [device_g, device_nh, device_dup1, device_dup2], room_d1)
    _contain(b, "rel-cont-x", [device_x], building)
    # Broken containment inside zone D: the related element does not exist.
    b.entity(
        "IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid("rel-cont-d1-broken")), "$", "$", "$",
        f"(#{b.next_id + 5000})", f"#{room_d1}",
    )
    text = b.render(file_name="model.ifc")
    model_path = os.path.join(root, "model.ifc")
    with open(model_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)

    # Second source: device E2 shares E's GlobalId but is a different object.
    b2 = IfcBuilder()
    _project2, site2 = _spatial_chain(b2, "rel-other")
    building2 = b2.entity(
        "IFCBUILDING", _s(gid("rel-building-2")), "$", _s("Building 2"), "$", "$", "$",
        "$", "$", ".ELEMENT.", "$", "$", "$",
    )
    _aggregate(b2, "rel-agg-sb2", site2, [building2])
    storey_c = _storey(b2, STOREY_C, "Storey C")
    _aggregate(b2, "rel-agg-c", building2, [storey_c])
    device_e2 = _device(b2, DEVICE_E, "E2")  # same GlobalId as E, other source
    _contain(b2, "rel-cont-c", [device_e2], storey_c)
    text2 = b2.render(file_name="other.ifc")
    other_path = os.path.join(root, "other.ifc")
    with open(other_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text2)

    return _write_manifest(
        root,
        [
            {"source_id": "model", "path": "model.ifc", "sha256": sha256_bytes(text.encode("utf-8"))},
            {"source_id": "other", "path": "other.ifc", "sha256": sha256_bytes(text2.encode("utf-8"))},
        ],
    )


# ---------------------------------------------------------------------------
# Rules / checkpoints
# ---------------------------------------------------------------------------


def build_rules(path: str) -> str:
    zones = []
    roots = {
        "zone-a": ("model", gid(STOREY_A)),
        "zone-b": ("model", gid(ROOM_B)),
        "zone-c": ("other", gid(STOREY_C)),
        "zone-d": ("model", gid(STOREY_D)),
        "zone-e": ("model", gid(STOREY_E)),
    }
    for zone_id, (source_id, global_id) in roots.items():
        zones.append(
            {
                "zone_id": zone_id,
                "root": {"source_id": source_id, "global_id": global_id},
                "device_types": ["IfcBuildingElementProxy"],
                "matters": [
                    {
                        "code": MATTER,
                        "coverage": "device_and_space",
                        "min_signatories": 2,
                    }
                ],
            }
        )
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"rules_version": 1, "zones": zones}, fh, indent=2)
        fh.write("\n")
    return path


def build_checkpoints(path: str) -> str:
    checkpoints = [
        {"checkpoint_id": CP_OLD, "effective_at": T, "known_at": K1},
        {"checkpoint_id": CP_NEW, "effective_at": T, "known_at": K2},
        {"checkpoint_id": CP_NARROWED, "effective_at": T, "known_at": K3},
        {"checkpoint_id": CP_VALIDITY_START, "effective_at": T2, "known_at": K3},
    ]
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"checkpoints": checkpoints}, fh, indent=2)
        fh.write("\n")
    return path


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

_DOC_CONTENT = {
    DOC_FLOOR_A: b"floor-a signoff r1\n",
    DOC_DEV_E: b"device-e signoff r1\n",
    DOC_WD: b"withdrawn-later signoff r1\n",
    DOC_STRAY: b"stray signoff r1\n",
    DOC_GHOST: b"ghost signoff r1\n",
    DOC_FLOOR_B1: b"floor-a beta signoff r1\n",
    DOC_FLOOR_B2: b"floor-a beta signoff r2\n",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rev(
    signoff_id: str,
    revision: int,
    recorded_at: str,
    valid_from: str,
    decision: str,
    signatory: str,
    target_kind: str,
    source_id: str,
    global_id: str,
    doc: dict | None,
    *,
    valid_to: str | None = None,
) -> dict:
    row = {
        "signoff_id": signoff_id,
        "revision": revision,
        "recorded_at": recorded_at,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "decision": decision,
        "signatory": signatory,
        "matter_code": MATTER,
        "target_kind": target_kind,
        "target": {"source_id": source_id, "global_id": global_id},
    }
    if doc is not None:
        row["document"] = doc
    return row


def _doc_ref(doc: str) -> dict:
    return {"path": doc, "sha256": _sha(_DOC_CONTENT[doc])}


def ledger_revisions(*, include_floor_b_r2: bool = True) -> list[dict]:
    rows = [
        _rev(SIG_FLOOR, 1, R1, VALID_FROM, "accepted", PARTY_ALPHA, "space",
             "model", gid(STOREY_A), _doc_ref(DOC_FLOOR_A)),
        _rev(SIG_DEV_E, 1, R1, VALID_FROM, "accepted", PARTY_ALPHA, "device",
             "model", gid(DEVICE_E), _doc_ref(DOC_DEV_E)),
        _rev(SIG_WD, 1, R1, VALID_FROM, "accepted", PARTY_GAMMA, "device",
             "model", gid(DEVICE_F), _doc_ref(DOC_WD)),
        _rev(SIG_REJ, 1, R1, VALID_FROM, "rejected", PARTY_BETA, "device",
             "model", gid(DEVICE_E), None),
        _rev(SIG_STRAY, 1, R1, VALID_FROM, "accepted", PARTY_ALPHA, "device",
             "model", gid(DEVICE_X), _doc_ref(DOC_STRAY)),
        _rev(SIG_GHOST, 1, R1, VALID_FROM, "accepted", PARTY_ALPHA, "device",
             "model", gid(GHOST), _doc_ref(DOC_GHOST)),
        _rev(SIG_FLOOR_B, 1, R2, VALID_FROM, "accepted", PARTY_BETA, "space",
             "model", gid(STOREY_A), _doc_ref(DOC_FLOOR_B1)),
        _rev(SIG_WD, 2, R2, VALID_FROM, "withdrawn", PARTY_GAMMA, "device",
             "model", gid(DEVICE_F), None),
    ]
    if include_floor_b_r2:
        rows.append(
            _rev(SIG_FLOOR_B, 2, R3, T2, "accepted", PARTY_BETA, "space",
                 "model", gid(STOREY_A), _doc_ref(DOC_FLOOR_B2))
        )
    return rows


def build_ledger(
    root: str,
    *,
    include_floor_b_r2: bool = True,
    corrupted_docs: frozenset[str] = frozenset(),
    extra_lines: tuple[dict, ...] = (),
) -> str:
    """Write a ledger directory (docs + ledger.jsonl); return the JSONL path."""

    docs_dir = os.path.join(root, "docs")
    os.makedirs(docs_dir, exist_ok=True)
    for doc, content in _DOC_CONTENT.items():
        name = doc.split("/", 1)[1]
        data = b"corrupted bytes\n" if doc in corrupted_docs else content
        with open(os.path.join(docs_dir, name), "wb") as fh:
            fh.write(data)
    rows = ledger_revisions(include_floor_b_r2=include_floor_b_r2)
    rows.extend(extra_lines)
    ledger_path = os.path.join(root, "ledger.jsonl")
    with open(ledger_path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return ledger_path


def build_release_fixture(root: str) -> ReleaseFixture:
    os.makedirs(root, exist_ok=True)
    repo_manifest = build_model(os.path.join(root, "model"))
    rules_path = build_rules(os.path.join(root, "rules.json"))
    checkpoints_path = build_checkpoints(os.path.join(root, "checkpoints.json"))
    ledger_path = build_ledger(os.path.join(root, "ledger"))
    corrupted_ledger_path = build_ledger(
        os.path.join(root, "ledger-corrupt"),
        include_floor_b_r2=False,
        corrupted_docs=frozenset({DOC_FLOOR_B1}),
    )
    return ReleaseFixture(
        root=root,
        repo_manifest=repo_manifest,
        rules_path=rules_path,
        ledger_path=ledger_path,
        checkpoints_path=checkpoints_path,
        corrupted_ledger_path=corrupted_ledger_path,
    )


# ---------------------------------------------------------------------------
# Hand-written expectations
# ---------------------------------------------------------------------------

# checkpoint -> zone -> decision
EXPECTED_ZONE_DECISIONS = {
    CP_OLD: {
        "zone-a": "held",
        "zone-b": "held",
        "zone-c": "held",
        "zone-d": "scope_unverified",
        "zone-e": "empty_scope",
    },
    CP_NEW: {
        "zone-a": "released",
        "zone-b": "held",
        "zone-c": "held",
        "zone-d": "scope_unverified",
        "zone-e": "empty_scope",
    },
    CP_NARROWED: {
        "zone-a": "held",
        "zone-b": "held",
        "zone-c": "held",
        "zone-d": "scope_unverified",
        "zone-e": "empty_scope",
    },
    CP_VALIDITY_START: {
        "zone-a": "released",
        "zone-b": "held",
        "zone-c": "held",
        "zone-d": "scope_unverified",
        "zone-e": "empty_scope",
    },
}

# (checkpoint, zone, device name) -> (status, verified signatories, pending)
EXPECTED_MATTERS = {
    (CP_OLD, "zone-a", "E"): ("blocked", [PARTY_ALPHA], []),
    (CP_OLD, "zone-a", "F"): ("ready", [PARTY_ALPHA, PARTY_GAMMA], []),
    (CP_OLD, "zone-b", "E"): ("blocked", [PARTY_ALPHA], []),
    (CP_OLD, "zone-c", "E2"): ("blocked", [], []),
    (CP_OLD, "zone-d", "G"): ("blocked", [], []),
    (CP_NEW, "zone-a", "E"): ("ready", [PARTY_ALPHA, PARTY_BETA], []),
    (CP_NEW, "zone-a", "F"): ("ready", [PARTY_ALPHA, PARTY_BETA], []),
    (CP_NEW, "zone-b", "E"): ("blocked", [PARTY_ALPHA], []),
    (CP_NEW, "zone-c", "E2"): ("blocked", [], []),
    (CP_NARROWED, "zone-a", "E"): ("blocked", [PARTY_ALPHA], []),
    (CP_NARROWED, "zone-a", "F"): ("blocked", [PARTY_ALPHA], []),
    (CP_NARROWED, "zone-b", "E"): ("blocked", [PARTY_ALPHA], []),
    (CP_VALIDITY_START, "zone-a", "E"): ("ready", [PARTY_ALPHA, PARTY_BETA], []),
    (CP_VALIDITY_START, "zone-a", "F"): ("ready", [PARTY_ALPHA, PARTY_BETA], []),
    (CP_VALIDITY_START, "zone-b", "E"): ("blocked", [PARTY_ALPHA], []),
}

# (checkpoint, zone, device name) -> {signoff_id: (classification, reason)}
EXPECTED_SIGNOFFS = {
    (CP_OLD, "zone-a", "E"): {
        SIG_DEV_E: ("counted", None),
        SIG_FLOOR: ("counted", None),
        SIG_REJ: ("excluded", "rejected"),
    },
    (CP_OLD, "zone-a", "F"): {
        SIG_FLOOR: ("counted", None),
        SIG_WD: ("counted", None),
    },
    (CP_OLD, "zone-b", "E"): {
        SIG_DEV_E: ("counted", None),
        SIG_FLOOR: ("excluded", "out_of_coverage"),
        SIG_REJ: ("excluded", "rejected"),
    },
    (CP_NEW, "zone-a", "E"): {
        SIG_DEV_E: ("counted", None),
        SIG_FLOOR: ("counted", None),
        SIG_FLOOR_B: ("counted", None),
        SIG_REJ: ("excluded", "rejected"),
    },
    (CP_NEW, "zone-a", "F"): {
        SIG_FLOOR: ("counted", None),
        SIG_FLOOR_B: ("counted", None),
        SIG_WD: ("excluded", "withdrawn"),
    },
    (CP_NEW, "zone-b", "E"): {
        SIG_DEV_E: ("counted", None),
        SIG_FLOOR: ("excluded", "out_of_coverage"),
        SIG_FLOOR_B: ("excluded", "out_of_coverage"),
        SIG_REJ: ("excluded", "rejected"),
    },
    (CP_NARROWED, "zone-a", "E"): {
        SIG_DEV_E: ("counted", None),
        SIG_FLOOR: ("counted", None),
        SIG_FLOOR_B: ("excluded", "out_of_validity"),
        SIG_REJ: ("excluded", "rejected"),
    },
    (CP_NARROWED, "zone-a", "F"): {
        SIG_FLOOR: ("counted", None),
        SIG_FLOOR_B: ("excluded", "out_of_validity"),
        SIG_WD: ("excluded", "withdrawn"),
    },
    (CP_VALIDITY_START, "zone-a", "E"): {
        SIG_DEV_E: ("counted", None),
        SIG_FLOOR: ("counted", None),
        SIG_FLOOR_B: ("counted", None),
        SIG_REJ: ("excluded", "rejected"),
    },
    (CP_VALIDITY_START, "zone-a", "F"): {
        SIG_FLOOR: ("counted", None),
        SIG_FLOOR_B: ("counted", None),
        SIG_WD: ("excluded", "withdrawn"),
    },
}

# checkpoint -> {signoff_id: anomaly reason}
EXPECTED_ANOMALIES = {
    CP_OLD: {SIG_STRAY: "no_candidate_association", SIG_GHOST: "invalid_target"},
    CP_NEW: {SIG_STRAY: "no_candidate_association", SIG_GHOST: "invalid_target"},
    CP_NARROWED: {SIG_STRAY: "no_candidate_association", SIG_GHOST: "invalid_target"},
    CP_VALIDITY_START: {SIG_STRAY: "no_candidate_association", SIG_GHOST: "invalid_target"},
}

# Corrupted-ledger run at CP_NEW: beta's file fails the hash check.
EXPECTED_CORRUPTED_MATTERS = {
    (CP_NEW, "zone-a", "E"): ("undetermined", [PARTY_ALPHA], [PARTY_BETA]),
    (CP_NEW, "zone-a", "F"): ("undetermined", [PARTY_ALPHA], [PARTY_BETA]),
    (CP_NEW, "zone-b", "E"): ("blocked", [PARTY_ALPHA], []),
}
EXPECTED_CORRUPTED_ZONE_DECISIONS = {
    "zone-a": "held",
    "zone-b": "held",
    "zone-c": "held",
    "zone-d": "scope_unverified",
    "zone-e": "empty_scope",
}


# ---------------------------------------------------------------------------
# Pending-coverage fixtures (identity conflict / invalid-edge candidate path)
# ---------------------------------------------------------------------------

PC_T = "2026-04-01T12:00:00Z"
PC_R1 = "2026-04-01T09:00:00Z"
PC_K1 = "2026-04-01T10:00:00Z"
PC_VALID = "2026-03-01T00:00:00Z"


def _pc_base(b: IfcBuilder) -> tuple[int, int]:
    mm = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", ".MILLI.", ".METRE.")
    units = b.entity("IFCUNITASSIGNMENT", f"(#{mm})")
    project = b.entity(
        "IFCPROJECT", _s(gid("pc-project")), "$", _s("pc"), "$", "$", "$", "$", "$",
        f"#{units}",
    )
    site = b.entity(
        "IFCSITE", _s(gid("pc-site")), "$", _s("Site"), "$", "$", "$", "$", "$",
        ".ELEMENT.", "$", "$", "$", "$", "$",
    )
    _aggregate(b, "pc-agg-ps", project, [site])
    building = b.entity(
        "IFCBUILDING", _s(gid("pc-building")), "$", _s("Building"), "$", "$", "$", "$",
        "$", ".ELEMENT.", "$", "$", "$",
    )
    _aggregate(b, "pc-agg-sb", site, [building])
    return project, building


def _pc_write(root: str, b: IfcBuilder, rows: list[dict], zone_root_gid: str) -> dict:
    os.makedirs(root, exist_ok=True)
    text = b.render(file_name="pc.ifc")
    model_path = os.path.join(root, "pc.ifc")
    with open(model_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    manifest = _write_manifest(
        root, [{"source_id": "pc", "path": "pc.ifc", "sha256": sha256_bytes(text.encode("utf-8"))}]
    )
    rules_path = os.path.join(root, "rules.json")
    with open(rules_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(
            {
                "rules_version": 1,
                "zones": [
                    {
                        "zone_id": "zone-pc",
                        "root": {"source_id": "pc", "global_id": zone_root_gid},
                        "device_types": ["IfcBuildingElementProxy"],
                        "matters": [
                            {"code": MATTER, "coverage": "device_and_space", "min_signatories": 2}
                        ],
                    }
                ],
            },
            fh,
            indent=2,
        )
        fh.write("\n")
    docs_dir = os.path.join(root, "docs")
    os.makedirs(docs_dir, exist_ok=True)
    for row in rows:
        doc = row.get("document")
        if doc:
            with open(os.path.join(docs_dir, doc["path"].split("/", 1)[1]), "wb") as fh:
                fh.write(b"pc document\n")
    ledger_path = os.path.join(root, "ledger.jsonl")
    with open(ledger_path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    checkpoints_path = os.path.join(root, "checkpoints.json")
    with open(checkpoints_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(
            {"checkpoints": [{"checkpoint_id": "pc-cp", "effective_at": PC_T, "known_at": PC_K1}]},
            fh,
            indent=2,
        )
        fh.write("\n")
    return {
        "manifest": manifest,
        "rules": rules_path,
        "ledger": ledger_path,
        "checkpoints": checkpoints_path,
    }


def _pc_doc(name: str) -> dict:
    return {"path": f"docs/{name}", "sha256": _sha(b"pc document\n")}


def build_identity_conflict_fixture(root: str) -> dict:
    """A space signoff whose target GlobalId is duplicated in the source.

    Room R is duplicated (a dangling second instance shares its GlobalId), so
    the signoff targeting ``(pc, roomR)`` cannot be confirmed: it becomes a
    pending signoff with the two conflicting rows as evidence.
    """

    b = IfcBuilder()
    _project, building = _pc_base(b)
    storey = _storey(b, "pc-storey", "Storey")
    _aggregate(b, "pc-agg-bs", building, [storey])
    room = _space(b, "pc-room", "Room")
    _aggregate(b, "pc-agg-sr", storey, [room])
    device = _device(b, "pc-device", "DV")
    _contain(b, "pc-cont", [device], room)
    # Dangling duplicate of the room: same GlobalId, not aggregated anywhere.
    _space(b, "pc-room", "Room (shadow)")
    rows = [
        _rev("SIG-PC-DEV", 1, PC_R1, PC_VALID, "accepted", PARTY_ALPHA, "device",
             "pc", gid("pc-device"), _pc_doc("dev.pdf")),
        _rev("SIG-PC-DUP", 1, PC_R1, PC_VALID, "accepted", PARTY_BETA, "space",
             "pc", gid("pc-room"), _pc_doc("dup.pdf")),
    ]
    return _pc_write(root, b, rows, gid("pc-storey"))


def build_candidate_path_fixture(root: str) -> dict:
    """A space signoff whose only path to the device uses invalid edges.

    S1 and S2 decompose each other (a cycle, so both edges are conflicts).
    The device is contained in S1; S2 is a zone member via a valid edge from
    the storey.  The signoff targeting S2 has a candidate path
    (root -> S2 member, device -> S1 -> S2 over a conflict edge) that cannot
    confirm coverage.
    """

    b = IfcBuilder()
    _project, building = _pc_base(b)
    storey = _storey(b, "pc-storey", "Storey")
    _aggregate(b, "pc-agg-bs", building, [storey])
    s1 = _space(b, "pc-s1", "S1")
    s2 = _space(b, "pc-s2", "S2")
    _aggregate(b, "pc-agg-s1", storey, [s1, s2])
    device = _device(b, "pc-device", "DV")
    _contain(b, "pc-cont", [device], s1)
    # The cycle: S1 decomposes S2 and S2 decomposes S1.
    _aggregate(b, "pc-agg-cycle-1", s1, [s2])
    _aggregate(b, "pc-agg-cycle-2", s2, [s1])
    rows = [
        _rev("SIG-PC-DEV", 1, PC_R1, PC_VALID, "accepted", PARTY_ALPHA, "device",
             "pc", gid("pc-device"), _pc_doc("dev.pdf")),
        _rev("SIG-PC-CAND", 1, PC_R1, PC_VALID, "accepted", PARTY_BETA, "space",
             "pc", gid("pc-s2"), _pc_doc("cand.pdf")),
    ]
    return _pc_write(root, b, rows, gid("pc-storey"))


# ---------------------------------------------------------------------------
# Scaled fixture (fixed seed; generated at run time, never committed)
# ---------------------------------------------------------------------------

SC_T = "2026-06-01T12:00:00Z"
SC_R1 = "2026-06-01T08:00:00Z"
SC_R2 = "2026-06-01T10:00:00Z"
SC_R3 = "2026-06-01T11:00:00Z"
SC_K1 = "2026-06-01T09:00:00Z"
SC_K2 = "2026-06-01T10:30:00Z"
SC_K3 = "2026-06-01T12:30:00Z"
SC_VALID = "2026-05-01T00:00:00Z"


def build_scaled_fixture(
    root: str,
    *,
    seed: int = 20261119,
    storeys: int = 4,
    rooms: int = 3,
    devices: int = 5,
) -> dict:
    """A deterministic many-device fixture for count and memory checks.

    Layout: one zone per storey; every room holds ``devices`` proxies.  The
    ledger gives every device a party-alpha device signoff (recorded R1) and
    every storey a party-beta space signoff (recorded R2); one device per room
    (chosen by the seeded RNG) has its alpha signoff withdrawn at R3.

    Closed-form expectations (returned as ``expected``):

    * cp1 (T, K1): every matter verified={alpha} -> blocked; all zones held.
    * cp2 (T, K2): every matter verified={alpha,beta} -> ready; all released.
    * cp3 (T, K3): one device per room loses alpha -> blocked; the rest ready;
      every zone held.
    """

    import random

    rng = random.Random(seed)
    os.makedirs(root, exist_ok=True)
    b = IfcBuilder()
    mm = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", ".MILLI.", ".METRE.")
    units = b.entity("IFCUNITASSIGNMENT", f"(#{mm})")
    project = b.entity(
        "IFCPROJECT", _s(gid("sc-project")), "$", _s("scaled"), "$", "$", "$", "$", "$",
        f"#{units}",
    )
    site = b.entity(
        "IFCSITE", _s(gid("sc-site")), "$", _s("Site"), "$", "$", "$", "$", "$",
        ".ELEMENT.", "$", "$", "$", "$", "$",
    )
    _aggregate(b, "sc-agg-ps", project, [site])
    building = b.entity(
        "IFCBUILDING", _s(gid("sc-building")), "$", _s("Building"), "$", "$", "$", "$",
        "$", ".ELEMENT.", "$", "$", "$",
    )
    _aggregate(b, "sc-agg-sb", site, [building])

    withdrawn: list[tuple[int, int, int]] = []
    for s in range(storeys):
        storey = _storey(b, f"sc-storey-{s}", f"Storey {s}")
        _aggregate(b, f"sc-agg-b-{s}", building, [storey])
        for r in range(rooms):
            room = _space(b, f"sc-room-{s}-{r}", f"Room {s}-{r}")
            _aggregate(b, f"sc-agg-{s}-{r}", storey, [room])
            room_devices = []
            for d in range(devices):
                room_devices.append(_device(b, f"sc-dev-{s}-{r}-{d}", f"D{s}-{r}-{d}"))
            _contain(b, f"sc-cont-{s}-{r}", room_devices, room)
            withdrawn.append((s, r, rng.randrange(devices)))
    text = b.render(file_name="scaled.ifc")
    model_path = os.path.join(root, "scaled.ifc")
    with open(model_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    manifest_path = _write_manifest(
        root,
        [{"source_id": "scaled", "path": "scaled.ifc", "sha256": sha256_bytes(text.encode("utf-8"))}],
    )

    # Rules: one zone per storey.
    zones = [
        {
            "zone_id": f"zone-s{s}",
            "root": {"source_id": "scaled", "global_id": gid(f"sc-storey-{s}")},
            "device_types": ["IfcBuildingElementProxy"],
            "matters": [{"code": MATTER, "coverage": "device_and_space", "min_signatories": 2}],
        }
        for s in range(storeys)
    ]
    rules_path = os.path.join(root, "rules.json")
    with open(rules_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"rules_version": 1, "zones": zones}, fh, indent=2)
        fh.write("\n")

    # Ledger + documents.
    docs_dir = os.path.join(root, "docs")
    os.makedirs(docs_dir, exist_ok=True)

    def write_doc(name: str) -> dict:
        data = f"signoff document {name}\n".encode("utf-8")
        with open(os.path.join(docs_dir, name), "wb") as fh:
            fh.write(data)
        return {"path": f"docs/{name}", "sha256": _sha(data)}

    rows: list[dict] = []
    for s in range(storeys):
        for r in range(rooms):
            for d in range(devices):
                doc = write_doc(f"dev-{s}-{r}-{d}.txt")
                rows.append(
                    _rev(
                        f"SIG-DEV-{s}-{r}-{d}", 1, SC_R1, SC_VALID, "accepted",
                        PARTY_ALPHA, "device", "scaled", gid(f"sc-dev-{s}-{r}-{d}"),
                        doc,
                    )
                )
    for s in range(storeys):
        doc = write_doc(f"floor-{s}.txt")
        rows.append(
            _rev(
                f"SIG-FLOOR-{s}", 1, SC_R2, SC_VALID, "accepted", PARTY_BETA,
                "space", "scaled", gid(f"sc-storey-{s}"), doc,
            )
        )
    for s, r, d in withdrawn:
        rows.append(
            _rev(
                f"SIG-DEV-{s}-{r}-{d}", 2, SC_R3, SC_VALID, "withdrawn",
                PARTY_ALPHA, "device", "scaled", gid(f"sc-dev-{s}-{r}-{d}"), None,
            )
        )
    ledger_path = os.path.join(root, "ledger.jsonl")
    with open(ledger_path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    checkpoints_path = os.path.join(root, "checkpoints.json")
    with open(checkpoints_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(
            {
                "checkpoints": [
                    {"checkpoint_id": "sc-cp1", "effective_at": SC_T, "known_at": SC_K1},
                    {"checkpoint_id": "sc-cp2", "effective_at": SC_T, "known_at": SC_K2},
                    {"checkpoint_id": "sc-cp3", "effective_at": SC_T, "known_at": SC_K3},
                ]
            },
            fh,
            indent=2,
        )
        fh.write("\n")

    total = storeys * rooms * devices
    blocked_cp3 = storeys * rooms
    expected = {
        "matters": total,
        "revisions": len(rows),
        "withdrawn": [f"SIG-DEV-{s}-{r}-{d}" for s, r, d in withdrawn],
        "cp1": {"blocked": total, "ready": 0, "undetermined": 0, "zone_decision": "held"},
        "cp2": {"blocked": 0, "ready": total, "undetermined": 0, "zone_decision": "released"},
        "cp3": {
            "blocked": blocked_cp3,
            "ready": total - blocked_cp3,
            "undetermined": 0,
            "zone_decision": "held",
        },
        "sample_device_gid": gid("sc-dev-0-0-0"),
        "sample_storey_gid": gid("sc-storey-0"),
    }
    return {
        "manifest": manifest_path,
        "rules": rules_path,
        "ledger": ledger_path,
        "checkpoints": checkpoints_path,
        "expected": expected,
        "storeys": storeys,
        "rooms": rooms,
        "devices": devices,
    }
