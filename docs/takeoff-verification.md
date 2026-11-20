# Spatial quantity take-off dataset format

## Purpose

The take-off entry point reads one fixed, read-only object repository plus a
UTF-8 JSON rules file and produces an independently re-openable dataset that
answers, per spatial root and equipment entity type:

* how many included objects fall in scope;
* how much declared count / length / area / volume / weight they carry;
* which exact `IfcElementQuantity` / `IfcPhysicalSimpleQuantity` declarations
  produce each number, with type/instance owner and STEP source coordinates.

It performs **no geometry**, derives nothing from dimensions, never modifies
the input repository and is neither a model-version diff nor a signoff
conclusion.

## Rules file

```json
{
  "rules_version": 1,
  "rules": [
    {
      "rule_id": "zone-a-pumps",
      "root": {"source_id": "mechanical", "global_id": "0_RV8HL0x40..."},
      "entity_types": ["IfcPump", "IfcValve"],
      "quantity_identities": [
        {"entity_type": "IfcPump",
         "quantity_set": "Qto_PumpBaseQuantities",
         "name": "Count", "declared_type": "IfcQuantityCount"},
        {"entity_type": "IfcValve",
         "quantity_set": "Qto_ValveBaseQuantities",
         "name": "TotalLength", "declared_type": "IfcQuantityLength"}
      ]
    }
  ]
}
```

* Unknown keys, wrong types, empty lists, non-IFC entity type names,
  unsupported declared types, an `entity_type` binding not present in the
  rule's `entity_types`, and duplicate rule ids / in-rule identities reject
  the whole file before any output is written.
* An `entity_type` field on a quantity identity binds it to that one entity
  type; when omitted the identity applies to every entity type of the rule.
* A **quantity identity** is the triple
  `(quantity_set, name, declared_type)`.  Same set + name but different
  declared type means a different identity; the two are never merged.

## Scope rules

* Only spatial `decomposition` (`IfcRelAggregates`) and `containment`
  (`IfcRelContainedInSpatialStructure`) edges are walked downward from the
  root.  Type assignment and material relations never widen a scope, and a
  type object is never itself counted as equipment.
* The root must resolve to exactly one usable spatial object; otherwise a
  `root_unresolved`, `root_ambiguous`, `root_unusable` or
  `root_not_spatial` anomaly is emitted with source coordinates.
* Repeated edges reaching one object inside a rule produce one member.  The
  same object reached from two different roots is stored once per rule and
  summarised independently per root.
* Every invalid edge reachable from a root (missing endpoint, conflict) is an
  `edge_missing_endpoint` / `edge_conflict` anomaly; the scope becomes
  `unverified` rather than being silently narrowed.  An included object with
  a missing/conflicted GlobalId is recorded as an identity anomaly and
  excluded from the valid membership; its source-local identity is retained
  for explanation and STEP numbers are never used to merge objects.
* Damage outside a root never affects it.  A scope with zero included
  objects and no anomalies is an explicit **empty_scope**, distinct from a
  populated scope whose objects lack a required declaration.

## Quantity ingestion and interpretation

* The repository retains `IfcElementQuantity` definitions, their owner scope
  (`instance` / `type` / `unassigned`), every `IfcPhysicalSimpleQuantity`
  declaration, the quantity set and definition provenance, the STEP position
  and the original decimal token.
* Supported subtypes: `IfcQuantityCount`, `IfcQuantityLength`,
  `IfcQuantityArea`, `IfcQuantityVolume`, `IfcQuantityWeight`.  Any other
  subtype (including `IfcPhysicalComplexQuantity`) is stored as an
  unsupported declaration with a reason; it is never dropped or assigned a
  guessed value.
* An explicit unit on the quantity wins; otherwise the project unit for the
  quantity's dimension is used; counts need no unit.  Canonical units are
  `count`, `m`, `m²`, `m³`, `kg`.  SI prefix powers (e.g. mm² → 10⁻⁶ m²) and
  offset-free conversion units are applied exactly.  Dimension mismatch,
  offset conversion, unit reference cycle, missing unit and non-finite values
  are distinct failure statuses and never become zero.
* All arithmetic uses exact `Fraction`/decimal values; binary floats are never
  accumulated.

## Precedence and line states

For each included object and each rule-required identity, exactly one line is
emitted:

| line state | meaning |
|---|---|
| `valid` | one effective value with an exact canonical number (an explicit zero is a normal valid value) |
| `missing` | scope confirmed; no instance or type declaration of the identity (an explicit null is also reported here with `explicit_null` status and never counts as zero) |
| `conflict` | same-level same-identity declarations differ; no value is picked; all declarations and coordinates retained |
| `unsupported` | the declaration exists but its subtype/value is not computable, or a same-name quantity has a different declared type (with reason) |
| `unit_unavailable` | dimension mismatch / unit cycle / missing or unresolved unit (including an offset conversion unit) |
| `scope_uncertain` | the scope has unresolved edges, so absence cannot be confirmed |

Precedence:

1. instance declarations completely replace type declarations for that
   identity — including an explicit zero, an explicit null and an instance
   conflict; the shadowed type evidence is retained but never added on top;
2. type declarations are inherited only when no instance declaration exists;
3. equal declarations at one level merge while all source coordinates remain;
4. differing declarations at one level are a conflict and none is selected.

Missing or invalid lines never participate in a total as zero and never mask
the object's other usable quantities.  An object matched by multiple rules is
attributed once per rule id; there is no cross-rule double count.

## Dataset tables (`takeoff.sqlite`)

* `rules`, `rule_entities`, `rule_quantity_identities` — parsed inputs,
  including the per-entity-type binding of each quantity identity.
* `root_scopes` — per-rule scope status, included object count, anomaly count.
* `scope_anomalies` — every scope issue with in-repository source
  coordinates (`source_id`, `step_id`, `relation_key`, JSON detail).
* `scope_membership` — every rule member with `valid` / `excluded_identity`.
* `object_details` — one row per `(rule, object, identity)` with full object
  identity, root, effective scope (instance/type), original value, canonical
  exact value, unit source, line state, and all declarations embedded as JSON
  (including shadowed type evidence and conflict members).
* `quantity_summaries` — grouped by rule / root / entity type / identity with
  included object count, the six line-state counts and the exact rational
  total (`total_num` / `total_den`) plus a decimal rendering that is marked
  exact only when the expansion terminates.
* `evidence` — minimum STEP excerpts for object lines and scope anomalies.
* `sources` and `meta` — source summary, input fingerprints, completion marker.

`takeoff.jsonl` contains one JSON object per object detail, per summary and
per scope anomaly, in stable order.  The directory is self-contained and
reopens read-only after being copied, with no access to the repository, IFC
files or original paths.

## Reproducible commands

```bash
# Build (staging directory + atomic publish; a failure keeps the old dataset).
python3 -m ifc_handover takeoff-build \
    --repository out/handover.sqlite \
    --rules takeoff-rules.json --dest out/takeoff-dataset

# Report, filtered queries and one-object drill-down.
python3 -m ifc_handover takeoff-report --dataset out/takeoff-dataset
python3 -m ifc_handover takeoff-query  --dataset out/takeoff-dataset summaries
python3 -m ifc_handover takeoff-query  --dataset out/takeoff-dataset details \
    --rule rule-storey-a --type IfcPump --state valid
python3 -m ifc_handover takeoff-object --dataset out/takeoff-dataset \
    --source-id mechanical --global-id '<GlobalId>'
python3 -m ifc_handover takeoff-export-jsonl --dataset out/takeoff-dataset \
    --jsonl out/takeoff.jsonl
```

Every summary total can be recomputed independently by summing the
`valid` detail lines' `canonical_num / canonical_den` for the same rule,
entity type and identity.
