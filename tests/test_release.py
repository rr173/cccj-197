"""Release verification drill tests against hand-written expectations.

The expected zone decisions, matter statuses, participating signoff
classifications and anomaly lists were written by hand in
``tests/release_fixtures.py`` (see also ``docs/release-drill-expectations.md``).
These tests do not trust the verifier's own summary: every expectation is
located by checkpoint, zone and device and checked row by row.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import helpers
from fixtures import gid
from release_fixtures import (
    CP_NARROWED,
    CP_NEW,
    CP_OLD,
    CP_VALIDITY_START,
    DEVICE_E,
    DOC_FLOOR_B1,
    EXPECTED_ANOMALIES,
    EXPECTED_CORRUPTED_MATTERS,
    EXPECTED_CORRUPTED_ZONE_DECISIONS,
    EXPECTED_MATTERS,
    EXPECTED_SIGNOFFS,
    EXPECTED_ZONE_DECISIONS,
    MATTER,
    PARTY_ALPHA,
    PARTY_BETA,
    SIG_DEV_E,
    SIG_FLOOR,
    SIG_FLOOR_B,
    SIG_WD,
    STOREY_A,
    build_release_fixture,
    ledger_revisions,
)
from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.release import (
    LedgerError,
    ReleaseDataset,
    build_release_verification,
    is_complete_dataset,
)
from ifc_handover.release.dataset import (
    CLASS_COUNTED,
    CLASS_EXCLUDED,
    CLASS_PENDING,
    DECISION_RELEASED,
    DECISION_SCOPE_UNVERIFIED,
    REASON_FILE_HASH_MISMATCH,
    REASON_OUT_OF_COVERAGE,
)
from ifc_handover.store import Repository


def _ingest_repo(path: str, manifest_path: str) -> None:
    repo = Repository.create(path)
    try:
        for entry in load_manifest(manifest_path):
            result = SourceIngestor(repo, entry).run()
            assert result.status == "ok", result.error
    finally:
        repo.close()


class ReleaseDrillTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="ifc-release-test-")
        cls.fx = build_release_fixture(os.path.join(cls.tmp, "fx"))
        cls.repo_path = os.path.join(cls.tmp, "repo.sqlite")
        _ingest_repo(cls.repo_path, cls.fx.repo_manifest)
        cls.dataset_path = os.path.join(cls.tmp, "dataset")
        cls.manifest = build_release_verification(
            cls.repo_path,
            cls.fx.rules_path,
            cls.fx.ledger_path,
            cls.fx.checkpoints_path,
            cls.dataset_path,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _matters(self, ds, checkpoint):
        return {
            (m["zone_id"], m["device_name"]): m
            for m in ds.matters(checkpoint=checkpoint)
        }

    def test_zone_decisions_match_handwritten(self):
        with ReleaseDataset(self.dataset_path) as ds:
            decisions = {
                (d["checkpoint_idx"], d["zone_id"]): d["decision"]
                for d in ds.zone_decisions()
            }
            checkpoints = {cp["idx"]: cp["checkpoint_id"] for cp in ds.checkpoints()}
            for (idx, zone_id), decision in decisions.items():
                expected = EXPECTED_ZONE_DECISIONS[checkpoints[idx]][zone_id]
                self.assertEqual(
                    decision,
                    expected,
                    f"{checkpoints[idx]} {zone_id}: expected {expected}, got {decision}",
                )
            self.assertEqual(len(decisions), 4 * 5)

    def test_matter_statuses_match_handwritten(self):
        with ReleaseDataset(self.dataset_path) as ds:
            for (checkpoint, zone_id, device), (status, verified, pending) in EXPECTED_MATTERS.items():
                matters = self._matters(ds, checkpoint)
                matter = matters.get((zone_id, device))
                self.assertIsNotNone(matter, f"{checkpoint} {zone_id} {device} missing")
                self.assertEqual(matter["status"], status, f"{checkpoint} {zone_id} {device}")
                self.assertEqual(matter["verified_signatories"], verified)
                self.assertEqual(matter["pending_signatories"], pending)
                self.assertEqual(matter["matter_code"], MATTER)
                self.assertEqual(matter["min_signatories"], 2)

    def test_signoff_classifications_match_handwritten(self):
        with ReleaseDataset(self.dataset_path) as ds:
            for (checkpoint, zone_id, device), expected in EXPECTED_SIGNOFFS.items():
                matters = self._matters(ds, checkpoint)
                record_id = matters[(zone_id, device)]["record_id"]
                rows = {
                    s["signoff_id"]: (s["classification"], s["reason"])
                    for s in ds.signoffs(record_id=record_id, checkpoint=checkpoint)
                }
                self.assertEqual(
                    rows,
                    expected,
                    f"{checkpoint} {zone_id} {device} signoff classifications differ",
                )

    def test_anomalies_match_handwritten(self):
        with ReleaseDataset(self.dataset_path) as ds:
            for checkpoint, expected in EXPECTED_ANOMALIES.items():
                rows = {
                    a["signoff_id"]: a["reason"] for a in ds.anomalies(checkpoint=checkpoint)
                }
                self.assertEqual(rows, expected, f"{checkpoint} anomalies differ")

    def test_zone_d_scope_unverified_with_evidence(self):
        with ReleaseDataset(self.dataset_path) as ds:
            zones = {z["zone_id"]: z for z in ds.zones()}
            self.assertEqual(zones["zone-d"]["scope_status"], "unverified")
            self.assertEqual(zones["zone-d"]["device_count"], 1)  # G is determined
            issues = ds.scope_issues(zone="zone-d")
            kinds = sorted(i["issue_kind"] for i in issues)
            self.assertEqual(
                kinds,
                [
                    "device_identity_conflict",
                    "device_identity_conflict",
                    "device_identity_missing",
                    "edge_missing_endpoint",
                ],
            )
            edge = next(i for i in issues if i["issue_kind"] == "edge_missing_endpoint")
            self.assertEqual(edge["detail"]["edge_state"], "missing")
            self.assertIsNotNone(edge["detail"]["child_step_id"])
            self.assertIsNotNone(edge["relation_key"])
            conflict = next(i for i in issues if i["issue_kind"] == "device_identity_conflict")
            self.assertEqual(conflict["detail"]["conflict_state"], "duplicate_global_id")
            # The broken edge's STEP evidence is retained (raw STEP spelling).
            evidence = ds.evidence(owner_kind="scope_issue")
            self.assertTrue(
                any(e["entity_type"] == "IFCRELCONTAINEDINSPATIALSTRUCTURE" for e in evidence),
                "expected the broken containment relation in the evidence",
            )
            # Zone D's damage does not veto the other zones.
            decisions = {
                d["zone_id"]: d["decision"]
                for d in ds.zone_decisions(checkpoint=CP_NEW)
            }
            self.assertEqual(decisions["zone-a"], DECISION_RELEASED)
            self.assertEqual(decisions["zone-d"], DECISION_SCOPE_UNVERIFIED)

    def test_device_reached_twice_in_one_zone_is_one_matter(self):
        """E is contained in room B and room B2: still one record in zone-a."""

        with ReleaseDataset(self.dataset_path) as ds:
            matters = [
                m for m in ds.matters(zone="zone-a") if m["device_name"] == "E"
            ]
            self.assertEqual(len(matters), 1)
            zones = {z["zone_id"]: z for z in ds.zones()}
            self.assertEqual(zones["zone-a"]["device_count"], 2)  # E and F
            self.assertEqual(zones["zone-a"]["matter_count"], 2)

    def test_cross_source_signoff_isolation(self):
        """E's device signoff targets (model, gidE) and must not cover (other, gidE)."""

        with ReleaseDataset(self.dataset_path) as ds:
            matters = self._matters(ds, CP_NEW)
            other_record = matters[("zone-c", "E2")]
            self.assertEqual(other_record["device_source_id"], "other")
            self.assertEqual(other_record["device_global_id"], gid(DEVICE_E))
            self.assertEqual(other_record["status"], "blocked")
            self.assertEqual(other_record["verified_signatories"], [])
            rows = ds.signoffs(record_id=other_record["record_id"], checkpoint=CP_NEW)
            self.assertEqual(rows, [], "signoff leaked across sources")
            model_record = matters[("zone-a", "E")]
            ids = {s["signoff_id"] for s in ds.signoffs(record_id=model_record["record_id"], checkpoint=CP_NEW)}
            self.assertIn(SIG_DEV_E, ids)

    def test_out_of_coverage_visible_for_zone_b(self):
        """The storey signoff is considered for zone B and rejected as out of coverage."""

        with ReleaseDataset(self.dataset_path) as ds:
            matters = self._matters(ds, CP_NEW)
            record_id = matters[("zone-b", "E")]["record_id"]
            rows = {
                s["signoff_id"]: (s["classification"], s["reason"])
                for s in ds.signoffs(record_id=record_id, checkpoint=CP_NEW)
            }
            self.assertEqual(rows[SIG_FLOOR], (CLASS_EXCLUDED, REASON_OUT_OF_COVERAGE))
            self.assertEqual(rows[SIG_FLOOR_B], (CLASS_EXCLUDED, REASON_OUT_OF_COVERAGE))

    def test_ledger_revisions_and_source_lines_queryable(self):
        with ReleaseDataset(self.dataset_path) as ds:
            revisions = {r["signoff_id"]: r for r in ds.revisions() if r["revision"] == 1}
            self.assertIn(SIG_FLOOR_B, revisions)
            floor_b = {r["revision"]: r for r in ds.revisions(signoff_id=SIG_FLOOR_B)}
            self.assertEqual(set(floor_b), {1, 2})
            self.assertEqual(floor_b[2]["valid_from"], "2026-03-16T17:00:00Z")
            # The participating revision's ledger source line is on the row.
            matters = self._matters(ds, CP_NARROWED)
            record_id = matters[("zone-a", "E")]["record_id"]
            rows = {s["signoff_id"]: s for s in ds.signoffs(record_id=record_id, checkpoint=CP_NARROWED)}
            self.assertEqual(rows[SIG_FLOOR_B]["reason"], "out_of_validity")
            self.assertGreater(rows[SIG_FLOOR_B]["line_no"], 0)
            self.assertEqual(rows[SIG_FLOOR_B]["revision"], 2)

    def test_file_checks_recorded(self):
        with ReleaseDataset(self.dataset_path) as ds:
            checks = {c["path"]: c for c in ds.file_checks()}
            self.assertIn("docs/floor-a-r1.pdf", checks)
            for check in checks.values():
                self.assertEqual(check["status"], "ok")
                self.assertEqual(check["computed_sha256"], check["declared_sha256"])

    def test_step_evidence_located_to_device_and_edges(self):
        with ReleaseDataset(self.dataset_path) as ds:
            matters = self._matters(ds, CP_NEW)
            record_id = matters[("zone-a", "E")]["record_id"]
            # Device entity evidence on the matter itself (raw STEP spelling).
            matter_evidence = ds.evidence(owner_kind="matter", owner_id=record_id)
            self.assertTrue(
                any(e["entity_type"] == "IFCBUILDINGELEMENTPROXY" for e in matter_evidence)
            )
            # Space signoff evidence: storey entity + spatial relation entities.
            signoff_evidence = ds.evidence(
                owner_kind="signoff", owner_id=f"signoff:{record_id}:{SIG_FLOOR}"
            )
            types = {e["entity_type"] for e in signoff_evidence}
            self.assertIn("IFCBUILDINGSTOREY", types)
            self.assertTrue(
                {"IFCRELAGGREGATES", "IFCRELCONTAINEDINSPATIALSTRUCTURE"} & types,
                f"expected a spatial relation in the coverage evidence, got {types}",
            )
            for row in signoff_evidence:
                self.assertTrue(row["raw_text"].startswith("#"))
                self.assertGreater(row["line_start"], 0)

    def test_manifest_binds_fixed_inputs(self):
        with ReleaseDataset(self.dataset_path) as ds:
            manifest = ds.manifest()
            self.assertEqual(manifest["rules"]["sha256"], self._sha(self.fx.rules_path))
            self.assertEqual(manifest["ledger"]["sha256"], self._sha(self.fx.ledger_path))
            self.assertEqual(
                manifest["checkpoints_file"]["sha256"], self._sha(self.fx.checkpoints_path)
            )
            self.assertEqual(len(manifest["repository"]["fingerprint"]), 64)
            self.assertIn("whole-second UTC", manifest["timestamp_precision"])
            self.assertEqual(manifest["ledger"]["revisions"], 9)
            self.assertEqual(ds.rules()["zones"][0]["zone_id"], "zone-a")

    @staticmethod
    def _sha(path):
        import hashlib

        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def test_dataset_reopens_after_move(self):
        moved = os.path.join(self.tmp, "dataset-moved")
        shutil.copytree(self.dataset_path, moved)
        with ReleaseDataset(self.dataset_path) as original, ReleaseDataset(moved) as copy:
            self.assertEqual(original.zone_decisions(), copy.zone_decisions())
            self.assertEqual(
                original.matters(checkpoint=CP_NEW), copy.matters(checkpoint=CP_NEW)
            )
            self.assertEqual(original.manifest()["ledger"], copy.manifest()["ledger"])

    def test_jsonl_export_matches_tables(self):
        with ReleaseDataset(self.dataset_path) as ds:
            lines = list(ds.iter_jsonl())
            decisions = [l for l in lines if l["kind"] == "zone_decision"]
            matters = [l for l in lines if l["kind"] == "matter"]
            self.assertEqual(len(decisions), 4 * 5)
            self.assertEqual(len(matters), 4 * 5)
            released = [
                l for l in decisions if l["decision"] == "released"
            ]
            self.assertEqual(
                {(l["checkpoint_id"], l["zone_id"]) for l in released},
                {(CP_NEW, "zone-a"), (CP_VALIDITY_START, "zone-a")},
            )


class ReleaseCorruptedLedgerTestCase(unittest.TestCase):
    """Beta's completing document is corrupted: zone A becomes undetermined."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="ifc-release-corrupt-")
        cls.fx = build_release_fixture(os.path.join(cls.tmp, "fx"))
        cls.repo_path = os.path.join(cls.tmp, "repo.sqlite")
        _ingest_repo(cls.repo_path, cls.fx.repo_manifest)
        cls.dataset_path = os.path.join(cls.tmp, "dataset-corrupt")
        build_release_verification(
            cls.repo_path,
            cls.fx.rules_path,
            cls.fx.corrupted_ledger_path,
            cls.fx.checkpoints_path,
            cls.dataset_path,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_zone_a_undetermined_at_new_cutoff(self):
        with ReleaseDataset(self.dataset_path) as ds:
            matters = {
                (m["zone_id"], m["device_name"]): m
                for m in ds.matters(checkpoint=CP_NEW)
            }
            for (checkpoint, zone_id, device), (status, verified, pending) in EXPECTED_CORRUPTED_MATTERS.items():
                matter = matters[(zone_id, device)]
                self.assertEqual(matter["status"], status, f"{zone_id} {device}")
                self.assertEqual(matter["verified_signatories"], verified)
                self.assertEqual(matter["pending_signatories"], pending)
            decisions = {
                d["zone_id"]: d["decision"] for d in ds.zone_decisions(checkpoint=CP_NEW)
            }
            for zone_id, decision in EXPECTED_CORRUPTED_ZONE_DECISIONS.items():
                self.assertEqual(decisions[zone_id], decision, zone_id)

    def test_hash_mismatch_recorded(self):
        with ReleaseDataset(self.dataset_path) as ds:
            checks = {c["path"]: c for c in ds.file_checks()}
            corrupted = checks[DOC_FLOOR_B1]
            self.assertEqual(corrupted["status"], "hash_mismatch")
            self.assertNotEqual(corrupted["computed_sha256"], corrupted["declared_sha256"])
            matters = {
                (m["zone_id"], m["device_name"]): m
                for m in ds.matters(checkpoint=CP_NEW)
            }
            record_id = matters[("zone-a", "E")]["record_id"]
            rows = {
                s["signoff_id"]: s
                for s in ds.signoffs(record_id=record_id, checkpoint=CP_NEW)
            }
            self.assertEqual(rows[SIG_FLOOR_B]["classification"], CLASS_PENDING)
            self.assertEqual(rows[SIG_FLOOR_B]["reason"], REASON_FILE_HASH_MISMATCH)
            self.assertEqual(rows[SIG_FLOOR_B]["doc_status"], "hash_mismatch")


class LedgerIntegrityTests(unittest.TestCase):
    """Ledger structural errors reject the run and preserve the last report."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ifc-release-ledger-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fx = build_release_fixture(os.path.join(self.tmp, "fx"))
        self.repo_path = os.path.join(self.tmp, "repo.sqlite")
        _ingest_repo(self.repo_path, self.fx.repo_manifest)

    def _write_ledger(self, rows, name="ledger.jsonl"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        return path

    def _base_rows(self):
        return ledger_revisions()

    def test_conflicting_revisions_rejected_and_previous_report_preserved(self):
        dest = os.path.join(self.tmp, "dataset")
        build_release_verification(
            self.repo_path, self.fx.rules_path, self.fx.ledger_path,
            self.fx.checkpoints_path, dest,
        )
        self.assertTrue(is_complete_dataset(dest))
        rows = self._base_rows()
        conflict = dict(rows[0])
        conflict["signatory"] = "party-other"  # same id+revision, different content
        rows.append(conflict)
        bad_ledger = self._write_ledger(rows)
        with self.assertRaises(LedgerError) as ctx:
            build_release_verification(
                self.repo_path, self.fx.rules_path, bad_ledger,
                self.fx.checkpoints_path, dest,
            )
        self.assertIn("conflicting content", str(ctx.exception))
        self.assertIn(str(len(rows)), str(ctx.exception))  # offending line located
        # The previous successful report is untouched.
        self.assertTrue(is_complete_dataset(dest))
        with ReleaseDataset(dest) as ds:
            decisions = {
                (d["zone_id"]): d["decision"] for d in ds.zone_decisions(checkpoint=CP_NEW)
            }
            self.assertEqual(decisions["zone-a"], "released")

    def test_identical_replay_is_deduplicated(self):
        rows = self._base_rows()
        rows.append(dict(rows[0]))  # byte-identical replay
        ledger = self._write_ledger(rows)
        dest = os.path.join(self.tmp, "dataset-replay")
        manifest = build_release_verification(
            self.repo_path, self.fx.rules_path, ledger, self.fx.checkpoints_path, dest,
        )
        self.assertEqual(manifest["ledger"]["revisions"], 9)
        self.assertEqual(manifest["ledger"]["replays"], 1)

    def test_revision_recorded_order_inversion_rejected(self):
        rows = self._base_rows()
        moved = dict(rows[6])  # SIG-FLOOR-B r1 recorded at R2
        moved["revision"] = 3
        moved["recorded_at"] = "2026-03-15T08:00:00Z"  # before r2's recording
        rows.append(moved)
        ledger = self._write_ledger(rows)
        with self.assertRaises(LedgerError) as ctx:
            build_release_verification(
                self.repo_path, self.fx.rules_path, ledger,
                self.fx.checkpoints_path, os.path.join(self.tmp, "d1"),
            )
        self.assertIn("revision order and recorded order disagree", str(ctx.exception))

    def test_accepted_revision_requires_document(self):
        rows = self._base_rows()
        bad = dict(rows[0])
        bad["signoff_id"] = "SIG-NODOC"
        del bad["document"]
        rows.append(bad)
        ledger = self._write_ledger(rows)
        with self.assertRaises(LedgerError) as ctx:
            build_release_verification(
                self.repo_path, self.fx.rules_path, ledger,
                self.fx.checkpoints_path, os.path.join(self.tmp, "d2"),
            )
        self.assertIn("must carry a document", str(ctx.exception))

    def test_bad_timestamp_rejected_with_line(self):
        rows = self._base_rows()
        rows[0]["recorded_at"] = "2026-03-15 10:00:00"  # not the Z format
        ledger = self._write_ledger(rows)
        with self.assertRaises(LedgerError) as ctx:
            build_release_verification(
                self.repo_path, self.fx.rules_path, ledger,
                self.fx.checkpoints_path, os.path.join(self.tmp, "d3"),
            )
        self.assertIn("ledger line 1", str(ctx.exception))

    def test_structural_rules_error(self):
        rules_path = os.path.join(self.tmp, "bad-rules.json")
        with open(rules_path, "w", encoding="utf-8") as fh:
            json.dump({"zones": [{"zone_id": "z", "root": {"source_id": "model"}}]}, fh)
        from ifc_handover.release import RulesError

        with self.assertRaises(RulesError):
            build_release_verification(
                self.repo_path, rules_path, self.fx.ledger_path,
                self.fx.checkpoints_path, os.path.join(self.tmp, "d4"),
            )

    def test_same_instant_revisions_ordered_by_revision_number(self):
        """Two revisions recorded at one instant: the higher revision wins."""

        rows = self._base_rows()
        r1 = dict(rows[1])  # SIG-DEV-E r1 as a template
        r1["signoff_id"] = "SIG-SAME"
        r1["signatory"] = "party-delta"
        r2 = dict(r1)
        r2["revision"] = 2
        r2["decision"] = "withdrawn"
        r2.pop("document", None)
        rows.extend([r1, r2])
        ledger = self._write_ledger(rows)
        dest = os.path.join(self.tmp, "dataset-same")
        build_release_verification(
            self.repo_path, self.fx.rules_path, ledger, self.fx.checkpoints_path, dest,
        )
        with ReleaseDataset(dest) as ds:
            matters = {
                (m["zone_id"], m["device_name"]): m
                for m in ds.matters(checkpoint=CP_OLD)
            }
            record = matters[("zone-a", "E")]
            self.assertNotIn("party-delta", record["verified_signatories"])
            signoffs = {
                s["signoff_id"]: s
                for s in ds.signoffs(record_id=record["record_id"], checkpoint=CP_OLD)
            }
            self.assertEqual(signoffs["SIG-SAME"]["revision"], 2)
            self.assertEqual(signoffs["SIG-SAME"]["classification"], "excluded")
            self.assertEqual(signoffs["SIG-SAME"]["reason"], "withdrawn")


class ReleaseRobustnessTests(unittest.TestCase):
    """Input modification and failure safety for the release run."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ifc-release-robust-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fx = build_release_fixture(os.path.join(self.tmp, "fx"))
        self.repo_path = os.path.join(self.tmp, "repo.sqlite")
        _ingest_repo(self.repo_path, self.fx.repo_manifest)

    def _successful_run(self, dest: str) -> None:
        build_release_verification(
            self.repo_path, self.fx.rules_path, self.fx.ledger_path,
            self.fx.checkpoints_path, dest,
        )

    def test_ledger_change_mid_run_rejected_and_report_preserved(self) -> None:
        from ifc_handover.diff.snapshot import InputModifiedError, RepoSnapShot

        dest = os.path.join(self.tmp, "dataset")
        self._successful_run(dest)
        self.assertTrue(is_complete_dataset(dest))

        original = RepoSnapShot.assert_unchanged
        calls = {"n": 0}

        def guarded(snapshot):
            calls["n"] += 1
            if calls["n"] == 1:
                # Tamper with the ledger after the run has fully loaded it.
                with open(self.fx.ledger_path, "a", encoding="utf-8") as fh:
                    fh.write("\n")
            return original(snapshot)

        import ifc_handover.release.verify as verify_module

        verify_module.RepoSnapShot.assert_unchanged = guarded
        try:
            with self.assertRaises(InputModifiedError):
                self._successful_run(dest)
        finally:
            verify_module.RepoSnapShot.assert_unchanged = original
        # The previous successful report is untouched and still opens.
        self.assertTrue(is_complete_dataset(dest))
        with ReleaseDataset(dest) as ds:
            decisions = {
                d["zone_id"]: d["decision"] for d in ds.zone_decisions(checkpoint=CP_NEW)
            }
            self.assertEqual(decisions["zone-a"], "released")
        # No staging directory is left behind.
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".ifc-release-")]
        self.assertEqual(leftovers, [])

    def test_repository_change_mid_run_rejected(self) -> None:
        import sqlite3

        from ifc_handover.diff.snapshot import InputModifiedError, RepoSnapShot

        original = RepoSnapShot.assert_unchanged

        def tamper(snapshot):
            conn = sqlite3.connect(self.repo_path)
            conn.execute("INSERT INTO meta(key, value) VALUES('midrun', 'x')")
            conn.commit()
            conn.close()
            return original(snapshot)

        import ifc_handover.release.verify as verify_module

        verify_module.RepoSnapShot.assert_unchanged = tamper
        try:
            with self.assertRaises(InputModifiedError):
                self._successful_run(os.path.join(self.tmp, "d2"))
        finally:
            verify_module.RepoSnapShot.assert_unchanged = original
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "d2")))

    def test_empty_ledger_blocks_everything(self) -> None:
        ledger = os.path.join(self.tmp, "empty.jsonl")
        with open(ledger, "w", encoding="utf-8"):
            pass
        dest = os.path.join(self.tmp, "dataset-empty")
        manifest = build_release_verification(
            self.repo_path, self.fx.rules_path, ledger, self.fx.checkpoints_path, dest,
        )
        self.assertEqual(manifest["ledger"]["revisions"], 0)
        with ReleaseDataset(dest) as ds:
            for matter in ds.matters(checkpoint=CP_NEW):
                self.assertEqual(matter["status"], "blocked")
                self.assertEqual(matter["verified_signatories"], [])
            decisions = {
                d["zone_id"]: d["decision"] for d in ds.zone_decisions(checkpoint=CP_NEW)
            }
            self.assertEqual(decisions["zone-a"], "held")
            self.assertEqual(decisions["zone-e"], "empty_scope")

    def test_failed_run_leaves_no_staging(self) -> None:
        rows = ledger_revisions()
        conflict = dict(rows[0])
        conflict["signatory"] = "party-other"
        rows.append(conflict)
        bad = os.path.join(self.tmp, "bad.jsonl")
        with open(bad, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        with self.assertRaises(LedgerError):
            build_release_verification(
                self.repo_path, self.fx.rules_path, bad,
                self.fx.checkpoints_path, os.path.join(self.tmp, "d3"),
            )
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".ifc-release-")]
        self.assertEqual(leftovers, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "d3")))


class ReleasePendingCoverageTests(unittest.TestCase):
    """The two pending-coverage variants: identity conflict and invalid edges."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ifc-release-pending-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, builder, name):
        fx = builder(os.path.join(self.tmp, name))
        repo_path = os.path.join(self.tmp, f"{name}.sqlite")
        _ingest_repo(repo_path, fx["manifest"])
        dest = os.path.join(self.tmp, f"{name}-ds")
        build_release_verification(
            repo_path, fx["rules"], fx["ledger"], fx["checkpoints"], dest
        )
        return dest

    def test_identity_conflict_target_is_pending(self):
        from release_fixtures import build_identity_conflict_fixture

        dest = self._run(build_identity_conflict_fixture, "dup")
        with ReleaseDataset(dest) as ds:
            matters = ds.matters(checkpoint="pc-cp")
            self.assertEqual(len(matters), 1)
            matter = matters[0]
            self.assertEqual(matter["status"], "undetermined")
            self.assertEqual(matter["verified_signatories"], ["party-alpha"])
            self.assertEqual(matter["pending_signatories"], ["party-beta"])
            rows = {
                s["signoff_id"]: s
                for s in ds.signoffs(record_id=matter["record_id"], checkpoint="pc-cp")
            }
            dup = rows["SIG-PC-DUP"]
            self.assertEqual(dup["classification"], "pending")
            self.assertEqual(dup["reason"], "coverage_unverified")
            self.assertIn("2 repository objects", dup["reason_detail"])
            # Both conflicting rows are retained as STEP evidence.
            evidence = ds.evidence(
                owner_kind="signoff",
                owner_id=f"signoff:{matter['record_id']}:SIG-PC-DUP",
            )
            room_rows = [e for e in evidence if e["entity_type"] == "IFCSPACE"]
            self.assertEqual(len(room_rows), 2)

    def test_invalid_edge_candidate_path_is_pending(self):
        from release_fixtures import build_candidate_path_fixture

        dest = self._run(build_candidate_path_fixture, "cand")
        with ReleaseDataset(dest) as ds:
            zones = {z["zone_id"]: z for z in ds.zones()}
            # The cycle marks the zone scope unverified, but the device is
            # still a determined matter and the signoff is evaluated.
            self.assertEqual(zones["zone-pc"]["scope_status"], "unverified")
            matters = ds.matters(checkpoint="pc-cp")
            self.assertEqual(len(matters), 1)
            matter = matters[0]
            self.assertEqual(matter["status"], "undetermined")
            self.assertEqual(matter["verified_signatories"], ["party-alpha"])
            self.assertEqual(matter["pending_signatories"], ["party-beta"])
            rows = {
                s["signoff_id"]: s
                for s in ds.signoffs(record_id=matter["record_id"], checkpoint="pc-cp")
            }
            cand = rows["SIG-PC-CAND"]
            self.assertEqual(cand["classification"], "pending")
            self.assertEqual(cand["reason"], "coverage_unverified")
            # The candidate path is recorded with repository coordinates and
            # shows the conflict edge.
            path = cand["coverage_path"]
            self.assertIsNotNone(path)
            self.assertFalse(path["complete_ok"])
            states = {hop["edge_state"] for hop in path["hops"]}
            self.assertIn("conflict", states)
            for hop in path["hops"]:
                self.assertIsNotNone(hop["relation_key"])
                self.assertIsNotNone(hop["parent_step_id"])
            # The zone decision stays scope_unverified with the cycle issues.
            decisions = {d["zone_id"]: d for d in ds.zone_decisions(checkpoint="pc-cp")}
            self.assertEqual(decisions["zone-pc"]["decision"], "scope_unverified")
            issue_kinds = {i["issue_kind"] for i in ds.scope_issues(zone="zone-pc")}
            self.assertIn("edge_conflict", issue_kinds)


class ReleaseCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="ifc-release-cli-")
        cls.fx = build_release_fixture(os.path.join(cls.tmp, "fx"))
        cls.repo_path = os.path.join(cls.tmp, "repo.sqlite")
        _ingest_repo(cls.repo_path, cls.fx.repo_manifest)
        cls.dataset_path = os.path.join(cls.tmp, "dataset")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_cli_roundtrip(self):
        from ifc_handover.cli import main

        rc = main(
            [
                "release-verify",
                "--repository", self.repo_path,
                "--rules", self.fx.rules_path,
                "--ledger", self.fx.ledger_path,
                "--checkpoints", self.fx.checkpoints_path,
                "--dest", self.dataset_path,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(main(["release-report", "--dataset", self.dataset_path]), 0)
        self.assertEqual(
            main(
                [
                    "release-query", "--dataset", self.dataset_path, "matters",
                    "--checkpoint", CP_NEW, "--zone", "zone-a",
                ]
            ),
            0,
        )
        self.assertEqual(
            main(
                [
                    "release-query", "--dataset", self.dataset_path, "signoffs",
                    "--classification", "excluded", "--checkpoint", CP_NEW,
                ]
            ),
            0,
        )
        out_jsonl = os.path.join(self.tmp, "export.jsonl")
        self.assertEqual(
            main(
                [
                    "release-export-jsonl", "--dataset", self.dataset_path,
                    "--jsonl", out_jsonl, "--checkpoint", CP_NEW,
                ]
            ),
            0,
        )
        with open(out_jsonl, encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        self.assertTrue(lines)
        self.assertTrue(all(l["checkpoint_id"] == CP_NEW for l in lines))


if __name__ == "__main__":
    unittest.main()
