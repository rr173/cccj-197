"""Runtime-generated two-period IFC fixtures for cross-version diffing.

Two minimal IFC4 files are authored by hand (one "old" period, one "new"
period), ingested into two independent repositories.  Every scenario demanded
by the change-review requirements is represented deliberately:

* ``EQ-UNIT``     : 2 m rewritten as 2000 mm; business value unchanged.
* ``EQ-PROV``     : a property moves from a type declaration to an instance
                    declaration with the same effective value; provenance-only.
* ``EQ-NULL``     : an actual value is replaced by an explicit null.
* ``EQ-MOVED``    : the equipment moves from one room to another.
* ``WALL-MAT``    : material layer composition/order changes at named layers.
* ``EQ-MAPPED``   : same object, changed GlobalId, covered by an explicit
                    one-to-one mapping.
* ``EQ-UNMAPPED`` : changed GlobalId without a mapping -> removed + added,
                    never guessed.
* ``EQ-DUP``      : the GlobalId is duplicated in the new period -> incomparable.
* ``EQ-BROKEN``   : a spatial relation endpoint goes missing in the new period
                    -> the relation is locally incomparable.
* ``EQ-STABLE``   : only STEP entity numbers/line order change -> no change.

No large file is ever written or committed; every fixture is regenerated at
test time into a throw-away directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from fixtures import IfcBuilder, gid, sha256_bytes, _s, _typed, _write_manifest

# Labels used both to derive deterministic GlobalIds and to key the hand
# written expectation table.  Labels are never used as matching identity.
LABEL_UNIT = "diff-eq-unit"
LABEL_PROV = "diff-eq-prov"
LABEL_PROV_TYPE = "diff-type-prov"
LABEL_NULL = "diff-eq-null"
LABEL_MOVED = "diff-eq-moved"
LABEL_WALL_MAT = "diff-wall-mat"
LABEL_WALL_TYPE = "diff-wall-type"
LABEL_MAPPED_OLD = "diff-eq-mapped-old"
LABEL_MAPPED_NEW = "diff-eq-mapped-new"
LABEL_UNMAPPED_OLD = "diff-eq-unmapped-old"
LABEL_UNMAPPED_NEW = "diff-eq-unmapped-new"
LABEL_DUP = "diff-eq-dup"
LABEL_BROKEN = "diff-eq-broken"
LABEL_STABLE = "diff-eq-stable"
LABEL_VALUE = "diff-eq-value"

ROOM_A = "diff-room-a"
ROOM_B = "diff-room-b"
STOREY = "diff-storey"
BUILDING = "diff-building"
SITE = "diff-site"
PROJECT = "diff-project"

# Materials used so a layer swap is an order change, not just a rename.
MAT_CONCRETE = "Concrete-Core"
MAT_INSULATION = "Insulation-Layer"
MAT_PLASTER = "Plaster-Finish"
MAT_BRICK = "Brick-Backup"


@dataclass
class DiffFixture:
    root: str
    old_path: str
    new_path: str
    old_repo: str
    new_repo: str
    mapping_path: str
    expectations_path: str
    labels: dict[str, str]


def _add_units(b: IfcBuilder) -> dict[str, int]:
    mm = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", ".MILLI.", ".METRE.")
    metre = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", "$", ".METRE.")
    m2 = b.entity("IFCSIUNIT", "*", ".AREAUNIT.", "$", ".SQUARE_METRE.")
    m3 = b.entity("IFCSIUNIT", "*", ".VOLUMEUNIT.", "$", ".CUBIC_METRE.")
    kg = b.entity("IFCSIUNIT", "*", ".MASSUNIT.", ".KILO.", ".GRAM.")
    assignment = b.entity(
        "IFCUNITASSIGNMENT", f"(#{mm},#{m2},#{m3},#{kg})"
    )
    return {"mm": mm, "metre": metre, "assignment": assignment}


def _add_spatial(b: IfcBuilder, units: dict[str, int]) -> dict[str, int]:
    project = b.entity(
        "IFCPROJECT", _s(gid(PROJECT)), "$", _s("Diff project"), "$", "$", "$",
        "$", "$", f"#{units['assignment']}",
    )
    site = b.entity(
        "IFCSITE", _s(gid(SITE)), "$", _s("Site"), "$", "$", "$", "$", "$",
        ".ELEMENT.", "$", "$", "$", "$", "$",
    )
    building = b.entity(
        "IFCBUILDING", _s(gid(BUILDING)), "$", _s("Building"), "$", "$", "$",
        "$", "$", ".ELEMENT.", "$", "$", "$",
    )
    storey = b.entity(
        "IFCBUILDINGSTOREY", _s(gid(STOREY)), "$", _s("Level"), "$", "$", "$",
        "$", "$", ".ELEMENT.", "$",
    )
    room_a = b.entity(
        "IFCSPACE", _s(gid(ROOM_A)), "$", _s("Room A"), "$", "$", "$", "$",
        _s("RA"), ".ELEMENT.", ".SPACE.", "$",
    )
    room_b = b.entity(
        "IFCSPACE", _s(gid(ROOM_B)), "$", _s("Room B"), "$", "$", "$", "$",
        _s("RB"), ".ELEMENT.", ".SPACE.", "$",
    )
    b.entity("IFCRELAGGREGATES", _s(gid("diff-agg-1")), "$", "$", "$",
             f"#{project}", f"(#{site})")
    b.entity("IFCRELAGGREGATES", _s(gid("diff-agg-2")), "$", "$", "$",
             f"#{site}", f"(#{building})")
    b.entity("IFCRELAGGREGATES", _s(gid("diff-agg-3")), "$", "$", "$",
             f"#{building}", f"(#{storey})")
    b.entity("IFCRELAGGREGATES", _s(gid("diff-agg-4")), "$", "$", "$",
             f"#{storey}", f"(#{room_a},#{room_b})")
    return {
        "project": project,
        "site": site,
        "building": building,
        "storey": storey,
        "room_a": room_a,
        "room_b": room_b,
    }


def _equipment(b: IfcBuilder, label: str, name: str) -> int:
    return b.entity(
        "IFCBUILDINGELEMENTPROXY", _s(gid(label)), "$", _s(name), "$", "$",
        "$", "$", _s(name), ".NOTDEFINED.",
    )


def _single_value(b: IfcBuilder, name: str, typed: str, unit: int | None) -> int:
    return b.entity(
        "IFCPROPERTYSINGLEVALUE", _s(name), "$", typed,
        "$" if unit is None else f"#{unit}",
    )


def _pset(b: IfcBuilder, label: str, name: str, props: list[int]) -> int:
    return b.entity(
        "IFCPROPERTYSET", _s(gid(label)), "$", _s(name), "$",
        "(" + ",".join(f"#{p}" for p in props) + ")",
    )


def _defines_properties(b: IfcBuilder, label: str, objects: list[int], pset: int) -> int:
    return b.entity(
        "IFCRELDEFINESBYPROPERTIES", _s(gid(label)), "$", "$", "$",
        "(" + ",".join(f"#{o}" for o in objects) + ")", f"#{pset}",
    )


def _contains(b: IfcBuilder, label: str, objects: list[int], room: int) -> int:
    return b.entity(
        "IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid(label)), "$", "$", "$",
        "(" + ",".join(f"#{o}" for o in objects) + ")", f"#{room}",
    )


def _material_layers(
    b: IfcBuilder, ordered_materials: list[tuple[str, float, int]]
) -> int:
    """Build a layer set; ordered_materials is (material label, thickness, priority)."""

    material_ids: dict[str, int] = {}
    for label, _thickness, _priority in ordered_materials:
        if label not in material_ids:
            material_ids[label] = b.entity("IFCMATERIAL", _s(label), "$", "$")
    layer_ids: list[int] = []
    for ordinal, (label, thickness, priority) in enumerate(ordered_materials):
        thickness_text = str(int(thickness)) if float(thickness).is_integer() else str(thickness)
        layer_ids.append(
            b.entity(
                "IFCMATERIALLAYER",
                f"#{material_ids[label]}",
                thickness_text + ".",
                "$",
                _s(label),
                "$",
                "$",
                str(priority),
            )
        )
    layer_set = b.entity(
        "IFCMATERIALLAYERSET",
        "(" + ",".join(f"#{i}" for i in layer_ids) + ")",
        _s("Wall layers"),
        "$",
    )
    usage = b.entity(
        "IFCMATERIALLAYERSETUSAGE", f"#{layer_set}", ".AXIS2.", ".POSITIVE.",
        "0.", "$",
    )
    return usage


def _build_old(b: IfcBuilder) -> dict[str, int]:
    units = _add_units(b)
    spatial = _add_spatial(b, units)

    # EQ-UNIT: 2 metres via an explicit metre unit (project unit is mm).
    eq_unit = _equipment(b, LABEL_UNIT, "EQ-Unit")
    length_2m = _single_value(
        b, "Length", _typed("IFCLENGTHMEASURE", "2."), units["metre"]
    )
    pset_unit = _pset(b, "diff-pset-unit-old", "Pset_Dimensions", [length_2m])
    _defines_properties(b, "diff-rel-pset-unit-old", [eq_unit], pset_unit)

    # EQ-PROV: effective Rating value comes from the TYPE in the old period.
    # Type property sets are declared through HasPropertySets (the type's own
    # attribute), not through IfcRelDefinesByProperties.
    type_rating = _single_value(
        b, "Rating", _typed("IFCLABEL", _s("Gold")), None
    )
    type_pset = _pset(b, "diff-pset-prov-type", "Pset_Common", [type_rating])
    prov_type = b.entity(
        "IFCBUILDINGELEMENTPROXYTYPE", _s(gid(LABEL_PROV_TYPE)), "$",
        _s("ProvType"), "$", "$", f"(#{type_pset})", "$", "$", "$", ".NOTDEFINED.",
    )
    eq_prov = _equipment(b, LABEL_PROV, "EQ-Prov")
    b.entity(
        "IFCRELDEFINESBYTYPE", _s(gid("diff-rel-prov-type")), "$", "$", "$",
        f"(#{eq_prov})", f"#{prov_type}",
    )

    # EQ-NULL: an actual pressure value in the old period.
    eq_null = _equipment(b, LABEL_NULL, "EQ-Null")
    pressure = _single_value(
        b, "Pressure", _typed("IFCLABEL", _s("4 bar")), None
    )
    pset_null = _pset(b, "diff-pset-null-old", "Pset_Common", [pressure])
    _defines_properties(b, "diff-rel-pset-null-old", [eq_null], pset_null)

    # EQ-MOVED: starts in Room A.
    eq_moved = _equipment(b, LABEL_MOVED, "EQ-Moved")
    _contains(b, "diff-cont-moved-old", [eq_moved], spatial["room_a"])

    # WALL-MAT: old layer order insulation -> concrete -> plaster.
    wall_type = b.entity(
        "IFCWALLTYPE", _s(gid(LABEL_WALL_TYPE)), "$", _s("WallType"), "$",
        "$", "$", "$", "$", "$", ".STANDARD.",
    )
    old_usage = _material_layers(
        b,
        [
            (MAT_INSULATION, 50.0, 1),
            (MAT_CONCRETE, 200.0, 2),
            (MAT_PLASTER, 15.0, 3),
        ],
    )
    b.entity(
        "IFCRELASSOCIATESMATERIAL", _s(gid("diff-rel-mat-old")), "$", "$", "$",
        f"(#{wall_type})", f"#{old_usage}",
    )
    wall_mat = b.entity(
        "IFCWALL", _s(gid(LABEL_WALL_MAT)), "$", _s("Wall-Mat"), "$", "$",
        "$", "$", _s("Wall-Mat"), ".STANDARD.", ".NOTDEFINED.",
    )
    b.entity(
        "IFCRELDEFINESBYTYPE", _s(gid("diff-rel-wall-type-old")), "$", "$", "$",
        f"(#{wall_mat})", f"#{wall_type}",
    )

    # EQ-MAPPED: old GlobalId (mapping will bridge the new GlobalId).
    eq_mapped = _equipment(b, LABEL_MAPPED_OLD, "EQ-Mapped")
    # EQ-UNMAPPED: old GlobalId, deliberately left out of the mapping.
    eq_unmapped = _equipment(b, LABEL_UNMAPPED_OLD, "EQ-Unmapped")

    # EQ-DUP: single occurrence in the old period.
    eq_dup = _equipment(b, LABEL_DUP, "EQ-Dup")

    # EQ-BROKEN: healthy containment in the old period.
    eq_broken = _equipment(b, LABEL_BROKEN, "EQ-Broken")
    _contains(b, "diff-cont-broken-old", [eq_broken], spatial["room_a"])

    # EQ-STABLE: identical semantics, only STEP numbering differs later.
    eq_stable = _equipment(b, LABEL_STABLE, "EQ-Stable")
    stable_value = _single_value(
        b, "Serial", _typed("IFCLABEL", _s("SN-0001")), None
    )
    stable_pset = _pset(b, "diff-pset-stable", "Pset_Common", [stable_value])
    _defines_properties(b, "diff-rel-pset-stable-old", [eq_stable], stable_pset)

    # EQ-VALUE: a genuine numeric business value change (same project unit).
    eq_value = _equipment(b, LABEL_VALUE, "EQ-Value")
    old_weight = _single_value(
        b, "Weight", _typed("IFCMASSMEASURE", "10."), None
    )
    value_pset = _pset(b, "diff-pset-value-old", "Pset_Common", [old_weight])
    _defines_properties(b, "diff-rel-pset-value-old", [eq_value], value_pset)
    _contains(
        b,
        "diff-cont-base-old",
        [eq_unit, eq_null, eq_mapped, eq_unmapped, eq_dup, eq_stable, eq_value],
        spatial["room_a"],
    )
    # EQ-PROV also needs spatial containment.
    b.entity(
        "IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid("diff-cont-prov-old")),
        "$", "$", "$", f"(#{eq_prov})", f"#{spatial['room_a']}",
    )
    return {
        "spatial": spatial,
        "eq_unit": eq_unit,
        "eq_prov": eq_prov,
        "eq_null": eq_null,
        "eq_moved": eq_moved,
        "wall_mat": wall_mat,
        "eq_mapped": eq_mapped,
        "eq_unmapped": eq_unmapped,
        "eq_dup": eq_dup,
        "eq_broken": eq_broken,
        "eq_stable": eq_stable,
    }


def _build_new(b: IfcBuilder) -> dict[str, int]:
    units = _add_units(b)
    spatial = _add_spatial(b, units)

    # EQ-UNIT: 2000 mm against the project millimetre unit -> SI value still 2 m.
    eq_unit = _equipment(b, LABEL_UNIT, "EQ-Unit")
    length_2000mm = _single_value(
        b, "Length", _typed("IFCLENGTHMEASURE", "2000."), None
    )
    pset_unit = _pset(b, "diff-pset-unit-new", "Pset_Dimensions", [length_2000mm])
    _defines_properties(b, "diff-rel-pset-unit-new", [eq_unit], pset_unit)

    # EQ-PROV: same Rating value, now declared on the INSTANCE.
    prov_type = b.entity(
        "IFCBUILDINGELEMENTPROXYTYPE", _s(gid(LABEL_PROV_TYPE)), "$",
        _s("ProvType"), "$", "$", "$", "$", "$", "$", ".NOTDEFINED.",
    )
    eq_prov = _equipment(b, LABEL_PROV, "EQ-Prov")
    b.entity(
        "IFCRELDEFINESBYTYPE", _s(gid("diff-rel-prov-type-new")), "$", "$", "$",
        f"(#{eq_prov})", f"#{prov_type}",
    )
    inst_rating = _single_value(
        b, "Rating", _typed("IFCLABEL", _s("Gold")), None
    )
    inst_pset = _pset(b, "diff-pset-prov-inst", "Pset_Common", [inst_rating])
    _defines_properties(b, "diff-rel-pset-prov-inst", [eq_prov], inst_pset)

    # EQ-NULL: the actual value is replaced by an explicit null.
    eq_null = _equipment(b, LABEL_NULL, "EQ-Null")
    null_prop = b.entity("IFCPROPERTYSINGLEVALUE", _s("Pressure"), "$", "$", "$")
    pset_null = _pset(b, "diff-pset-null-new", "Pset_Common", [null_prop])
    _defines_properties(b, "diff-rel-pset-null-new", [eq_null], pset_null)

    # EQ-MOVED: now in Room B.
    eq_moved = _equipment(b, LABEL_MOVED, "EQ-Moved")
    _contains(b, "diff-cont-moved-new", [eq_moved], spatial["room_b"])

    # WALL-MAT: layer order and composition change (plaster dropped, brick added;
    # concrete and insulation swap positions).
    wall_type = b.entity(
        "IFCWALLTYPE", _s(gid(LABEL_WALL_TYPE)), "$", _s("WallType"), "$",
        "$", "$", "$", "$", "$", ".STANDARD.",
    )
    new_usage = _material_layers(
        b,
        [
            (MAT_CONCRETE, 200.0, 1),
            (MAT_BRICK, 100.0, 2),
            (MAT_INSULATION, 50.0, 3),
        ],
    )
    b.entity(
        "IFCRELASSOCIATESMATERIAL", _s(gid("diff-rel-mat-new")), "$", "$", "$",
        f"(#{wall_type})", f"#{new_usage}",
    )
    wall_mat = b.entity(
        "IFCWALL", _s(gid(LABEL_WALL_MAT)), "$", _s("Wall-Mat"), "$", "$",
        "$", "$", _s("Wall-Mat"), ".STANDARD.", ".NOTDEFINED.",
    )
    b.entity(
        "IFCRELDEFINESBYTYPE", _s(gid("diff-rel-wall-type-new")), "$", "$", "$",
        f"(#{wall_mat})", f"#{wall_type}",
    )

    # EQ-MAPPED: new GlobalId, bridged by the explicit mapping.
    eq_mapped = _equipment(b, LABEL_MAPPED_NEW, "EQ-Mapped")
    # EQ-UNMAPPED: new GlobalId with no mapping -> removed/added, never guessed.
    eq_unmapped = _equipment(b, LABEL_UNMAPPED_NEW, "EQ-Unmapped")

    # EQ-DUP: the GlobalId now occurs twice -> the identity is unusable.
    eq_dup_a = b.entity(
        "IFCBUILDINGELEMENTPROXY", _s(gid(LABEL_DUP)), "$", _s("EQ-Dup-A"),
        "$", "$", "$", "$", _s("DUP-A"), ".NOTDEFINED.",
    )
    eq_dup_b = b.entity(
        "IFCBUILDINGELEMENTPROXY", _s(gid(LABEL_DUP)), "$", _s("EQ-Dup-B"),
        "$", "$", "$", "$", _s("DUP-B"), ".NOTDEFINED.",
    )

    # EQ-BROKEN: the containment relation points at a STEP id that does not
    # exist, so the endpoint is missing on the new side.
    eq_broken = _equipment(b, LABEL_BROKEN, "EQ-Broken")
    dangling_room = b.next_id + 9000
    b.entity(
        "IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid("diff-cont-broken-new")),
        "$", "$", "$", f"(#{eq_broken})", f"#{dangling_room}",
    )

    # EQ-STABLE: unchanged semantics.
    eq_stable = _equipment(b, LABEL_STABLE, "EQ-Stable")
    stable_value = _single_value(
        b, "Serial", _typed("IFCLABEL", _s("SN-0001")), None
    )
    stable_pset = _pset(b, "diff-pset-stable-new", "Pset_Common", [stable_value])
    _defines_properties(b, "diff-rel-pset-stable-new", [eq_stable], stable_pset)

    # EQ-VALUE: the mass genuinely changes from 10 kg to 12 kg.
    eq_value = _equipment(b, LABEL_VALUE, "EQ-Value")
    new_weight = _single_value(
        b, "Weight", _typed("IFCMASSMEASURE", "12."), None
    )
    value_pset = _pset(b, "diff-pset-value-new", "Pset_Common", [new_weight])
    _defines_properties(b, "diff-rel-pset-value-new", [eq_value], value_pset)
    _contains(
        b,
        "diff-cont-base-new",
        [eq_unit, eq_null, eq_unmapped, eq_mapped, eq_dup_a, eq_dup_b, eq_stable,
         eq_value],
        spatial["room_a"],
    )
    b.entity(
        "IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid("diff-cont-prov-new")),
        "$", "$", "$", f"(#{eq_prov})", f"#{spatial['room_a']}",
    )
    return {
        "spatial": spatial,
        "eq_unit": eq_unit,
        "eq_prov": eq_prov,
        "eq_null": eq_null,
        "eq_moved": eq_moved,
        "wall_mat": wall_mat,
        "eq_mapped": eq_mapped,
        "eq_unmapped": eq_unmapped,
        "eq_dup_a": eq_dup_a,
        "eq_dup_b": eq_dup_b,
        "eq_broken": eq_broken,
        "eq_stable": eq_stable,
    }


def _render(b: IfcBuilder, file_name: str) -> str:
    return b.render(file_name=file_name)


def build_diff_fixture(root: str) -> DiffFixture:
    """Generate both periods, manifests, mapping and the hand-written table."""

    os.makedirs(root, exist_ok=True)
    old_builder = IfcBuilder()
    _build_old(old_builder)
    new_builder = IfcBuilder()
    _build_new(new_builder)

    old_text = _render(old_builder, "period-old.ifc")
    new_text = _render(new_builder, "period-new.ifc")
    old_path = os.path.join(root, "period-old.ifc")
    new_path = os.path.join(root, "period-new.ifc")
    for path, text in ((old_path, old_text), (new_path, new_text)):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    old_sha = sha256_bytes(old_text.encode("utf-8"))
    new_sha = sha256_bytes(new_text.encode("utf-8"))
    _write_manifest(
        root,
        [
            {"source_id": "period-old", "path": "period-old.ifc", "sha256": old_sha},
            {"source_id": "period-new", "path": "period-new.ifc", "sha256": new_sha},
        ],
    )
    mapping = {
        "mappings": [
            {
                "left": {"source_id": "period-old", "global_id": gid(LABEL_MAPPED_OLD)},
                "right": {"source_id": "period-new", "global_id": gid(LABEL_MAPPED_NEW)},
                "rationale": (
                    "equipment was re-issued a GlobalId during handover but the "
                    "asset tag EQ-Mapped confirms continuity"
                ),
            }
        ]
    }
    mapping_path = os.path.join(root, "identity-map.json")
    with open(mapping_path, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=2)
        fh.write("\n")

    expectations = handwritten_expectations()
    expectations_path = os.path.join(root, "expected-changes.json")
    with open(expectations_path, "w", encoding="utf-8") as fh:
        json.dump(expectations, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    return DiffFixture(
        root=root,
        old_path=old_path,
        new_path=new_path,
        old_repo=os.path.join(root, "old.sqlite"),
        new_repo=os.path.join(root, "new.sqlite"),
        mapping_path=mapping_path,
        expectations_path=expectations_path,
        labels={
            "unit": gid(LABEL_UNIT),
            "prov": gid(LABEL_PROV),
            "null": gid(LABEL_NULL),
            "moved": gid(LABEL_MOVED),
            "wall_mat": gid(LABEL_WALL_MAT),
            "mapped_old": gid(LABEL_MAPPED_OLD),
            "mapped_new": gid(LABEL_MAPPED_NEW),
            "unmapped_old": gid(LABEL_UNMAPPED_OLD),
            "unmapped_new": gid(LABEL_UNMAPPED_NEW),
            "dup": gid(LABEL_DUP),
            "broken": gid(LABEL_BROKEN),
            "stable": gid(LABEL_STABLE),
            "value": gid(LABEL_VALUE),
            "room_a": gid(ROOM_A),
            "room_b": gid(ROOM_B),
        },
    )


def handwritten_expectations() -> dict[str, Any]:
    """The manually authored expected change table, keyed by stable scenario."""

    return {
        "scenarios": [
            {
                "scenario": "unit_rewrite_no_business_change",
                "object_label": LABEL_UNIT,
                "expected_records": [],
                "note": "2 m vs 2000 mm normalise to the same SI value; no record",
            },
            {
                "scenario": "type_to_instance_provenance_only",
                "object_label": LABEL_PROV,
                "expected_records": [
                    {
                        "category": "provenance_change",
                        "aspect": "property",
                        "property": "Pset_Common\x1fRating\x1fIfcPropertySingleValue",
                        "before_scope": "type",
                        "after_scope": "instance",
                        "value_unchanged": True,
                    }
                ],
            },
            {
                "scenario": "explicit_null_replaces_value",
                "object_label": LABEL_NULL,
                "expected_records": [
                    {
                        "category": "semantic_change",
                        "aspect": "property",
                        "property": "Pset_Common\x1fPressure\x1fIfcPropertySingleValue",
                        "before_state": "value",
                        "after_state": "explicit_null",
                    }
                ],
            },
            {
                "scenario": "room_move",
                "object_label": LABEL_MOVED,
                "expected_records": [
                    {
                        "category": "semantic_change",
                        "aspect": "spatial_relation",
                        "from_room_label": ROOM_A,
                        "to_room_label": ROOM_B,
                    },
                    {
                        "category": "semantic_change",
                        "aspect": "spatial_relation",
                        "from_room_label": ROOM_A,
                        "to_room_label": ROOM_B,
                        "note": "one parent_removed edge and one parent_added edge",
                    },
                ],
            },
            {
                "scenario": "material_layer_change",
                "object_label": LABEL_WALL_MAT,
                "expected_records": [
                    {
                        "category": "semantic_change",
                        "aspect": "material",
                        "old_layers": [
                            MAT_INSULATION,
                            MAT_CONCRETE,
                            MAT_PLASTER,
                        ],
                        "new_layers": [MAT_CONCRETE, MAT_BRICK, MAT_INSULATION],
                    }
                ],
            },
            {
                "scenario": "mapped_global_id_change",
                "object_label_old": LABEL_MAPPED_OLD,
                "object_label_new": LABEL_MAPPED_NEW,
                "expected_records": [],
                "note": "mapping bridges the GlobalId; semantics are unchanged",
            },
            {
                "scenario": "unmapped_global_id_change",
                "object_label_old": LABEL_UNMAPPED_OLD,
                "object_label_new": LABEL_UNMAPPED_NEW,
                "expected_records": [
                    {"category": "removed", "aspect": "identity"},
                    {"category": "added", "aspect": "identity"},
                ],
                "note": "no mapping and no GlobalId match; never auto-paired",
            },
            {
                "scenario": "duplicate_global_id_incomparable",
                "object_label": LABEL_DUP,
                "expected_records": [
                    {"category": "incomparable", "aspect": "identity"}
                ],
            },
            {
                "scenario": "missing_relation_endpoint_incomparable",
                "object_label": LABEL_BROKEN,
                "expected_records": [
                    {"category": "incomparable", "aspect": "spatial_relation"}
                ],
                "note": "properties remain comparable; only the spatial edge is blocked",
            },
            {
                "scenario": "step_reorder_no_business_change",
                "object_label": LABEL_STABLE,
                "expected_records": [],
            },
            {
                "scenario": "genuine_numeric_value_change",
                "object_label": LABEL_VALUE,
                "expected_records": [
                    {
                        "category": "semantic_change",
                        "aspect": "property",
                        "property": "Pset_Common\x1fWeight\x1fIfcPropertySingleValue",
                        "before_si_value": {"numerator": "10", "denominator": "1"},
                        "after_si_value": {"numerator": "12", "denominator": "1"},
                    }
                ],
                "note": "same unit and dimension, but the SI business value differs",
            },
        ]
    }
