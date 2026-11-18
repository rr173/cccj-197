# IFC handover offline repository

An offline object repository and handover exporter for IFC4 files.  It is meant
for property analysts who need to review equipment relationships item by item,
not just a flattened attribute CSV.

The tool parses IFC and reads attributes, types and units.  It performs **no
geometry, placement, rendering, area/volume computation or design work**.

## What it provides

* **Per-source ingestion** from a manifest with a logical `source_id` and a
  SHA-256 for each file.  Each file is an independent namespace; objects are
  identified by `(source_id, GlobalId)`.  Nodes without a GlobalId use
  `(source_id, file SHA-256, STEP id)`.  GlobalIds are never invented.
* **Relationships preserved**: type assignment, spatial decomposition and
  containment, and material assignment.  Material layer sets keep their layer
  order and per-layer materials; material lists and constituent sets keep their
  structure rather than being flattened to names.
* **Properties**: `IfcPropertySingleValue` and `IfcPropertyEnumeratedValue`.
  Property identity is `(property set name, property name, declared kind)`.
  States distinguish `absent`, `explicit_null`, `value`, `unsupported`,
  `expression_unevaluated` and `conflict`.
* **Override semantics**: an instance declaration overrides the same identity
  from its type, including an explicit null (no fallback).  Identical
  declarations at one level merge and keep all sources.  Differing declarations
  at one level are a `conflict` for that identity only; other identities still
  resolve.  An instance declaration masks a type conflict and the masked
  evidence is retained.
* **Units**: exact rational arithmetic for length, area, volume and mass.
  SI prefixes, prefix powers for area/volume (`mm²` → 10⁻⁶ m²), and
  offset-free conversion units are supported.  A property's explicit unit wins;
  otherwise the project unit for that dimension is used.  Output carries the
  original value, exact SI value, SI decimal when exactly representable, the
  dimension and the unit source.  `missing_unit`, `dimension_mismatch`,
  `unit_cycle` and `unresolved_unit` are distinct value statuses; none means
  zero, and they never erase the object's other properties.  Decimal values and
  conversion factors are never converted through binary floats.
* **Failure evidence**: duplicate GlobalIds, spatial decomposition cycles and
  missing relation endpoints are retained.  The affected objects are excluded
  from the usable set, and lookups that would have to choose between duplicates
  raise `AmbiguousIdentity` instead of guessing.  External references record
  the external target only; no network or filesystem access is attempted.
* **Queries** by object, type, spatial ancestor and property state/status,
  exposed as a Python API and a CLI.
* **Subset export**: given spatial roots, selects objects by decomposition and
  containment only.  The package contains exactly the types, materials,
  properties and unit evidence those objects use, with shared definitions
  stored once.  It is self-contained (SQLite + JSON/CSV tables + exact STEP
  excerpts) and works after being copied to a new directory.  Writes are atomic;
  a failed export never replaces an existing complete package.

## Install

Python 3.10+ and IfcOpenShell:

```bash
python3 -m pip install ifcopenshell
```

The project itself is pure Python and has no other runtime dependencies.

## Usage

```bash
# 1. Ingest a manifest of IFC files into an offline repository.
python3 -m ifc_handover ingest \
    --manifest manifest.json \
    --repository out/handover.sqlite \
    --store-dir out/sources

# 2. Query objects and evidence.
python3 -m ifc_handover query --repository out/handover.sqlite objects \
    --type IfcSpace
python3 -m ifc_handover query --repository out/handover.sqlite objects \
    --pset Pset_WallCommon --property-name Length --status missing_unit
python3 -m ifc_handover query --repository out/handover.sqlite object \
    --key 'gid:drill:0_RO_RV8HL0x40IIDUQjUx'
python3 -m ifc_handover report --repository out/handover.sqlite

# 3. Export a room/handover subset, then verify it independently.
python3 -m ifc_handover export --repository out/handover.sqlite \
    --root 'gid:drill:0_RO_RV8HL0x40IIDUQjUx' --dest out/package-room-1
python3 -m ifc_handover verify --package out/package-room-1
```

The manifest format is either a list or `{"sources": [...]}`:

```json
{
  "sources": [
    {"source_id": "mechanical", "path": "mech.ifc", "sha256": "…64 hex…"}
  ]
}
```

Relative paths are resolved against the manifest's directory.  A hash mismatch
fails that source loudly; partial business rows from a failed source are never
left behind.

## Package layout

```
package/
  handover.sqlite          # queryable tables (objects, properties, materials, …)
  tables/*.csv|json        # human-readable copies
  sources/*.ifc.excerpt    # exact STEP lines backing each business row
  README.md
```

`HandoverPackage` reopens a package read-only with no access to the original
repository or source paths.

## Memory model

One source file may be fully loaded by IfcOpenShell.  Its STEP text and
IfcOpenShell view are released before the next source starts; the durable store
is SQLite, and spatial paths are walked from parent edges on demand rather than
cached.

Resolved properties and source excerpts are the two tables that grow with file
size, so they are streamed into SQLite through a bounded buffer inside the
source's write transaction instead of being accumulated in Python lists.
Declaration equality uses a hashable key rather than per-property JSON.

Measured on this machine (single source, generated corpus, `resources.getrusage`):

| Source size | Properties | Peak RSS |
|---|---|---|
| ~2.7 MB | 24,000 | ~290 MiB |
| ~13.4 MB | 128,000 | ~345 MiB |

Most of the floor is the Python interpreter, IfcOpenShell and the raw STEP
index; peak grows sublinearly with property count.  Across sources, peak RSS
stays flat: the generated-corpus memory test compares 1 source with 8 sources of
the same shape and fails if peak growth exceeds 96 MiB.  `ifc_handover.memory.ingest_with_memory` records per-source
and total peak RSS and asserts no source view survives ingestion.
`build_large_corpus` generates a throw-away corpus for measurement; large
generated files are not committed.

## Tests

```bash
python3 run_tests.py            # 125 tests
python3 run_tests.py test_drill -v
```

`run_tests.py` works under `python3 -I` by explicitly adding the repository and
the local IfcOpenShell to `sys.path`.

The drill expectations were written by hand in
[`docs/drill-expectations.md`](docs/drill-expectations.md) and are asserted by
`tests/test_expectations.py`.

## Design notes and limits

* IfcOpenShell silently drops unresolvable references (for example a dangling
  `#999` becomes an empty list).  The raw STEP index is therefore the authority
  for reference completeness and source spelling; IfcOpenShell supplies schema
  attributes and the entity inventory.
* Values are read from the raw STEP arguments, not from Python floats, so
  `1000.` and `1E3` and conversion factors stay exact.
* Area/volume units are interpreted with the prefix raised to the dimension
  power; mass and length are linear.
* Units with an offset (e.g. Celsius) are recognised but not applied to the
  four supported dimensions; they are reported as unresolved rather than
  silently using the linear factor alone.
* `IfcPropertyBoundedValue`, `IfcPropertyTableValue`, `IfcPropertyListValue`,
  `IfcPropertyReferenceValue` and quantities are retained with their raw
  attributes (and unevaluated expressions) but not interpreted.
* A declaration whose raw argument count does not match the schema is refused
  rather than misread by position, and the mismatch is recorded as failure
  evidence.
