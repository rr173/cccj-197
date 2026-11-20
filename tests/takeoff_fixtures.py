"""Runtime-generated fixtures for the spatial quantity take-off drill.

The scenario is authored by hand (every value is written explicitly) and the
expected table lives in ``docs/takeoff-drill-expectations.md``; nothing here
is computed from geometry.  All files are regenerated at test time, so no
large IFC file is committed.

Covered phenomena, one per constructed object/declaration:

* two distinct spatial roots (storey A, room B1 under A) and an object
  reached through repeated edges that must count once per rule;
* a type quantity inherited by instances, then overridden by an instance;
* an explicit instance zero that is a real value, not a missing declaration;
* an instance same-identity conflict (no value picked, evidence retained);
* same quantity name with different declared types (never merged);
* a missing identity, an unsupported quantity subtype and a unit cycle;
* explicit units, project units, prefix conversion and a dimension mismatch;
* a broken containment edge, a root that cannot be resolved and an empty scope;
* an object without a GlobalId (source-local STEP identity retained for the
  anomaly, never merged by STEP number).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from fixtures import IfcBuilder, gid, sha256_bytes, _s, _write_manifest

SOURCE = "to-model"

# Spatial labels.
SITE = "to-site"
BUILDING = "to-building"
STOREY_A = "to-storey-a"
STOREY_C = "to-storey-c"
ROOM_B1 = "to-room-b1"
ROOM_B2 = "to-room-b2"
ROOM_D = "to-room-d"

# Device labels.
PUMP_TYPE = "to-pump-type"
P1 = "to-pump-1"
P2 = "to-pump-2"
P3 = "to-pump-3"
P4 = "to-pump-4"
P5 = "to-pump-5"
P6 = "to-pump-6"
V1 = "to-valve-1"
V2 = "to-valve-2"
V3 = "to-valve-3"
NO_GID = None  # the GlobalId-less object gets a deterministic label in text

# Rule ids.
RULE_A = "rule-storey-a"
RULE_A_VALVES = "rule-storey-a-valves"
RULE_B = "rule-room-b1"
RULE_B_VALVES = "rule-room-b1-valves"
RULE_A_DUP = "rule-storey-a-duplicate"
RULE_MISSING_ROOT = "rule-missing-root"
RULE_EMPTY = "rule-storey-c"
RULE_ROOM_D = "rule-room-d"

# Quantity set / identity names.
QTO_PUMP = "Qto_PumpBaseQuantities"
QTO_VALVE = "Qto_ValveBaseQuantities"
QNAME_COUNT = "Count"
QNAME_LENGTH = "TotalLength"
QNAME_AREA = "NetArea"
QNAME_GROSS_AREA = "GrossArea"
QNAME_BORE = "BoreLength"
QNAME_GAUGE = "GaugeLength"


@dataclass
class TakeoffFixture:
    root: str
    manifest: str
    rules: str
    ifc_path: str


# ---------------------------------------------------------------------------
# Low-level builders
# ---------------------------------------------------------------------------


def _qcount(b: IfcBuilder, name: str, value: str | None, unit: int | None = None) -> int:
    # IfcQuantityCount(Name, Description, Unit, CountValue, Formula)
    return b.entity(
        "IFCQUANTITYCOUNT",
        _s(name),
        "$",
        f"#{unit}" if unit is not None else "$",
        "$" if value is None else f"IFCCOUNTMEASURE({value})",
        "$",
    )


def _qlength(
    b: IfcBuilder, name: str, value: str | None, unit: int | None = None
) -> int:
    # IfcQuantityLength(Name, Description, Unit, LengthValue, Formula)
    return b.entity(
        "IFCQUANTITYLENGTH",
        _s(name),
        "$",
        f"#{unit}" if unit is not None else "$",
        "$" if value is None else f"IFCLENGTHMEASURE({value})",
        "$",
    )


def _qarea(b: IfcBuilder, name: str, value: str | None, unit: int | None = None) -> int:
    return b.entity(
        "IFCQUANTITYAREA",
        _s(name),
        "$",
        f"#{unit}" if unit is not None else "$",
        "$" if value is None else f"IFCAREAMEASURE({value})",
        "$",
    )


def _qweight(b: IfcBuilder, name: str, value: str, unit: int | None = None) -> int:
    return b.entity(
        "IFCQUANTITYWEIGHT",
        _s(name),
        "$",
        f"#{unit}" if unit is not None else "$",
        f"IFCWEIGHTMEASURE({value})",
        "$",
    )


def _qvolume(b: IfcBuilder, name: str, value: str, unit: int | None = None) -> int:
    return b.entity(
        "IFCQUANTITYVOLUME",
        _s(name),
        "$",
        f"#{unit}" if unit is not None else "$",
        f"IFCVOLUMEMEASURE({value})",
        "$",
    )


def _element_quantity(b: IfcBuilder, name: str, quantities: list[int]) -> int:
    # IfcElementQuantity(GlobalId, OwnerHistory, Name, Description,
    #                    MethodOfMeasurement, Quantities)
    return b.entity(
        "IFCELEMENTQUANTITY",
        _s(gid(f"qset-{name}-{b.next_id}")),
        "$",
        _s(name),
        "$",
        _s("hand-authored drill"),
        "(" + ",".join(f"#{q}" for q in quantities) + ")",
    )


def _defines_by_properties(b: IfcBuilder, label: str, objects: list[int], qset: int) -> int:
    return b.entity(
        "IFCRELDEFINESBYPROPERTIES",
        _s(gid(label)),
        "$",
        "$",
        "$",
        "(" + ",".join(f"#{o}" for o in objects) + ")",
        f"#{qset}",
    )


def _contain(b: IfcBuilder, label: str, elements: list[int], structure: int) -> int:
    return b.entity(
        "IFCRELCONTAINEDINSPATIALSTRUCTURE",
        _s(gid(label)),
        "$",
        "$",
        "$",
        "(" + ",".join(f"#{e}" for e in elements) + ")",
        f"#{structure}",
    )


def _aggregate(b: IfcBuilder, label: str, parent: int, children: list[int]) -> int:
    return b.entity(
        "IFCRELAGGREGATES",
        _s(gid(label)),
        "$",
        "$",
        "$",
        f"#{parent}",
        "(" + ",".join(f"#{c}" for c in children) + ")",
    )


def _pump(b: IfcBuilder, label: str | None, name: str) -> int:
    # IfcPump -> IfcFlowTerminal -> IfcDistributionElement -> IfcElement ->
    # IfcRoot: nine all-attributes arguments in IFC4.
    return b.entity(
        "IFCPUMP",
        _s(gid(label)) if label is not None else "$",
        "$",
        _s(name),
        "$",
        "$",
        "$",
        _s(name),
        ".NOTDEFINED.",
        "$",
    )


def _valve(b: IfcBuilder, label: str | None, name: str) -> int:
    # IfcValve shares the IfcFlowTerminal nine-argument shape.
    return b.entity(
        "IFCVALVE",
        _s(gid(label)) if label is not None else "$",
        "$",
        _s(name),
        "$",
        "$",
        "$",
        _s(name),
        ".NOTDEFINED.",
        "$",
    )


# ---------------------------------------------------------------------------
# Full drill model
# ---------------------------------------------------------------------------


def build_drill_model(root: str) -> TakeoffFixture:
    os.makedirs(root, exist_ok=True)
    b = IfcBuilder()

    # -- units -----------------------------------------------------------
    mm = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", ".MILLI.", ".METRE.")
    metre = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", "$", ".METRE.")
    m2 = b.entity("IFCSIUNIT", "*", ".AREAUNIT.", "$", ".SQUARE_METRE.")
    mm2 = b.entity("IFCSIUNIT", "*", ".AREAUNIT.", ".MILLI.", ".SQUARE_METRE.")
    m3 = b.entity("IFCSIUNIT", "*", ".VOLUMEUNIT.", "$", ".CUBIC_METRE.")
    kg = b.entity("IFCSIUNIT", "*", ".MASSUNIT.", ".KILO.", ".GRAM.")
    # A conversion-based length unit: 1 "foot-ish" = 0.3048 m exactly, no
    # offset.  Prefix/conversion exercise without an offset unit.
    mwu = b.entity(
        "IFCMEASUREWITHUNIT", "IFCLENGTHMEASURE(0.3048)", f"#{metre}"
    )
    foot = b.entity(
        "IFCCONVERSIONBASEDUNIT",
        "*",
        ".LENGTHUNIT.",
        _s("DRILL_FOOT"),
        f"#{mwu}",
    )
    # A conversion factor expressed as a fraction: 1 unit = 1/4 m exactly.
    mwu_quarter = b.entity(
        "IFCMEASUREWITHUNIT", "IFCLENGTHMEASURE(0.25)", f"#{metre}"
    )
    quarter = b.entity(
        "IFCCONVERSIONBASEDUNIT",
        "*",
        ".LENGTHUNIT.",
        _s("DRILL_QUARTER_METRE"),
        f"#{mwu_quarter}",
    )
    # Unit reference cycle: CYCLE_A -> MWU -> CYCLE_B -> MWU -> CYCLE_A.
    cyc_a = b.entity(
        "IFCCONVERSIONBASEDUNIT",
        "*",
        ".LENGTHUNIT.",
        _s("DRILL_CYCLE_A"),
        "$",
    )
    cyc_b = b.entity(
        "IFCCONVERSIONBASEDUNIT",
        "*",
        ".LENGTHUNIT.",
        _s("DRILL_CYCLE_B"),
        "$",
    )
    mwu_a = b.entity(
        "IFCMEASUREWITHUNIT", "IFCLENGTHMEASURE(1.)", f"#{cyc_b}"
    )
    mwu_b = b.entity(
        "IFCMEASUREWITHUNIT", "IFCLENGTHMEASURE(1.)", f"#{cyc_a}"
    )
    u_a, u_b = cyc_a, cyc_b
    for index, line in enumerate(b.lines):
        if line.startswith(f"#{cyc_a}="):
            b.lines[index] = (
                f"#{cyc_a}=IFCCONVERSIONBASEDUNIT(*,.LENGTHUNIT.,"
                f"'DRILL_CYCLE_A',#{mwu_a});"
            )
        if line.startswith(f"#{cyc_b}="):
            b.lines[index] = (
                f"#{cyc_b}=IFCCONVERSIONBASEDUNIT(*,.LENGTHUNIT.,"
                f"'DRILL_CYCLE_B',#{mwu_b});"
            )
    unit_assignment = b.entity(
        "IFCUNITASSIGNMENT", f"(#{mm},#{m2},#{m3},#{kg})"
    )

    # -- spatial structure ----------------------------------------------
    project = b.entity(
        "IFCPROJECT",
        _s(gid("to-project")),
        "$",
        _s("Take-off drill project"),
        "$",
        "$",
        "$",
        "$",
        "$",
        f"#{unit_assignment}",
    )
    site = b.entity(
        "IFCSITE",
        _s(gid(SITE)),
        "$",
        _s("Site"),
        "$",
        "$",
        "$",
        "$",
        "$",
        ".ELEMENT.",
        "$",
        "$",
        "$",
        "$",
        "$",
    )
    building = b.entity(
        "IFCBUILDING",
        _s(gid(BUILDING)),
        "$",
        _s("Building"),
        "$",
        "$",
        "$",
        "$",
        "$",
        ".ELEMENT.",
        "$",
        "$",
        "$",
    )
    storey_a = b.entity(
        "IFCBUILDINGSTOREY",
        _s(gid(STOREY_A)),
        "$",
        _s("Storey A"),
        "$",
        "$",
        "$",
        "$",
        "$",
        ".ELEMENT.",
        "$",
    )
    storey_c = b.entity(
        "IFCBUILDINGSTOREY",
        _s(gid(STOREY_C)),
        "$",
        _s("Storey C (empty)"),
        "$",
        "$",
        "$",
        "$",
        "$",
        ".ELEMENT.",
        "$",
    )
    room_b1 = b.entity(
        "IFCSPACE",
        _s(gid(ROOM_B1)),
        "$",
        _s("Room B1"),
        "$",
        "$",
        "$",
        "$",
        _s("B1"),
        ".ELEMENT.",
        ".SPACE.",
        "$",
    )
    room_b2 = b.entity(
        "IFCSPACE",
        _s(gid(ROOM_B2)),
        "$",
        _s("Room B2"),
        "$",
        "$",
        "$",
        "$",
        _s("B2"),
        ".ELEMENT.",
        ".SPACE.",
        "$",
    )
    room_d = b.entity(
        "IFCSPACE",
        _s(gid(ROOM_D)),
        "$",
        _s("Room D"),
        "$",
        "$",
        "$",
        "$",
        _s("D"),
        ".ELEMENT.",
        ".SPACE.",
        "$",
    )
    _aggregate(b, "to-agg-ps", project, [site])
    _aggregate(b, "to-agg-sb", site, [building])
    _aggregate(b, "to-agg-b", building, [storey_a, storey_c, room_d])
    _aggregate(b, "to-agg-a", storey_a, [room_b1, room_b2])

    # -- type with type-scope quantities --------------------------------
    type_count = _qcount(b, QNAME_COUNT, "4.")
    type_length = _qlength(b, QNAME_LENGTH, "1000.")  # project mm -> 1 m
    type_qset = _element_quantity(b, QTO_PUMP, [type_count, type_length])
    pump_type = b.entity(
        # IfcPumpType(GlobalId, OwnerHistory, Name, Description,
        #             ApplicableOccurrence, HasPropertySets, RepresentationMaps,
        #             Tag, ElementType, PredefinedType)
        "IFCPUMPTYPE",
        _s(gid(PUMP_TYPE)),
        "$",
        _s("Pump type T"),
        "$",
        "$",
        f"(#{type_qset})",
        "()",
        "$",
        "$",
        ".NOTDEFINED.",
    )

    # -- devices ---------------------------------------------------------
    p1 = _pump(b, P1, "P1")
    p2 = _pump(b, P2, "P2")
    p3 = _pump(b, P3, "P3")
    p4 = _pump(b, P4, "P4")
    p5 = _pump(b, P5, "P5")
    p6 = _pump(b, P6, "P6")
    v1 = _valve(b, V1, "V1")
    v2 = _valve(b, V2, "V2")
    v3 = _valve(b, V3, "V3")
    no_gid = b.entity(
        "IFCPUMP",
        "$",
        "$",
        _s("P-NO-GID"),
        "$",
        "$",
        "$",
        _s("P-NO-GID"),
        ".NOTDEFINED.",
        "$",
    )
    b.entity(
        "IFCRELDEFINESBYTYPE",
        _s(gid("to-rel-pump-type")),
        "$",
        "$",
        "$",
        f"(#{p1},#{p2},#{p3},#{p4},#{p5},#{p6})",
        f"#{pump_type}",
    )

    # P1: fully inherits the type quantities (no instance qset).
    # P2: instance overrides length (2.5 m explicit) and count (explicit 0);
    #     the type rows are shadowed and never added on top.
    p2_count_zero = _qcount(b, QNAME_COUNT, "0.")
    p2_length = _qlength(b, QNAME_LENGTH, "2.5", metre)
    p2_qset = _element_quantity(b, QTO_PUMP, [p2_count_zero, p2_length])
    _defines_by_properties(b, "to-rel-q-p2", [p2], p2_qset)

    # P3: instance same-identity conflict on TotalLength (1/4 m vs 1/2 m);
    #     count still inherits the type.
    p3_len_a = _qlength(b, QNAME_LENGTH, "1.", quarter)        # 0.25 m
    p3_len_b = _qlength(b, QNAME_LENGTH, "2.", quarter)        # 0.5 m
    p3_qset = _element_quantity(b, QTO_PUMP, [p3_len_a, p3_len_b])
    _defines_by_properties(b, "to-rel-q-p3", [p3], p3_qset)

    # P4: same name with different declared types (length vs area) -- distinct
    #     identities, never merged; plus an explicit area in square millimetres
    #     carrying an exact decimal fraction (3025.25 mm^2).
    p4_gauge_len = _qlength(b, QNAME_GAUGE, "1000.")           # project mm -> 1 m
    p4_gauge_area = _qarea(b, QNAME_GAUGE, "3025.25", mm2)     # 0.00302525 m^2
    p4_qset = _element_quantity(
        b, QTO_PUMP, [p4_gauge_len, p4_gauge_area]
    )
    _defines_by_properties(b, "to-rel-q-p4", [p4], p4_qset)

    # P5: unit cycle on TotalLength; NetArea valid decimal fraction (1/8 m^2
    #     written 0.125); an unsupported complex quantity is retained.
    p5_bad_length = _qlength(b, QNAME_LENGTH, "7.", u_a)
    p5_area = _qarea(b, QNAME_AREA, "0.125", m2)
    p5_complex = b.entity(
        # IfcPhysicalComplexQuantity(Name, Description, HasQuantities,
        #                           Discrimination, Quality, Usage)
        "IFCPHYSICALCOMPLEXQUANTITY",
        _s("ComplexPack"),
        "$",
        "()",
        _s("layer"),
        "$",
        "$",
    )
    p5_qset = b.entity(
        "IFCELEMENTQUANTITY",
        _s(gid("to-p5-qset")),
        "$",
        _s(QTO_PUMP),
        "$",
        _s("hand-authored drill"),
        f"(#{p5_bad_length},#{p5_area},#{p5_complex})",
    )
    _defines_by_properties(b, "to-rel-q-p5", [p5], p5_qset)

    # P6: dimension mismatch -- an area measure declared on a length quantity
    #     against an area unit; and a valid foot-conversion length on BoreLength.
    p6_mismatch = b.entity(
        # IfcQuantityLength carrying an area measure with an area unit: the
        # interpreter must reject this as a dimension mismatch.
        "IFCQUANTITYLENGTH",
        _s(QNAME_LENGTH),
        "$",
        f"#{m2}",
        "IFCAREAMEASURE(2.)",
        "$",
    )
    p6_bore = _qlength(b, QNAME_BORE, "2.", foot)  # 2 * 0.3048 = 0.6096 m
    p6_qset = _element_quantity(b, QTO_PUMP, [p6_mismatch, p6_bore])
    _defines_by_properties(b, "to-rel-q-p6", [p6], p6_qset)

    # V1: a valve with count 3 (counts are exact integers), length 3/2 m and
    #     area 5.5 m^2.
    v1_count = _qcount(b, QNAME_COUNT, "3.")
    v1_length = _qlength(b, QNAME_LENGTH, "1.5", metre)
    v1_area = _qarea(b, QNAME_AREA, "5.5", m2)
    v1_qset = _element_quantity(b, QTO_VALVE, [v1_count, v1_length, v1_area])
    _defines_by_properties(b, "to-rel-q-v1", [v1], v1_qset)

    # V2: valve length given in project millimetres (4000 mm = 4 m) and a
    # BoreLength expressed in the quarter-metre conversion unit (1 -> 0.25 m).
    v2_length_mm = _qlength(b, QNAME_LENGTH, "4000.")           # project mm -> 4 m
    v2_bore = _qlength(b, QNAME_BORE, "1.", quarter)            # 0.25 m
    v2_qset = _element_quantity(b, QTO_VALVE, [v2_length_mm, v2_bore])
    _defines_by_properties(b, "to-rel-q-v2", [v2], v2_qset)

    # V3: a valve whose declared TotalLength is an unsupported complex
    # quantity subtype (the line must be `unsupported`, not dropped or zero),
    # plus a Weight quantity outside the rule identities and a valid Volume
    # declaration that proves IfcQuantityVolume is retained and interpreted.
    v3_weight = _qweight(b, "Weight", "12.", kg)
    v3_volume = _qvolume(b, "NetVolume", "2.", m3)
    v3_complex = b.entity(
        # IfcPhysicalComplexQuantity with the rule's TotalLength name.
        "IFCPHYSICALCOMPLEXQUANTITY",
        _s(QNAME_LENGTH),
        "$",
        "()",
        _s("component"),
        "$",
        "$",
    )
    v3_qset = b.entity(
        "IFCELEMENTQUANTITY",
        _s(gid("to-v3-qset")),
        "$",
        _s(QTO_VALVE),
        "$",
        _s("hand-authored drill"),
        f"(#{v3_weight},#{v3_volume},#{v3_complex})",
    )
    _defines_by_properties(b, "to-rel-q-v3", [v3], v3_qset)

    # -- containment, including the repeated edge ------------------------
    _contain(b, "to-cont-b1", [p1, p2, p3, p4, p5, p6, v1, no_gid], room_b1)
    # P1 is contained in B2 as well: a second valid path inside storey A that
    # must not double count P1 under the storey rule.
    _contain(b, "to-cont-b2", [p1, v2], room_b2)
    _contain(b, "to-cont-d", [v3], room_d)

    # Broken containment reachable from room D (which is under the building,
    # but no rule roots above room D except none -- add a rule-rooted broken
    # edge inside B1 instead so the anomaly belongs to the A/B scopes):
    b.entity(
        "IFCRELCONTAINEDINSPATIALSTRUCTURE",
        _s(gid("to-cont-broken")),
        "$",
        "$",
        "$",
        f"(#{b.next_id + 9000})",
        f"#{room_b1}",
    )

    text = b.render(file_name="takeoff.ifc")
    ifc_path = os.path.join(root, "takeoff.ifc")
    with open(ifc_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    manifest = _write_manifest(
        root,
        [
            {
                "source_id": SOURCE,
                "path": "takeoff.ifc",
                "sha256": sha256_bytes(text.encode("utf-8")),
            }
        ],
    )
    rules_path = os.path.join(root, "takeoff-rules.json")
    with open(rules_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(build_rules_payload(), fh, indent=2)
        fh.write("\n")
    return TakeoffFixture(root=root, manifest=manifest, rules=rules_path, ifc_path=ifc_path)


def build_rules_payload() -> dict[str, Any]:
    def identity(
        qset: str, name: str, declared_type: str, entity_type: str | None = None
    ) -> dict[str, str]:
        row = {
            "quantity_set": qset,
            "name": name,
            "declared_type": declared_type,
        }
        if entity_type is not None:
            row["entity_type"] = entity_type
        return row

    pump_identities = [
        identity(QTO_PUMP, QNAME_COUNT, "IfcQuantityCount", "IfcPump"),
        identity(QTO_PUMP, QNAME_LENGTH, "IfcQuantityLength", "IfcPump"),
        identity(QTO_PUMP, QNAME_AREA, "IfcQuantityArea", "IfcPump"),
        identity(QTO_PUMP, QNAME_GAUGE, "IfcQuantityLength", "IfcPump"),
        identity(QTO_PUMP, QNAME_GAUGE, "IfcQuantityArea", "IfcPump"),
    ]
    valve_identities = [
        identity(QTO_VALVE, QNAME_COUNT, "IfcQuantityCount", "IfcValve"),
        identity(QTO_VALVE, QNAME_LENGTH, "IfcQuantityLength", "IfcValve"),
        identity(QTO_VALVE, QNAME_AREA, "IfcQuantityArea", "IfcValve"),
        identity(QTO_VALVE, QNAME_BORE, "IfcQuantityLength", "IfcValve"),
    ]
    return {
        "rules_version": 1,
        "rules": [
            {
                "rule_id": RULE_A,
                "root": {"source_id": SOURCE, "global_id": gid(STOREY_A)},
                "entity_types": ["IfcPump", "IfcValve"],
                "quantity_identities": pump_identities + valve_identities,
            },
            {
                "rule_id": RULE_B,
                "root": {"source_id": SOURCE, "global_id": gid(ROOM_B1)},
                "entity_types": ["IfcPump", "IfcValve"],
                "quantity_identities": pump_identities + valve_identities,
            },
            # Same scope as rule A: an object matched by two rules is emitted
            # once per rule and summaries are computed independently.
            {
                "rule_id": RULE_A_DUP,
                "root": {"source_id": SOURCE, "global_id": gid(STOREY_A)},
                "entity_types": ["IfcPump"],
                "quantity_identities": pump_identities,
            },
            {
                "rule_id": RULE_MISSING_ROOT,
                "root": {"source_id": SOURCE, "global_id": gid("to-no-such-root")},
                "entity_types": ["IfcPump"],
                "quantity_identities": pump_identities,
            },
            {
                "rule_id": RULE_EMPTY,
                "root": {"source_id": SOURCE, "global_id": gid(STOREY_C)},
                "entity_types": ["IfcPump", "IfcValve"],
                "quantity_identities": pump_identities + valve_identities,
            },
            {
                "rule_id": RULE_ROOM_D,
                "root": {"source_id": SOURCE, "global_id": gid(ROOM_D)},
                "entity_types": ["IfcValve"],
                "quantity_identities": valve_identities,
            },
        ],
    }


# ---------------------------------------------------------------------------
# Scaled fixture: many devices, bounded-memory evidence
# ---------------------------------------------------------------------------


def build_scaled_fixture(
    root: str,
    *,
    storeys: int = 6,
    rooms_per_storey: int = 4,
    devices_per_room: int = 12,
) -> dict[str, Any]:
    """Generate a large quantity-heavy fixture with closed-form expectations.

    Every pump gets an instance count of 2 and an instance length of
    ``1250`` mm (1.25 m); every 7th pump (deterministic) carries an explicit
    zero length instead.  Expected totals are derived arithmetically below and
    asserted independently of the implementation.
    """

    os.makedirs(root, exist_ok=True)
    b = IfcBuilder()
    mm = b.entity("IFCSIUNIT", "*", ".LENGTHUNIT.", ".MILLI.", ".METRE.")
    m2 = b.entity("IFCSIUNIT", "*", ".AREAUNIT.", "$", ".SQUARE_METRE.")
    m3 = b.entity("IFCSIUNIT", "*", ".VOLUMEUNIT.", "$", ".CUBIC_METRE.")
    kg = b.entity("IFCSIUNIT", "*", ".MASSUNIT.", ".KILO.", ".GRAM.")
    units = b.entity("IFCUNITASSIGNMENT", f"(#{mm},#{m2},#{m3},#{kg})")
    project = b.entity(
        "IFCPROJECT",
        _s(gid("sc-to-project")),
        "$",
        _s("scaled take-off"),
        "$",
        "$",
        "$",
        "$",
        "$",
        f"#{units}",
    )
    site = b.entity(
        "IFCSITE",
        _s(gid("sc-to-site")),
        "$",
        _s("Site"),
        "$",
        "$",
        "$",
        "$",
        "$",
        ".ELEMENT.",
        "$",
        "$",
        "$",
        "$",
        "$",
    )
    _aggregate(b, "sc-to-agg-ps", project, [site])
    total_devices = 0
    zero_length = 0
    rules = []
    for s in range(storeys):
        storey = b.entity(
            "IFCBUILDINGSTOREY",
            _s(gid(f"sc-to-storey-{s}")),
            "$",
            _s(f"Storey {s}"),
            "$",
            "$",
            "$",
            "$",
            "$",
            ".ELEMENT.",
            "$",
        )
        _aggregate(b, f"sc-to-agg-site-{s}", site, [storey])
        rules.append(
            {
                "rule_id": f"sc-rule-{s}",
                "root": {"source_id": SOURCE, "global_id": gid(f"sc-to-storey-{s}")},
                "entity_types": ["IfcPump"],
                "quantity_identities": [
                    {
                        "entity_type": "IfcPump",
                        "quantity_set": QTO_PUMP,
                        "name": QNAME_COUNT,
                        "declared_type": "IfcQuantityCount",
                    },
                    {
                        "entity_type": "IfcPump",
                        "quantity_set": QTO_PUMP,
                        "name": QNAME_LENGTH,
                        "declared_type": "IfcQuantityLength",
                    },
                ],
            }
        )
        for r in range(rooms_per_storey):
            room = b.entity(
                "IFCSPACE",
                _s(gid(f"sc-to-room-{s}-{r}")),
                "$",
                _s(f"Room {s}-{r}"),
                "$",
                "$",
                "$",
                "$",
                _s(f"R{s}-{r}"),
                ".ELEMENT.",
                ".SPACE.",
                "$",
            )
            _aggregate(b, f"sc-to-agg-{s}-{r}", storey, [room])
            pumps = []
            for d in range(devices_per_room):
                label = f"sc-to-pump-{s}-{r}-{d}"
                pump = _pump(b, label, f"P{s}-{r}-{d}")
                pumps.append(pump)
                total_devices += 1
                is_zero = (total_devices % 7 == 0)
                if is_zero:
                    zero_length += 1
                q_count = _qcount(b, QNAME_COUNT, "2.")
                q_length = _qlength(
                    b,
                    QNAME_LENGTH,
                    "0." if is_zero else "1250.",
                )
                qset = _element_quantity(b, QTO_PUMP, [q_count, q_length])
                _defines_by_properties(b, f"sc-to-rel-q-{s}-{r}-{d}", [pump], qset)
            _contain(b, f"sc-to-cont-{s}-{r}", pumps, room)
    text = b.render(file_name="scaled-takeoff.ifc")
    ifc_path = os.path.join(root, "scaled-takeoff.ifc")
    with open(ifc_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    manifest = _write_manifest(
        root,
        [
            {
                "source_id": SOURCE,
                "path": "scaled-takeoff.ifc",
                "sha256": sha256_bytes(text.encode("utf-8")),
            }
        ],
    )
    rules_path = os.path.join(root, "scaled-takeoff-rules.json")
    with open(rules_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"rules_version": 1, "rules": rules}, fh, indent=2)
        fh.write("\n")
    nonzero = total_devices - zero_length
    expected = {
        "devices": total_devices,
        "zero_length_devices": zero_length,
        "count_total": 2 * total_devices,
        # Each non-zero length is exactly 1.25 m.
        "length_total_numerator": 5 * nonzero,
        "length_total_denominator": 4,
        "length_total_decimal": _fraction_decimal(5 * nonzero, 4),
        "valid_length_lines": nonzero,
        "zero_is_valid": True,
    }
    return {
        "manifest": manifest,
        "rules": rules_path,
        "expected": expected,
        "storeys": storeys,
        "rooms_per_storey": rooms_per_storey,
        "devices_per_room": devices_per_room,
    }


def _fraction_decimal(numerator: int, denominator: int) -> str:
    from fractions import Fraction

    from ifc_handover.units import exact_si_decimal

    decimal, exact = exact_si_decimal(Fraction(numerator, denominator))
    assert exact
    return decimal
