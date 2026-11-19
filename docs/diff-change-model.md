# Cross-version change dataset reference

A `diff` run reads two built repositories strictly read-only and writes a new,
independent change dataset.  This document defines the categories, the record
shape and the identity rules so a handover reviewer can interpret every row
without reopening the two input packages.

## Identity rules

| Input situation | Result |
|---|---|
| Same GlobalId, exactly one usable root on each side | auto-matched pair |
| GlobalId only on the left | `removed` |
| GlobalId only on the right | `added` |
| Changed GlobalId + valid explicit one-to-one mapping | matched pair, mapping rationale retained |
| Changed GlobalId without a mapping | separate `removed` **and** `added`, never guessed |
| Duplicate GlobalId, or multiple sources for one id, on either side | `incomparable` (identity) |
| Mapped endpoint exists but is unusable (e.g. duplicate) | `incomparable` (identity), evidence retained |
| No GlobalId at all (STEP-scoped root) | listed separately as added/removed; STEP id is not identity |
| Structurally invalid mapping table (duplicate row, missing endpoint, many-to-one, no rationale) | hard rejection, no dataset written |

Names, entity types and spatial positions are never used to infer identity.

## Result categories

* `added` — an identity present only on the right.
* `removed` — an identity present only on the left.
* `semantic_change` — a matched pair whose resolved business meaning changed:
  entity type, effective property value/state, spatial parent edge, type
  assignment, or material structure/layer order.
* `provenance_change` — the effective value and state are unchanged but the
  declaration location changed (e.g. type scope → instance scope).
* `incomparable` — evidence is missing or conflicted so no confident
  before/after can be stated.  This is a distinct outcome, never a remove+add.

## Aspect (`aspect`)

`identity`, `entity_type`, `property`, `spatial_relation`, `type_assignment`,
`material`.

## Record fields

| Field | Meaning |
|---|---|
| `change_id` | SHA-256 over the record's stable content (excluding the ordinal) |
| `category`, `aspect` | result category and the semantic area |
| `left` / `right` | `{source_id, sha256}` for each participating side, or `null` |
| `identity.kind` | `global_id`, `explicit_mapping` or `step_scoped` |
| `identity.left_global_id` / `right_global_id` | the matched GlobalIds |
| `identity.mapping` | mapping ordinal + rationale when an explicit map was used |
| `subject` | stable locator for the changed item (property identity, edge, layer set) |
| `locator` | where in the structure the change sits (property id, edge kind/direction, layer ordinal) |
| `before` / `after` | canonical value or state on each side; `absent` means the item had no effective declaration |
| `before_provenance` / `after_provenance` | declaration scope and source STEP locators |
| `reason` | explanation for provenance or incomparable rows |
| `evidence[]` | minimum `source_entities` excerpts (side, source, SHA-256, STEP id, role, line range, raw text) |

## Property value normalisation

* Measured numeric values compare on the exact SI fraction.  `IFCLENGTHMEASURE(2.)`
  with a metre unit and `IFCLENGTHMEASURE(2000.)` against a millimetre project
  unit both normalise to SI `2`, so the property is unchanged.
* Non-numeric values compare on their typed canonical payload.
* The states `explicit_null`, `absent`, `conflict`, `unsupported` and
  `expression_unevaluated` are distinct.  An explicit null replacing a value is
  a `semantic_change` from `value` to `explicit_null`, never to an empty string.
* A property in `conflict` on either side yields an incomparable property row
  rather than a guessed value comparison.

## Spatial and material locators

* A room move emits two `spatial_relation` rows (`parent_removed`,
  `parent_added`), each carrying the relation type, edge kind and the parent
  GlobalId/name.  A missing or conflicted edge instead yields one
  `incomparable` spatial row.
* A material change carries the ordered layer list on each side.  Every layer
  keeps its `ordinal`, material name and layer detail (thickness, priority), so
  a reorder or substitution is traceable to the specific layer.

## Storage tables

`changes` (one row per record), `evidence` (STEP excerpts), `identity_mappings`
(the validated correspondence), `side_sources` (per-side source id/SHA/status),
`object_summary` (categories per matched identity) and `meta` (manifest and the
completion marker).  The same records are streamed to `changes.jsonl`.
