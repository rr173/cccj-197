"""Synthetic IFC4 fixtures for the handover drill.

Every value is written explicitly, with STEP entity numbering controlled, so
tests can compare business output against values that were authored by hand.
The fixtures are generated at test/run time; no large IFC file is committed.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import Iterable

import ifcopenshell.guid as ifc_guid


def gid(name: str) -> str:
    """Deterministic IFC GlobalId derived from a test label."""

    return ifc_guid.compress(str(uuid.uuid5(uuid.NAMESPACE_URL, "ifc-handover/" + name)))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class IfcBuilder:
    schema: str = "IFC4"

    def __post_init__(self) -> None:
        self.lines: list[str] = []
        self.next_id = 1

    def entity(self, type_name: str, *args: object) -> int:
        step_id = self.next_id
        self.next_id += 1
        self.lines.append(f"#{step_id}={type_name}({','.join(str(a) for a in args)});")
        return step_id

    def render(self, *, file_name: str = "fixture.ifc") -> str:
        header = [
            "ISO-10303-21;",
            "HEADER;",
            "FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');",
            f"FILE_NAME('{file_name}','2026-01-01T00:00:00',('drill'),('handover'),"
            "'ifc_handover','ifc_handover','');",
            f"FILE_SCHEMA(('{self.schema}'));",
            "ENDSEC;",
            "DATA;",
        ]
        return "\n".join(header + self.lines + ["ENDSEC;", "END-ISO-10303-21;", ""])

    def write(self, path: str, *, file_name: str | None = None) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        text = self.render(file_name=file_name or os.path.basename(path))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return text


def _s(value: str | None) -> str:
    if value is None:
        return "$"
    return "'" + value.replace("'", "''") + "'"


def _text(prefix: str) -> str:
    return _s(prefix)


def _typed(type_name: str, value: str | float) -> str:
    return f"{type_name}({value})"


@dataclass
class FixtureSet:
    """Paths and manifest for a generated fixture directory."""

    root: str
    files: dict[str, str]
    manifest_path: str

    def manifest_entries(self) -> list[dict[str, str]]:
        import json

        with open(self.manifest_path, encoding="utf-8") as fh:
            data = json.load(fh)
        entries = data["sources"] if isinstance(data, dict) else data
        return [
            {
                "source_id": e["source_id"],
                "path": os.path.join(self.root, e["path"]),
                "sha256": e["sha256"],
            }
            for e in entries
        ]


def _write_manifest(root: str, entries: Iterable[dict[str, str]]) -> str:
    import json

    manifest_path = os.path.join(root, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump({"sources": list(entries)}, fh, indent=2)
        fh.write("\n")
    return manifest_path


# ---------------------------------------------------------------------------
# Main drill fixture
# ---------------------------------------------------------------------------


def build_drill_fixture(root: str, *, order_offset: int = 0) -> FixtureSet:
    """Two rooms sharing a type, plus units, materials and conflict cases.

    Authored expectations (checked by tests, not by an evaluator):

    * Type wall ``IfcWallType`` carries Pset_WallCommon with:
      - ``Length`` = 1000 mm (no property unit -> project LENGTH unit mm)
      - ``AcousticRating`` = 55 dB
    * Equipment instances share ``EqType``:
      - ``EQ-A`` has an instance ``Length`` = 2 m -> 2 m in SI.
      - ``EQ-B`` has an instance ``Length`` explicitly null -> explicit_null,
        no fallback to the type's 1 m.
    * Rooms R1 and R2 share wall type ``WType``; only R1 is exported.
    * Material layer set has three ordered layers.
    * Mixed units: length mm project unit, area m2, volume m3, mass kg.
    """

    b = IfcBuilder()
    # -- units ---------------------------------------------------------
    mm = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", ".MILLI.", ".METRE.")
    m2 = b.entity("IFCSIUNIT", "*", ".AREAUNIT.", "$", ".SQUARE_METRE.")
    m3 = b.entity("IFCSIUNIT", "*", ".VOLUMEUNIT.", "$", ".CUBIC_METRE.")
    kg = b.entity("IFCSIUNIT", "*", ".MASSUNIT.", ".KILO.", ".GRAM.")
    metre = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", "$", ".METRE.")
    unit_assignment = b.entity("IFCUNITASSIGNMENT", f"(#{mm},#{m2},#{m3},#{kg})")

    # -- project / spatial --------------------------------------------
    project = b.entity(
        "IFCPROJECT", _s(gid("project")), "$", _s("Drill project"), "$", "$", "$", "$",
        "$", f"#{unit_assignment}",
    )
    site = b.entity(
        "IFCSITE", _s(gid("site")), "$", _s("Site"), "$", "$", "$", "$", "$",
        ".ELEMENT.", "$", "$", "$", "$", "$",
    )
    building = b.entity(
        "IFCBUILDING", _s(gid("building")), "$", _s("Building"), "$", "$", "$", "$", "$",
        ".ELEMENT.", "$", "$", "$",
    )
    storey = b.entity(
        "IFCBUILDINGSTOREY", _s(gid("storey")), "$", _s("Level 1"), "$", "$", "$", "$",
        "$", ".ELEMENT.", "$",
    )
    room1 = b.entity(
        "IFCSPACE", _s(gid("room-1")), "$", _s("Room 1"), "$", "$", "$", "$",
        _s("R1"), ".ELEMENT.", ".SPACE.", "$",
    )
    room2 = b.entity(
        "IFCSPACE", _s(gid("room-2")), "$", _s("Room 2"), "$", "$", "$", "$",
        _s("R2"), ".ELEMENT.", ".SPACE.", "$",
    )
    b.entity("IFCRELAGGREGATES", _s(gid("agg-project-site")), "$", "$", "$",
             f"#{project}", f"(#{site})")
    b.entity("IFCRELAGGREGATES", _s(gid("agg-site-building")), "$", "$", "$",
             f"#{site}", f"(#{building})")
    b.entity("IFCRELAGGREGATES", _s(gid("agg-building-storey")), "$", "$", "$",
             f"#{building}", f"(#{storey})")
    b.entity("IFCRELAGGREGATES", _s(gid("agg-storey-rooms")), "$", "$", "$",
             f"#{storey}", f"(#{room1},#{room2})")

    # -- types ---------------------------------------------------------
    fire_rating_a = b.entity("IFCPROPERTYSINGLEVALUE", _s("FireRating"), "$",
                             _typed("IFCLABEL", _s("F30")), "$")
    acoustic = b.entity("IFCPROPERTYSINGLEVALUE", _s("AcousticRating"), "$",
                        _typed("IFCLABEL", _s("55 dB")), "$")
    type_length = b.entity("IFCPROPERTYSINGLEVALUE", _s("Length"), "$",
                           _typed("IFCLENGTHMEASURE", "1000."), "$")
    type_pset = b.entity(
        "IFCPROPERTYSET", _s(gid("pset-type-wallcommon")), "$", _s("Pset_WallCommon"), "$",
        f"(#{type_length},#{fire_rating_a},#{acoustic})",
    )
    wall_type = b.entity(
        "IFCWALLTYPE", _s(gid("wall-type")), "$", _s("WType"), "$", "$", f"(#{type_pset})",
        "$", "$", "$", ".STANDARD.",
    )

    # A material layer set used by the type.
    concrete = b.entity("IFCMATERIAL", _s("Concrete"), "$", "$")
    insulation = b.entity("IFCMATERIAL", _s("Insulation"), "$", "$")
    plaster = b.entity("IFCMATERIAL", _s("Plaster"), "$", "$")
    layer1 = b.entity("IFCMATERIALLAYER", f"#{insulation}", "50.", "$", _s("Insulation"), "$", "$", "2")
    layer2 = b.entity("IFCMATERIALLAYER", f"#{concrete}", "200.", "$", _s("Concrete"), "$", "$", "1")
    layer3 = b.entity("IFCMATERIALLAYER", f"#{plaster}", "15.", "$", _s("Plaster"), "$", "$", "3")
    layer_set = b.entity("IFCMATERIALLAYERSET", f"(#{layer1},#{layer2},#{layer3})",
                         _s("Wall assembly"), "$")
    usage = b.entity("IFCMATERIALLAYERSETUSAGE", f"#{layer_set}", ".AXIS2.", ".POSITIVE.",
                     "0.", "$")
    b.entity("IFCRELASSOCIATESMATERIAL", _s(gid("rel-mat-walltype")), "$", "$", "$",
             f"(#{wall_type})", f"#{usage}")

    # -- walls in two rooms -------------------------------------------
    wall1 = b.entity(
        "IFCWALL", _s(gid("wall-1")), "$", _s("W1"), "$", "$", "$", "$", _s("W1"),
        ".SOLIDWALL.", ".NOTDEFINED.",
    )
    wall2 = b.entity(
        "IFCWALL", _s(gid("wall-2")), "$", _s("W2"), "$", "$", "$", "$", _s("W2"),
        ".SOLIDWALL.", ".NOTDEFINED.",
    )
    b.entity("IFCRELDEFINESBYTYPE", _s(gid("rel-type-walls")), "$", "$", "$",
             f"(#{wall1},#{wall2})", f"#{wall_type}")
    b.entity("IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid("rel-cont-room1")), "$", "$", "$",
             f"(#{wall1})", f"#{room1}")
    b.entity("IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid("rel-cont-room2")), "$", "$", "$",
             f"(#{wall2})", f"#{room2}")

    # -- equipment with instance overrides ----------------------------
    eq_type_length = b.entity("IFCPROPERTYSINGLEVALUE", _s("Length"), "$",
                              _typed("IFCLENGTHMEASURE", "1000."), "$")
    eq_type_pset = b.entity(
        "IFCPROPERTYSET", _s(gid("pset-type-eq")), "$", _s("Pset_EquipmentCommon"), "$",
        f"(#{eq_type_length})",
    )
    eq_type = b.entity(
        "IFCBUILDINGELEMENTPROXYTYPE", _s(gid("eq-type")), "$", _s("EqType"), "$", "$",
        f"(#{eq_type_pset})", "$", "$", "$", ".NOTDEFINED.",
    )
    equipment_a = b.entity(
        "IFCBUILDINGELEMENTPROXY", _s(gid("eq-a")), "$", _s("EQ-A"), "$", "$", "$", "$",
        _s("EQ-A"), ".NOTDEFINED.",
    )
    equipment_b = b.entity(
        "IFCBUILDINGELEMENTPROXY", _s(gid("eq-b")), "$", _s("EQ-B"), "$", "$", "$", "$",
        _s("EQ-B"), ".NOTDEFINED.",
    )
    b.entity("IFCRELDEFINESBYTYPE", _s(gid("rel-type-eq")), "$", "$", "$",
             f"(#{equipment_a},#{equipment_b})", f"#{eq_type}")
    b.entity("IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid("rel-cont-room1-eq")), "$", "$", "$",
             f"(#{equipment_a},#{equipment_b})", f"#{room1}")
    # 2 metres, stated against an explicit metre unit: the property unit wins
    # over the project's millimetre unit.
    length_2m = b.entity("IFCPROPERTYSINGLEVALUE", _s("Length"), "$",
                         _typed("IFCLENGTHMEASURE", "2."), f"#{metre}")
    length_null = b.entity("IFCPROPERTYSINGLEVALUE", _s("Length"), "$", "$", "$")
    inst_pset_a = b.entity(
        "IFCPROPERTYSET", _s(gid("pset-inst-eq-a")), "$", _s("Pset_EquipmentCommon"), "$",
        f"(#{length_2m})",
    )
    inst_pset_b = b.entity(
        "IFCPROPERTYSET", _s(gid("pset-inst-eq-b")), "$", _s("Pset_EquipmentCommon"), "$",
        f"(#{length_null})",
    )
    b.entity("IFCRELDEFINESBYPROPERTIES", _s(gid("rel-pset-eq-a")), "$", "$", "$",
             f"(#{equipment_a})", f"#{inst_pset_a}")
    b.entity("IFCRELDEFINESBYPROPERTIES", _s(gid("rel-pset-eq-b")), "$", "$", "$",
             f"(#{equipment_b})", f"#{inst_pset_b}")

    # -- conflicts, units and oddities --------------------------------
    # Type has a conflicting property pair; EQ-C overrides one identity only.
    conflict_a = b.entity("IFCPROPERTYSINGLEVALUE", _s("Load"), "$",
                          _typed("IFCMASSMEASURE", "10."), "$")
    conflict_b = b.entity("IFCPROPERTYSINGLEVALUE", _s("Load"), "$",
                          _typed("IFCMASSMEASURE", "20."), "$")
    other_prop = b.entity("IFCPROPERTYSINGLEVALUE", _s("Voltage"), "$",
                          _typed("IFCLABEL", _s("230V")), "$")
    conflict_pset = b.entity(
        "IFCPROPERTYSET", _s(gid("pset-conflict-type")), "$", _s("Pset_Loads"), "$",
        f"(#{conflict_a},#{conflict_b},#{other_prop})",
    )
    conflict_type = b.entity(
        "IFCBUILDINGELEMENTPROXYTYPE", _s(gid("conflict-type")), "$", _s("LoadType"), "$",
        "$", f"(#{conflict_pset})", "$", "$", "$", ".NOTDEFINED.",
    )
    equipment_c = b.entity(
        "IFCBUILDINGELEMENTPROXY", _s(gid("eq-c")), "$", _s("EQ-C"), "$", "$", "$", "$",
        _s("EQ-C"), ".NOTDEFINED.",
    )
    equipment_d = b.entity(
        "IFCBUILDINGELEMENTPROXY", _s(gid("eq-d")), "$", _s("EQ-D"), "$", "$", "$", "$",
        _s("EQ-D"), ".NOTDEFINED.",
    )
    b.entity("IFCRELDEFINESBYTYPE", _s(gid("rel-type-loads")), "$", "$", "$",
             f"(#{equipment_c},#{equipment_d})", f"#{conflict_type}")
    b.entity("IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid("rel-cont-room1-loads")), "$",
             "$", "$", f"(#{equipment_c},#{equipment_d})", f"#{room1}")
    load_override = b.entity("IFCPROPERTYSINGLEVALUE", _s("Load"), "$",
                             _typed("IFCMASSMEASURE", "30."), "$")
    override_pset = b.entity(
        "IFCPROPERTYSET", _s(gid("pset-loads-instance")), "$", _s("Pset_Loads"), "$",
        f"(#{load_override})",
    )
    b.entity("IFCRELDEFINESBYPROPERTIES", _s(gid("rel-pset-loads-c")), "$", "$", "$",
             f"(#{equipment_c})", f"#{override_pset}")

    # Unit variants: explicit property unit, prefix-power area, missing unit,
    # conversion unit, and a unit reference cycle.
    inch = b.entity("IFCMEASUREWITHUNIT", _typed("IFCLENGTHMEASURE", "0.0254"),
                    f"#{b.entity('IFCSIUNIT', '*', '.LENGTHUNIT.', '$', '.METRE.')}")
    inch_unit = b.entity("IFCCONVERSIONBASEDUNIT", "*", ".LENGTHUNIT.", _s("INCH"),
                         f"#{inch}")
    explicit_len = b.entity("IFCPROPERTYSINGLEVALUE", _s("Width"), "$",
                            _typed("IFCLENGTHMEASURE", "2."), f"#{inch_unit}")
    mm2 = b.entity("IFCSIUNIT", "*", ".AREAUNIT.", ".MILLI.", ".SQUARE_METRE.")
    area_value = b.entity("IFCPROPERTYSINGLEVALUE", _s("Coverage"), "$",
                          _typed("IFCAREAMEASURE", "3."), "$")
    area_explicit = b.entity("IFCPROPERTYSINGLEVALUE", _s("PatchArea"), "$",
                             _typed("IFCAREAMEASURE", "500000."), f"#{mm2}")
    dims_pset = b.entity(
        "IFCPROPERTYSET", _s(gid("pset-dimensions")), "$", _s("Pset_Dimensions"), "$",
        f"(#{explicit_len},#{area_value},#{area_explicit})",
    )
    b.entity("IFCRELDEFINESBYPROPERTIES", _s(gid("rel-pset-dims")), "$", "$", "$",
             f"(#{equipment_a})", f"#{dims_pset}")
    # Missing project unit: use an object in a second project with no units.
    project2 = b.entity(
        "IFCPROJECT", _s(gid("project-2")), "$", _s("Unitless project"), "$", "$", "$",
        "$", "$", "$",
    )
    building2 = b.entity(
        "IFCBUILDING", _s(gid("building-2")), "$", _s("Unitless building"), "$", "$", "$",
        "$", "$", ".ELEMENT.", "$", "$", "$",
    )
    b.entity("IFCRELAGGREGATES", _s(gid("agg-project2-building2")), "$", "$", "$",
             f"#{project2}", f"(#{building2})")
    missing_len = b.entity("IFCPROPERTYSINGLEVALUE", _s("Length"), "$",
                           _typed("IFCLENGTHMEASURE", "5."), "$")
    missing_pset = b.entity(
        "IFCPROPERTYSET", _s(gid("pset-missing-unit")), "$", _s("Pset_Unitless"), "$",
        f"(#{missing_len})",
    )
    unitless_equip = b.entity(
        "IFCBUILDINGELEMENTPROXY", _s(gid("eq-unitless")), "$", _s("EQ-Unitless"), "$",
        "$", "$", "$", _s("EQ-U"), ".NOTDEFINED.",
    )
    b.entity("IFCRELDEFINESBYPROPERTIES", _s(gid("rel-pset-unitless")), "$", "$", "$",
             f"(#{unitless_equip})", f"#{missing_pset}")
    b.entity("IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid("rel-cont-building2")), "$", "$",
             "$", f"(#{unitless_equip})", f"#{building2}")
    # Unit reference cycle: CYCLE_A -> MWU -> CYCLE_B -> MWU -> CYCLE_A.
    cycle_a = b.entity("IFCCONVERSIONBASEDUNIT", "*", ".LENGTHUNIT.", _s("CYCLE_A"), "$")
    cycle_b = b.entity("IFCCONVERSIONBASEDUNIT", "*", ".LENGTHUNIT.", _s("CYCLE_B"), "$")
    mwu_a = b.entity("IFCMEASUREWITHUNIT", _typed("IFCLENGTHMEASURE", "1."), f"#{cycle_b}")
    mwu_b = b.entity("IFCMEASUREWITHUNIT", _typed("IFCLENGTHMEASURE", "1."), f"#{cycle_a}")
    for index, line in enumerate(b.lines):
        if line.startswith(f"#{cycle_a}="):
            b.lines[index] = f"#{cycle_a}=IFCCONVERSIONBASEDUNIT(*,.LENGTHUNIT.,'CYCLE_A',#{mwu_a});"
        if line.startswith(f"#{cycle_b}="):
            b.lines[index] = f"#{cycle_b}=IFCCONVERSIONBASEDUNIT(*,.LENGTHUNIT.,'CYCLE_B',#{mwu_b});"
    cyclic_prop = b.entity("IFCPROPERTYSINGLEVALUE", _s("CyclicLength"), "$",
                           _typed("IFCLENGTHMEASURE", "4."), f"#{cycle_a}")
    cyclic_pset = b.entity(
        "IFCPROPERTYSET", _s(gid("pset-cyclic-unit")), "$", _s("Pset_Cyclic"), "$",
        f"(#{cyclic_prop})",
    )
    b.entity("IFCRELDEFINESBYPROPERTIES", _s(gid("rel-pset-cyclic")), "$", "$", "$",
             f"(#{equipment_d})", f"#{cyclic_pset}")

    # Dimension mismatch: an area unit applied to a length.
    mismatch = b.entity("IFCPROPERTYSINGLEVALUE", _s("BadLength"), "$",
                        _typed("IFCLENGTHMEASURE", "7."), f"#{m2}")
    mismatch_pset = b.entity(
        "IFCPROPERTYSET", _s(gid("pset-mismatch")), "$", _s("Pset_Mismatch"), "$",
        f"(#{mismatch})",
    )
    b.entity("IFCRELDEFINESBYPROPERTIES", _s(gid("rel-pset-mismatch")), "$", "$", "$",
             f"(#{equipment_d})", f"#{mismatch_pset}")

    # Same identity, different property kind (single vs enumerated) in one set.
    same_name_single = b.entity("IFCPROPERTYSINGLEVALUE", _s("Finish"), "$",
                                _typed("IFCLABEL", _s("Paint")), "$")
    same_name_enum = b.entity("IFCPROPERTYENUMERATEDVALUE", _s("Finish"), "$",
                              f"({_typed('IFCLABEL', _s('Paint'))},"
                              f"{_typed('IFCLABEL', _s('Tile'))})", "$")
    kind_pset = b.entity(
        "IFCPROPERTYSET", _s(gid("pset-kind")), "$", _s("Pset_Finish"), "$",
        f"(#{same_name_single},#{same_name_enum})",
    )
    b.entity("IFCRELDEFINESBYPROPERTIES", _s(gid("rel-pset-kind")), "$", "$", "$",
             f"(#{equipment_a})", f"#{kind_pset}")

    # External reference: recorded, never opened.
    document = b.entity("IFCDOCUMENTREFERENCE", _s("https://example.invalid/spec.pdf"),
                        _s("DOC-1"), _s("Spec"), "$", "$")
    b.entity("IFCRELASSOCIATESDOCUMENT", _s(gid("rel-doc")), "$", "$", "$",
             f"(#{equipment_a})", f"#{document}")

    # Forward reference: a relation defined before its related objects.
    # (Deliberately placed here: entity ids increase, but STEP order puts the
    # relation line before the referenced object lines in the reorder fixture.)
    # We rely on the reorder helper for this case.

    text = b.render(file_name="drill.ifc")
    path = os.path.join(root, "drill.ifc")
    os.makedirs(root, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    digest = sha256_bytes(text.encode("utf-8"))
    manifest = _write_manifest(
        root,
        [{"source_id": "drill", "path": "drill.ifc", "sha256": digest}],
    )
    return FixtureSet(root=root, files={"drill": path}, manifest_path=manifest)


def reorder_step(text: str, *, reverse_entities: bool = True) -> str:
    """Reorder DATA-section entity lines without changing ids or content.

    This keeps every reference and value identical while changing STEP
    ordinals and line coordinates.  Forward references become common when the
    order is reversed.
    """

    lines = text.splitlines()
    data_start = lines.index("DATA;")
    data_end = lines.index("ENDSEC;", data_start)
    body = lines[data_start + 1 : data_end]
    entity_lines = [line for line in body if line.startswith("#")]
    if reverse_entities:
        entity_lines = list(reversed(entity_lines))
    new_lines = lines[: data_start + 1] + entity_lines + lines[data_end:]
    return "\n".join(new_lines) + "\n"


def renumber_step(text: str, *, offset: int) -> str:
    """Shift every STEP id by a constant, preserving all references.

    Used to prove that STEP numbers are provenance and cannot affect business
    values or relations.
    """

    import re

    mapping: dict[int, int] = {}

    def replace(match: re.Match[str]) -> str:
        value = int(match.group(1))
        if value not in mapping:
            mapping[value] = value + offset
        return f"#{mapping[value]}"

    return re.sub(r"#(\d+)", replace, text)


def build_cycle_fixture(root: str) -> FixtureSet:
    """A source for the reorder test: spatial cycle, duplicate GUID, forward refs.

    Authored expectations:
    * Rooms ``A`` and ``B`` are aggregated under the storey and also under each
      other in both directions, forming a decomposition cycle.  Both are
      unusable conflict evidence.
    * ``D`` and ``E`` share one GlobalId: two distinct objects, both unusable.
    * Wall ``W`` has two identical ``Length`` declarations which merge into one
      property with two sources.
    * A dangling aggregate endpoint exercises missing-endpoint evidence.
    """

    b = IfcBuilder()
    mm = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", ".MILLI.", ".METRE.")
    units = b.entity("IFCUNITASSIGNMENT", f"(#{mm})")
    project = b.entity("IFCPROJECT", _s(gid("cycle-project")), "$", _s("P"), "$", "$",
                       "$", "$", "$", f"#{units}")
    site = b.entity("IFCSITE", _s(gid("cycle-site")), "$", _s("Site"), "$", "$", "$",
                    "$", "$", ".ELEMENT.", "$", "$", "$", "$", "$")
    building = b.entity("IFCBUILDING", _s(gid("cycle-building")), "$", _s("B"), "$", "$",
                        "$", "$", "$", ".ELEMENT.", "$", "$", "$")
    storey = b.entity("IFCBUILDINGSTOREY", _s(gid("cycle-storey")), "$", _s("S"), "$",
                      "$", "$", "$", "$", ".ELEMENT.", "$")
    room_a = b.entity("IFCSPACE", _s(gid("cycle-room-a")), "$", _s("A"), "$", "$", "$",
                      "$", _s("A"), ".ELEMENT.", ".SPACE.", "$")
    room_b = b.entity("IFCSPACE", _s(gid("cycle-room-b")), "$", _s("B"), "$", "$", "$",
                      "$", _s("B"), ".ELEMENT.", ".SPACE.", "$")
    room_d = b.entity("IFCSPACE", _s(gid("cycle-room-d")), "$", _s("D"), "$", "$", "$",
                      "$", _s("D"), ".ELEMENT.", ".SPACE.", "$")
    room_e = b.entity("IFCSPACE", _s(gid("cycle-room-d")), "$", _s("E-duplicate"), "$",
                      "$", "$", "$", _s("E"), ".ELEMENT.", ".SPACE.", "$")
    b.entity("IFCRELAGGREGATES", _s(gid("c1")), "$", "$", "$", f"#{project}", f"(#{site})")
    b.entity("IFCRELAGGREGATES", _s(gid("c2")), "$", "$", "$", f"#{site}", f"(#{building})")
    b.entity("IFCRELAGGREGATES", _s(gid("c3")), "$", "$", "$", f"#{building}",
             f"(#{storey})")
    b.entity("IFCRELAGGREGATES", _s(gid("c4")), "$", "$", "$", f"#{storey}",
             f"(#{room_a},#{room_b},#{room_d},#{room_e})")
    # Decomposition cycle: A contains B and B contains A.
    b.entity("IFCRELAGGREGATES", _s(gid("c5")), "$", "$", "$", f"#{room_a}",
             f"(#{room_b})")
    b.entity("IFCRELAGGREGATES", _s(gid("c6")), "$", "$", "$", f"#{room_b}",
             f"(#{room_a})")
    # A wall whose property set has two identical declarations (mergeable).
    length_1 = b.entity("IFCPROPERTYSINGLEVALUE", _s("Length"), "$",
                        _typed("IFCLENGTHMEASURE", "1000."), "$")
    length_2 = b.entity("IFCPROPERTYSINGLEVALUE", _s("Length"), "$",
                        _typed("IFCLENGTHMEASURE", "1000."), "$")
    fire_1 = b.entity("IFCPROPERTYSINGLEVALUE", _s("FireRating"), "$",
                      _typed("IFCLABEL", _s("F30")), "$")
    fire_2 = b.entity("IFCPROPERTYSINGLEVALUE", _s("FireRating"), "$",
                      _typed("IFCLABEL", _s("F90")), "$")
    pset = b.entity("IFCPROPERTYSET", _s(gid("cycle-pset")), "$", _s("Pset_Test"), "$",
                    f"(#{length_1},#{length_2},#{fire_1},#{fire_2})")
    wall = b.entity("IFCBUILDINGELEMENTPROXY", _s(gid("cycle-wall")), "$", _s("W"), "$",
                    "$", "$", "$", _s("W"), ".NOTDEFINED.")
    b.entity("IFCRELDEFINESBYPROPERTIES", _s(gid("cycle-rel-pset")), "$", "$", "$",
             f"(#{wall})", f"#{pset}")
    b.entity("IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid("cycle-cont")), "$", "$", "$",
             f"(#{wall})", f"#{storey}")
    # Dangling aggregate endpoint: #X does not exist anywhere in the file.
    b.entity("IFCRELAGGREGATES", _s(gid("cycle-dangling")), "$", "$", "$", f"#{storey}",
             f"(#{b.next_id + 5000})")
    text = b.render(file_name="cycle.ifc")
    path = os.path.join(root, "cycle.ifc")
    os.makedirs(root, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    digest = sha256_bytes(text.encode("utf-8"))
    manifest = _write_manifest(
        root, [{"source_id": "cycle", "path": "cycle.ifc", "sha256": digest}]
    )
    return FixtureSet(root=root, files={"cycle": path}, manifest_path=manifest)


def build_multi_fixture(root: str) -> FixtureSet:
    """Two independent sources with overlapping GlobalIds.

    The same GlobalId in two sources is two different objects; no cross-file
    merge may happen.
    """

    os.makedirs(root, exist_ok=True)
    entries = []
    for name in ("alpha", "beta"):
        b = IfcBuilder()
        mm = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", ".MILLI.", ".METRE.")
        units = b.entity("IFCUNITASSIGNMENT", f"(#{mm})")
        project = b.entity("IFCPROJECT", _s(gid("multi-project")), "$", _s(name), "$",
                           "$", "$", "$", "$", f"#{units}")
        site = b.entity("IFCSITE", _s(gid("multi-site")), "$", _s("Site"), "$", "$",
                        "$", "$", "$", ".ELEMENT.", "$", "$", "$", "$", "$")
        b.entity("IFCRELAGGREGATES", _s(gid(f"{name}-agg")), "$", "$", "$",
                 f"#{project}", f"(#{site})")
        wall = b.entity("IFCBUILDINGELEMENTPROXY", _s(gid("shared-wall")), "$",
                        _s(f"Wall {name}"), "$", "$", "$", "$", "$", ".NOTDEFINED.")
        length = b.entity("IFCPROPERTYSINGLEVALUE", _s("Length"), "$",
                          _typed("IFCLENGTHMEASURE", "1000." if name == "alpha" else "2000."),
                          "$")
        pset = b.entity("IFCPROPERTYSET", _s(gid(f"{name}-pset")), "$",
                        _s("Pset_Test"), "$", f"(#{length})")
        b.entity("IFCRELDEFINESBYPROPERTIES", _s(gid(f"{name}-rel")), "$", "$", "$",
                 f"(#{wall})", f"#{pset}")
        b.entity("IFCRELCONTAINEDINSPATIALSTRUCTURE", _s(gid(f"{name}-cont")), "$", "$",
                 "$", f"(#{wall})", f"#{site}")
        text = b.render(file_name=f"{name}.ifc")
        path = os.path.join(root, f"{name}.ifc")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        entries.append(
            {
                "source_id": name,
                "path": f"{name}.ifc",
                "sha256": sha256_bytes(text.encode("utf-8")),
            }
        )
    manifest = _write_manifest(root, entries)
    return FixtureSet(
        root=root,
        files={
            "alpha": os.path.join(root, "alpha.ifc"),
            "beta": os.path.join(root, "beta.ifc"),
        },
        manifest_path=manifest,
    )
