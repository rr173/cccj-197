# Spatial quantity take-off — hand-written drill expectations

These expectations are authored independently of the implementation and
asserted by `tests/test_takeoff.py`.  Every number below is computed by hand
from the explicit declarations in `tests/takeoff_fixtures.py`; no geometry is
evaluated and no quantity is derived from dimensions.

## Inputs

One source `to-model` (`takeoff.ifc`, regenerated at test time) and five
rules in `takeoff-rules.json`:

| rule | root | entity types |
|---|---|---|
| `rule-storey-a` | storey A | IfcPump, IfcValve |
| `rule-room-b1` | room B1 (under storey A) | IfcPump, IfcValve |
| `rule-storey-a-duplicate` | storey A | IfcPump |
| `rule-missing-root` | a GlobalId that does not exist | IfcPump |
| `rule-storey-c` | empty storey C | IfcPump, IfcValve |

Spatial facts:

* P1 is contained in both B1 and B2 (two valid containment edges).  Under
  the storey-A rule it is still **one** included object; under the B1 rule it
  is included through B1.
* The storey-A scope also contains a broken containment relation (dangling
  related element).  The scope is therefore `unverified`, but every object
  reached over valid edges still gets full lines; a *missing* identity in an
  unverified scope is reported `scope_uncertain`, not `missing`.
* A pump with no GlobalId sits in B1.  Its source-local STEP identity is
  retained as an anomaly (`object_identity_missing`) and it is never included
  or merged by STEP number.
* Storey C contains no devices: `empty_scope`, distinct from "scope complete
  but no matching quantity declaration".

## Quantity declarations per object

Type `IfcPumpType` (type-scope `Qto_PumpBaseQuantities`):
`Count = 4` (count), `TotalLength = 1000 mm` → **1 m** (project length unit).

| object | identity | declarations | effective outcome |
|---|---|---|---|
| P1 | pump Count / TotalLength | none on instance | inherits type: **4**, **1 m** |
| P2 | pump Count | instance `Count = 0` | **0** (explicit zero, valid; type 4 not added) |
| P2 | pump TotalLength | instance `2.5 m` (explicit metre unit) | **2.5 m** (type 1 m shadowed) |
| P3 | pump Count | none | inherits type **4** |
| P3 | pump TotalLength | two instance lengths: 1·quarter (0.25 m) and 2·quarter (0.5 m) | **conflict** — no value picked |
| P4 | pump `GaugeLength` (IfcQuantityLength) | instance `1000` project mm | **1 m**, valid |
| P4 | pump `GaugeLength` (IfcQuantityArea) | instance `3025.25 mm²` | **0.00302525 m²**, valid; different declared type → different identity, never merged |
| P5 | pump TotalLength | instance `7` with a cyclic unit | **unit_unavailable / unit_cycle** |
| P5 | pump NetArea | instance `0.125 m²` | **0.125 m² = 1/8 m²**, valid |
| P5 | pump (complex) | `IfcPhysicalComplexQuantity ComplexPack` | **unsupported** declaration retained, not dropped |
| P6 | pump TotalLength | `IfcQuantityLength` carrying `IfcAreaMeasure(2)` with an area unit | **unit_unavailable / dimension_mismatch** |
| P6 | pump BoreLength | `2` in the 0.3048 m conversion unit | **0.6096 m** exactly |
| V1 | valve Count / TotalLength / NetArea | `3`, `1.5 m`, `5.5 m²` | **3**, **1.5 m**, **5.5 m²**, all valid |
| V2 | valve TotalLength | `4000` project mm | **4 m** |
| V2 | valve BoreLength | `1` quarter-metre unit | **0.25 m** |
| V3 | valve required identities | only a Weight quantity | every required identity **missing**; scope itself complete |

## Expected summary — `rule-storey-a`

Included pumps: P1–P6 (6).  Included valves: V1 (B1), V2 (B2) (2).  V3 is in
room D, outside storey A.  Explicit-zero lines are `valid`; conflict,
unsupported and unit-unavailable lines never contribute zero to a total.

### IfcPump / IfcQuantityCount (`Qto_PumpBaseQuantities/Count`)

* included objects 6; all six lines are valid because every pump without an
  instance Count inherits the type Count=4, while P2's explicit instance 0
  replaces the type value.
* exact total = 4 (P1) + 0 (P2) + 4 (P3) + 4 (P4) + 4 (P5) + 4 (P6) =
  **20** (count).  The explicit zero changes nothing arithmetically but is
  still a distinct valid declaration.

### IfcPump / IfcQuantityLength (`Qto_PumpBaseQuantities/TotalLength`)

* valid 3 (P1=1 m, P2=2.5 m, P4=1 m inherited from the type because P4 only
  declares GaugeLength quantities); conflict 1 (P3); unit unavailable 2
  (P5 cycle, P6 dimension mismatch); missing 0; scope uncertain 0.
* exact total = 1 + 2.5 + 1 = **9/2 m = 4.5 m**.

### IfcPump / IfcQuantityArea (`Qto_PumpBaseQuantities/NetArea`)

* valid 1 (P5 = 1/8 m²); the other five lines are `scope_uncertain` because
  the storey-A scope is unverified and the objects neither declare nor
  inherit a NetArea (P4 carries GaugeLength area, not NetArea; P6's only
  area-bearing declaration is the mismatched TotalLength).
* exact total = **1/8 m² = 0.125 m²**.

### IfcPump / GaugeLength, two declared types

* `IfcQuantityLength` identity: valid 1 (P4 = 1 m), missing 4, scope
  uncertain 1; total **1 m**.
* `IfcQuantityArea` identity: valid 1 (P4 = 0.00302525 m²), missing 4, scope
  uncertain 1; total **12101/4,000,000 m² = 0.00302525 m²** — a terminating
  decimal with no float rounding.

### IfcValve lines (rule A)

* Count: valid 1 (V1=3), missing 1 (V2) → total **3**.
* TotalLength: valid 2 (V1=1.5 m, V2=4 m) → total **11/2 m = 5.5 m**.
* NetArea: valid 1 (V1=5.5 m²), missing 1 (V2) → total **11/2 m² = 5.5 m²**.
* BoreLength: valid 1 (V2=0.25 m), missing 1 (V1) → total **1/4 m = 0.25 m**.

## Expected summary — `rule-room-b1`

Included pumps P1–P6 and valve V1 only (V2 is in B2).  The broken
containment edge originates in B1, so this scope is also `unverified`.

* Pump Count: valid 6 = total **20**.
* Pump TotalLength: valid 3 total **4.5 m**; conflict 1; unit unavailable 2.
* Pump NetArea: valid 1 total **0.125 m²**; five scope-uncertain lines.
* Valve Count: valid 1 (V1=3); valve TotalLength: valid 1 (1.5 m); valve
  NetArea: valid 1 (5.5 m²); valve BoreLength: missing 1.

## Rules with no usable scope

* `rule-missing-root`: one `root_unresolved` anomaly with the requested
  `(source_id, GlobalId)` coordinates; zero included objects; no detail lines.
* `rule-storey-c`: scope `confirmed`, zero included objects, zero anomalies —
  an `empty_scope`.  It must not be confused with a populated scope whose
  objects simply lack declarations (V3 under a valve rule).
* `rule-storey-a-duplicate`: identical business numbers to the pump portion
  of `rule-storey-a`, stored under its own rule id; the shared objects are
  attributed once per rule, never double-counted inside either rule.

## Reordering invariance

A second fixture is generated with the same entities emitted in a shuffled
order (new STEP ids and line coordinates).  Every business total, line state
and object attribution above must be unchanged; only the STEP coordinates and
excerpt line numbers differ, and each stored excerpt must still point at the
declaration it evidences.
