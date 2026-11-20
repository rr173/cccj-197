"""Scaled-fixture count and evidence checks for release verification.

The fixture is generated with a fixed seed at test time (nothing large is
committed).  Expected counts are closed-form from the construction and are
asserted per checkpoint; sampled STEP evidence is re-read from the dataset and
checked against the repository's own ``source_entities`` rows.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import helpers
from release_fixtures import build_scaled_fixture
from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.release import ReleaseDataset, build_release_verification
from ifc_handover.store import Repository


class ScaledFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="ifc-release-scaled-")
        cls.fx = build_scaled_fixture(
            os.path.join(cls.tmp, "fx"), seed=20261119, storeys=4, rooms=3, devices=5
        )
        cls.repo_path = os.path.join(cls.tmp, "repo.sqlite")
        repo = Repository.create(cls.repo_path)
        try:
            for entry in load_manifest(cls.fx["manifest"]):
                result = SourceIngestor(repo, entry).run()
                assert result.status == "ok", result.error
        finally:
            repo.close()
        cls.dataset_path = os.path.join(cls.tmp, "dataset")
        cls.manifest = build_release_verification(
            cls.repo_path,
            cls.fx["rules"],
            cls.fx["ledger"],
            cls.fx["checkpoints"],
            cls.dataset_path,
        )
        cls.expected = cls.fx["expected"]

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_matter_and_revision_totals(self):
        self.assertEqual(self.manifest["run"]["matters"], self.expected["matters"])
        self.assertEqual(self.manifest["ledger"]["revisions"], self.expected["revisions"])

    def test_checkpoint_counts(self):
        with ReleaseDataset(self.dataset_path) as ds:
            summary = ds.summary()
            for cp, expected in (
                ("sc-cp1", self.expected["cp1"]),
                ("sc-cp2", self.expected["cp2"]),
                ("sc-cp3", self.expected["cp3"]),
            ):
                statuses = summary["matter_statuses"][cp]
                self.assertEqual(statuses.get("blocked", 0), expected["blocked"], cp)
                self.assertEqual(statuses.get("ready", 0), expected["ready"], cp)
                self.assertEqual(statuses.get("undetermined", 0), expected["undetermined"], cp)
                decisions = summary["zone_decisions"][cp]
                self.assertEqual(
                    decisions.get(expected["zone_decision"], 0), self.fx["storeys"], cp
                )

    def test_withdrawn_devices_are_the_blocked_ones(self):
        with ReleaseDataset(self.dataset_path) as ds:
            blocked = {
                s["signoff_id"]
                for s in ds.signoffs(checkpoint="sc-cp3", classification="excluded")
                if s["reason"] == "withdrawn"
            }
            self.assertEqual(blocked, set(self.expected["withdrawn"]))
            # A withdrawn signoff never resurrects the older revision.
            sample = sorted(self.expected["withdrawn"])[0]
            rows = ds.signoffs(signoff_id=sample, checkpoint="sc-cp3")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["revision"], 2)
            self.assertEqual(rows[0]["classification"], "excluded")

    def test_sampled_source_evidence(self):
        """Sampled evidence rows match the repository's own STEP excerpts."""

        from ifc_handover.store import Repository as Repo

        with ReleaseDataset(self.dataset_path) as ds:
            matters = ds.matters(
                checkpoint="sc-cp2", device_global_id=self.expected["sample_device_gid"]
            )
            self.assertEqual(len(matters), 1)
            record = matters[0]
            self.assertEqual(record["status"], "ready")
            evidence = ds.evidence(owner_kind="matter", owner_id=record["record_id"])
            self.assertTrue(evidence)
            repo = Repo(self.repo_path)
            try:
                for row in evidence:
                    reference = repo.conn.execute(
                        "SELECT raw_text FROM source_entities WHERE source_id=? AND step_id=? AND role=?",
                        (row["source_id"], row["step_id"], row["role"]),
                    ).fetchone()
                    self.assertIsNotNone(reference)
                    self.assertEqual(row["raw_text"], reference[0])
                    self.assertEqual(row["entity_type"], "IFCBUILDINGELEMENTPROXY")
            finally:
                repo.close()
            # The storey signoff's coverage evidence names the storey and a relation.
            signoff_evidence = ds.evidence(owner_kind="signoff")
            storey_rows = [
                e for e in signoff_evidence if e["entity_type"] == "IFCBUILDINGSTOREY"
            ]
            self.assertTrue(storey_rows)
            relation_rows = [
                e for e in signoff_evidence if e["entity_type"].startswith("IFCREL")
            ]
            self.assertTrue(relation_rows)


if __name__ == "__main__":
    unittest.main()
