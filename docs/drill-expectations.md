# Drill expectations (hand-authored)

These expectations were written from the synthetic IFC text in
`tests/fixtures.py`, not produced by running the implementation.  The test
suite compares business output to this table.

## Fixture: `drill.ifc` (IFC4)

Project units: LENGTH = millimetre (`IfcSIUnit` `MILLI`/`METRE`),
AREA = square metre, VOLUME = cubic metre, MASS = kilogram
(`KILO`/`GRAM`).

Spatial decomposition:
`IfcProject` → `IfcSite` → `IfcBuilding` → `IfcBuildingStorey` (“Level 1”)
→ `IfcSpace` “Room 1”, “Room 2”.  Room 1 contains `W1`, `EQ-A`, `EQ-B`,
`EQ-C`, `EQ-D`.  Room 2 contains `W2`.

Types:
`WType` (IfcWallType) is shared by `W1` and `W2`.
`EqType` (IfcBuildingElementProxyType) is shared by `EQ-A` and `EQ-B`.
`LoadType` is shared by `EQ-C` and `EQ-D`.

### Property states, values and provenance

| Object | Identity (pset, name, kind) | Declared | State | SI value | Scope used |
|---|---|---|---|---|---|
| W1 | Pset_WallCommon, Length, SingleValue | `IFCLENGTHMEASURE(1000.)`, no property unit | value | 1 m | type |
| W1 | Pset_WallCommon, FireRating, SingleValue | `IFCLABEL('F30')` | value | n/a (uninterpreted) | type |
| W1 | Pset_WallCommon, AcousticRating, SingleValue | `IFCLABEL('55 dB')` | value | n/a | type |
| W2 | same as W1 (shared `WType`) | | | | type |
| EQ-A | Pset_EquipmentCommon, Length, SingleValue | instance `IFCLENGTHMEASURE(2.)` with explicit metre unit | value | **2 m** | instance |
| EQ-B | Pset_EquipmentCommon, Length, SingleValue | instance `$` (explicit null) | **explicit_null** | none | instance |
| EQ-A | Pset_Dimensions, Width, SingleValue | `IFCLENGTHMEASURE(2.)` with `INCH` conversion unit (factor 0.0254 m) | value | 0.0508 m | instance |
| EQ-A | Pset_Dimensions, Coverage, SingleValue | `IFCAREAMEASURE(3.)`, no property unit | value | 3 m² | instance (project area unit) |
| EQ-A | Pset_Dimensions, PatchArea, SingleValue | `IFCAREAMEASURE(500000.)` with `MILLI`/`SQUARE_METRE` | value | 0.5 m² | instance (property unit, prefix power 10⁻⁶) |
| EQ-C | Pset_Loads, Load, SingleValue | instance `IFCMASSMEASURE(30.)` | value | 30 kg | instance |
| EQ-C | Pset_Loads, Voltage, SingleValue | type `IFCLABEL('230V')` | value | n/a | type (not overridden) |
| EQ-D | Pset_Loads, Load, SingleValue | type `IFCMASSMEASURE(10.)` and `IFCMASSMEASURE(20.)` | **conflict** | none | type |
| EQ-D | Pset_Loads, Voltage, SingleValue | type `IFCLABEL('230V')` | value | n/a | type |
| EQ-D | Pset_Cyclic, CyclicLength, SingleValue | `IFCLENGTHMEASURE(4.)` with cyclic conversion unit | value / **unit_cycle** | none | instance |
| EQ-D | Pset_Mismatch, BadLength, SingleValue | `IFCLENGTHMEASURE(7.)` with an area unit | value / **dimension_mismatch** | none | instance |
| EQ-Unitless | Pset_Unitless, Length, SingleValue | `IFCLENGTHMEASURE(5.)` in a project with no units | value / **missing_unit** | none | instance |
| EQ-A | Pset_Finish, Finish, SingleValue | `IFCLABEL('Paint')` | value (uninterpreted) | n/a | instance |
| EQ-A | Pset_Finish, Finish, EnumeratedValue | `('Paint','Tile')` | value (uninterpreted) | n/a | instance |

Notes:

* The `EqType` type declares `Length = 1000 mm` (1 m).  For `EQ-A` the instance
  declaration (2 m) overrides it, so 1 m must not appear.  For `EQ-B` the
  instance declaration is an explicit null and must not fall back to 1 m.
* The overridden type declaration remains visible as
  `type_declaration_ignored` evidence on both objects.
* `LoadType` has two differing declarations of `Load` (10 kg and 20 kg).
  `EQ-C` overrides `Load` with 30 kg, which resolves that object’s property and
  only that identity; the type conflict is retained as masked evidence on
  `EQ-C`.  `EQ-D` has no override and therefore has a `conflict` for `Load`.
  Because that object has a property conflict it is not usable and is excluded
  from the export’s usable object set.
* The same-name properties in `Pset_Finish` differ in declared kind and are
  two identities, not a conflict.

### Material hierarchy

`WType` is associated with `IfcMaterialLayerSetUsage` → `IfcMaterialLayerSet`
“Wall assembly” with three ordered layers:

1. Insulation, 50.0 (layer thickness in project length unit)
2. Concrete, 200.0
3. Plaster, 15.0

Each layer references its `IfcMaterial`.  `W1` and `W2` inherit this material
from their shared type; the assignment records `inherited_from_type = true`.
Order and multiplicity are preserved; the set is not flattened to a list of
names.

### Subset export for root = “Room 1”

Objects selected (containment/decomposition descent only):

* Room 1
* W1
* EQ-A
* EQ-B
* EQ-C
* (EQ-D is reached but is unusable because of the `Load` conflict; it is
  listed under `excluded` with its reason and is not part of the object table.)

Definitions included because the selected objects use them:

* type `WType` (supplies W1’s properties and inherited material)
* type `EqType` (its `Length` declaration is overridden by EQ-A/EQ-B and must
  remain as evidence)
* type `LoadType` (EQ-C’s `Voltage` comes from it)
* material layer set “Wall assembly” with all three ordered layers once
* project units for LENGTH, AREA and MASS, and the explicit metre, inch
  conversion, and square-millimetre units used by property values
* the external document target `https://example.invalid/spec.pdf` (recorded
  only, never opened)

Objects **not** in the package:

* Room 2 and its wall W2, even though W2 shares `WType` with W1
* any other object solely because it shares a type or material

### Failure evidence expected from `cycle.ifc`

| Category | Detail |
|---|---|
| `spatial_cycle` | rooms A and B decompose into each other; both unusable |
| `duplicate_global_id` | spaces D and E share one GlobalId; both unusable, neither merged |
| `missing_endpoint` | a decomposition reference to a non-existent STEP id |

### Reorder and renumbering

Reversing the DATA-section entity order, or shifting every STEP id by a
constant, must not change any row in the business projection (values, states,
scopes, conflicts, material order, relation states).  Line numbers and STEP ids
may change; each business row must resolve back to the same source declaration
text via `source_entities`.
