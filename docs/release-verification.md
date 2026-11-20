# Offline handover release verification

`ifc_handover release-verify` answers, for a set of verification checkpoints,
whether each acceptance zone in a fixed delivery scope may be released.  One
run reads exactly four inputs, all read-only:

| input | role |
|---|---|
| object repository | one ingested `ifc_handover` SQLite store; the fixed delivery scope for every checkpoint |
| rules file | JSON; zones with spatial roots, device types, matters, coverage modes and minimum signatory counts |
| signoff ledger | UTF-8 JSONL; one complete signoff revision per line |
| checkpoints | JSON; the `(effective_at, known_at)` pairs to evaluate |

It produces one **release dataset** directory that reopens read-only after
being copied elsewhere, with no access to the repository, the ledger or the
signoff attachments.

```bash
python3 -m ifc_handover release-verify \
    --repository out/handover.sqlite \
    --rules rules.json \
    --ledger ledger.jsonl \
    --checkpoints checkpoints.json \
    --dest out/release-dataset

python3 -m ifc_handover release-report --dataset out/release-dataset
python3 -m ifc_handover release-query --dataset out/release-dataset matters \
    --checkpoint cp-new-cutoff --zone zone-a
python3 -m ifc_handover release-query --dataset out/release-dataset signoffs \
    --classification excluded --checkpoint cp-new-cutoff
python3 -m ifc_handover release-export-jsonl --dataset out/release-dataset \
    --jsonl out/release.jsonl
```

Re-running the same command with the same fixed inputs reproduces the same
business records (matters are keyed by content hash and every table is
emitted in a stable order).

## Timestamp precision

All instants — ledger `recorded_at`/`valid_from`/`valid_to` and checkpoint
`effective_at`/`known_at` — are **whole-second UTC timestamps** written as
`YYYY-MM-DDTHH:MM:SSZ` with a mandatory `Z` suffix.  Numeric offsets and
fractional seconds are rejected as ledger/checkpoint structural errors.
Validity intervals are half-open: `[valid_from, valid_to)`; a missing
`valid_to` is open-ended.  Because the format is fixed and zero-padded, the
canonical strings compare chronologically.

## Rules file

```json
{
  "rules_version": 1,
  "zones": [
    {
      "zone_id": "zone-a",
      "root": {"source_id": "model", "global_id": "2vD7QDBfTIZODIwyH8tHxg"},
      "device_types": ["IfcBuildingElementProxy"],
      "matters": [
        {"code": "COMMISSION", "coverage": "device_and_space", "min_signatories": 2}
      ]
    }
  ]
}
```

* `root` identifies the spatial root by `(source_id, GlobalId)`.
* `device_types` are exact IFC entity type names; subtype expansion is never
  applied.
* `coverage` is `device` (device-specific signoffs only) or
  `device_and_space` (device-specific plus space signoffs).
* `min_signatories` is the number of independent signatory parties required.
  Unknown keys, duplicate zone ids and duplicate matter codes are structural
  errors and reject the run.

## Ledger (JSONL)

Each line is one complete revision:

```json
{"signoff_id": "SIG-FLOOR-B", "revision": 2,
 "recorded_at": "2026-03-15T16:00:00Z",
 "valid_from": "2026-03-16T17:00:00Z", "valid_to": null,
 "decision": "accepted", "signatory": "party-beta",
 "matter_code": "COMMISSION", "target_kind": "space",
 "target": {"source_id": "model", "global_id": "2vD7QDBfTIZODIwyH8tHxg"},
 "document": {"path": "docs/floor-b-r2.pdf", "sha256": "…64 hex…"}}
```

Integrity rules — any violation rejects the **entire run** and the error
names the 1-based ledger line:

* unknown or missing fields; non-positive revision; malformed timestamps;
  `valid_to <= valid_from`; a `decision` outside
  `accepted`/`rejected`/`withdrawn`; an `accepted` revision without a
  `document`; a document path that is absolute or escapes the ledger
  directory;
* the same `(signoff_id, revision)` repeated with **different** content
  (byte-identical repeats are replays and are deduplicated);
* within one `signoff_id`, `recorded_at` decreasing as the revision number
  grows.  Several revisions may share one recording instant; the revision
  number is then the order key.

## How a checkpoint is evaluated

For each checkpoint `(effective_at, known_at)`:

1. **Visibility.**  For every signoff id, the single newest revision with
   `recorded_at <= known_at` is selected.  Older revisions are never
   consulted again, so a withdrawal, rejection or narrowed validity can never
   resurrect an older revision — while a revision recorded later (but still
   before `known_at`) covers an earlier `effective_at` retroactively when its
   validity interval reaches back.
2. **Validity.**  The selected revision counts only if
   `valid_from <= effective_at < valid_to` (or `valid_to` is open).
3. **Coverage.**  A device signoff must target the device itself, by full
   `(source_id, GlobalId)` — the same GlobalId in another source never
   borrows the signoff.  A space signoff (only when the matter's coverage
   mode allows it) must target a spatial ancestor of the device over valid
   hierarchy edges, where the target and the whole path stay inside the zone;
   a storey signoff therefore covers every device of a storey-rooted zone but
   never crosses another zone's root.
4. **Document.**  A counted revision's document (relative to the ledger
   directory) is streamed and its SHA-256 must match the declared one.  File
   failures mark that signoff pending but never stop the run.

Classification priority per selected revision (first match wins):

| # | condition | classification |
|---|---|---|
| 1 | decision `rejected` / `withdrawn` | excluded (`rejected` / `withdrawn`) |
| 2 | validity interval misses `effective_at` | excluded (`out_of_validity`) |
| 3 | target identity resolves to several objects | pending (`coverage_unverified`) |
| 4 | target kind disagrees with the repository object | excluded (`invalid_target`) |
| 5 | coverage mode forbids space signoffs | excluded (`out_of_coverage`) |
| 6 | target beyond the zone root / outside the zone | excluded (`out_of_coverage`) |
| 7 | candidate path uses invalid edges | pending (`coverage_unverified`) |
| 8 | document missing | pending (`file_missing`) |
| 9 | document hash mismatch | pending (`file_hash_mismatch`) |
| 10 | otherwise | **counted** |

Signatories are deduplicated by their stable ledger id across all counted
revisions of a matter — one party's space signoff plus device signoff is
still one party.  A matter is `ready` when the verified parties reach
`min_signatories`; otherwise `undetermined` when verified + pending parties
together could reach it; otherwise `blocked`.  Devices without any signoff
record are listed as `blocked` matters, never silently dropped.

Visible revisions whose matter code matches a rule but that have no concrete
candidate association with any determined matter land in the **anomaly list**
(`invalid_target`, `ambiguous_target`, `no_candidate_association`).

## Zone scope and zone decision

Zone scope is computed once per run from the fixed model: the zone root must
resolve to one usable spatial object, and the downward walk over
decomposition/containment edges must find no missing endpoints, no cycle
conflicts and no device with a missing or conflicted GlobalId.  Invalid edges
reachable from the root are recorded with their known endpoints as evidence —
never hidden by looking at valid edges only — and damage outside a zone's
reach never affects that zone.  Only devices reached over valid edges with a
clear identity become determined matters.

Zone decisions per checkpoint:

| decision | meaning |
|---|---|
| `released` | scope confirmed, at least one matter, every matter `ready` |
| `held` | scope confirmed but some matter not `ready` (reasons listed) |
| `empty_scope` | scope confirmed but no device matches the rule (not a release) |
| `scope_unverified` | the scope itself cannot be confirmed (issues listed) |

## The release dataset

```
release-dataset/
  release.sqlite   # zones, matters, results, signoffs, anomalies, evidence, …
  release.jsonl    # zone decisions + matter results, one JSON object per line
  README.md        # format summary
```

Key tables: `checkpoints`, `zones` + `zone_scope_issues`, `matters` +
`matter_results` (per checkpoint), `matter_signoffs` (every classified
selected revision with its ledger `line_no`, rejection reason and document
check), `zone_decisions`, `anomalies`, `file_checks`, `evidence` (STEP
excerpts located to devices, spatial relations and scope issues),
`sources` (repository source summary), `ledger_revisions` (the parsed ledger
the decisions are bound to).  The `manifest` meta entry records the
repository fingerprint and the SHA-256 of the rules, ledger and checkpoint
files, plus the timestamp precision rule, so a copied dataset can be audited
without the original inputs.

## Safety and resource model

* The repository, rules, ledger and documents are never modified.  The
  repository is fingerprinted at the start and re-verified before
  publication; the rules/ledger/checkpoint files and every checked document
  are re-hashed before publication.  Any change raises `InputModifiedError`.
* Output is written to a hidden staging directory and atomically renamed only
  after the completion marker is committed.  A failed run — structural error
  or modified input — leaves the previous successful report untouched.
* The ledger is parsed once into an on-disk index; checkpoints are grouped by
  `known_at` and the visible revision set is advanced by a single streaming
  sweep.  Zone walks, coverage targets and results live in on-disk tables;
  Python memory stays bounded by one zone frontier and one matter's evidence,
  never by the device, revision or coverage totals (see
  `tests/test_release_memory.py`).

## Drill expectations

The hand-written expectations for the synthetic drill (two zones over device
E, late-recorded and validity-narrowed revisions, a corrupted document, a
cross-source GlobalId twin, a broken edge) live in
[`release-drill-expectations.md`](release-drill-expectations.md) and are
asserted row by row by `tests/test_release.py`.
