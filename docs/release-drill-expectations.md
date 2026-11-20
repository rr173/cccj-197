# Release verification drill — hand-written expectations

These expectations were written by hand before the verifier ran.  They are
asserted row by row by `tests/test_release.py` against the fixture generated
by `tests/release_fixtures.py`; nothing on this page is computed by the
verifier itself.

## Model

Source `model`:

```
project → site → building
                 ├─ storey A            ← zone-a root
                 │   ├─ room B  → E     ← zone-b root (room B)
                 │   └─ room B2 → F, E  (E is contained in both rooms:
                 │                       two valid paths, still one matter)
                 ├─ storey D            ← zone-d root
                 │   └─ room D1 → G, NH (no GlobalId),
                 │                DH1/DH2 (shared GlobalId),
                 │                ✗ dangling containment
                 ├─ storey E            ← zone-e root (empty)
                 └─ X (contained in the building, in no zone)
```

Source `other`: `storey C → E2`, where `E2` carries the **same GlobalId** as
`E` in source `model`.  `zone-c` is rooted at storey C.

Every zone requires matter `COMMISSION`, coverage `device_and_space`,
`min_signatories: 2`, device type `IfcBuildingElementProxy`.

## Timeline

| instant | value |
|---|---|
| `T` (business moment) | 2026-03-15T17:00:00Z |
| `R1` / `R2` / `R3` (recordings) | 10:00 / 14:00 / 16:00 |
| `K1` / `K2` / `K3` (visibility cutoffs) | 12:00 / 15:00 / 18:00 |
| `T2` (beta's moved validity start) | 2026-03-16T17:00:00Z |

Ledger (all valid from 2026-03-01 unless noted):

| signoff | rev | recorded | party | target | decision |
|---|---|---|---|---|---|
| SIG-FLOOR | 1 | R1 | alpha | storey A (space) | accepted |
| SIG-DEV-E | 1 | R1 | alpha | E (device) | accepted |
| SIG-WD | 1→2 | R1→R2 | gamma | F (device) | accepted → **withdrawn** |
| SIG-REJ | 1 | R1 | beta | E (device) | **rejected** |
| SIG-STRAY | 1 | R1 | alpha | X (device, no zone) | accepted |
| SIG-GHOST | 1 | R1 | alpha | nonexistent gid | accepted |
| SIG-FLOOR-B | 1 | R2 | beta | storey A (space) | accepted |
| SIG-FLOOR-B | 2 | R3 | beta | storey A (space) | accepted, **valid from T2** |

Checkpoints: `cp-old-cutoff` (T, K1), `cp-new-cutoff` (T, K2),
`cp-narrowed` (T, K3), `cp-validity-start` (T2, K3).

## Expected zone decisions

| zone | cp-old-cutoff | cp-new-cutoff | cp-narrowed | cp-validity-start |
|---|---|---|---|---|
| zone-a | held | **released** | held | **released** |
| zone-b | held | held | held | held |
| zone-c | held | held | held | held |
| zone-d | scope_unverified | scope_unverified | scope_unverified | scope_unverified |
| zone-e | empty_scope | empty_scope | empty_scope | empty_scope |

Reasoning:

* **cp-old-cutoff** — zone-a: E has only alpha (SIG-DEV-E + SIG-FLOOR, one
  party) → blocked; F has alpha + gamma (SIG-WD r1) → ready; zone held.
* **cp-new-cutoff** — beta's storey signoff completes E and F → zone-a
  released.  Zone-b still counts only SIG-DEV-E: the storey signoffs target
  an ancestor beyond the room-B root and are excluded `out_of_coverage`.
* **cp-narrowed** — SIG-FLOOR-B r2 (valid from T2) is the visible revision
  and is `out_of_validity` at T; r1 does not resurrect → zone-a loses beta.
* **cp-validity-start** — at T2 the revision is valid → zone-a released again.
* **zone-c** — SIG-DEV-E targets `(model, gidE)`; E2 is `(other, gidE)` and
  never borrows it → blocked everywhere.
* **zone-d** — the dangling containment edge, the GlobalId-less device NH and
  the duplicate-GlobalId pair DH1/DH2 are scope evidence; G is still a
  determined (blocked) matter; the damage does not affect the other zones.
* **zone-e** — confirmable but no matching devices → `empty_scope`.
* E is reachable from storey A through room B **and** through room B2; zone-a
  still produces exactly one matter record for E.

## Expected signoff classifications (excerpt)

| checkpoint | record | signoff | classification |
|---|---|---|---|
| cp-new-cutoff | zone-a / E | SIG-DEV-E, SIG-FLOOR, SIG-FLOOR-B | counted |
| cp-new-cutoff | zone-a / E | SIG-REJ | excluded / rejected |
| cp-new-cutoff | zone-a / F | SIG-WD | excluded / withdrawn |
| cp-new-cutoff | zone-b / E | SIG-FLOOR, SIG-FLOOR-B | excluded / out_of_coverage |
| cp-narrowed | zone-a / E, F | SIG-FLOOR-B | excluded / out_of_validity |
| cp-validity-start | zone-a / E, F | SIG-FLOOR-B | counted |

Anomalies at every checkpoint: `SIG-STRAY` → `no_candidate_association`,
`SIG-GHOST` → `invalid_target`.

## Corrupted-document run

A second ledger directory is byte-identical except that SIG-FLOOR-B r2 is
absent and `docs/floor-b-r1.pdf` is corrupted.  At `cp-new-cutoff` the
revision is visible, valid, accepted and covering, but the hash check fails:

* zone-a / E and zone-a / F: verified {alpha}, pending {beta} →
  **undetermined**; zone-a held;
* the file check is recorded as `hash_mismatch` with the computed digest.

## Ledger integrity

* Same id+revision with different content → the run is rejected
  (`LedgerError` naming the line) and the previous successful dataset at the
  destination is preserved untouched.
* Byte-identical replay of a line → deduplicated (replay count 1).
* Revision 3 recorded before revision 2 of the same id → rejected
  (revision/recorded order disagreement).
* An `accepted` revision without a document → rejected as a structural error.
