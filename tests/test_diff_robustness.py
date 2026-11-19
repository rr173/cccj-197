"""Robustness guarantees for the cross-version diff.

* input repositories are opened read-only and never modified;
* a failed comparison leaves no directory that looks complete;
* a repository that changes during comparison is rejected;
* STEP renumbering/reordering alone produces no business change;
* the change dataset is self-contained after being copied elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from diff_fixtures import build_diff_fixture
from fixtures import reorder_step, renumber_step, sha256_bytes
from ifc_handover.diff import ChangeDataset, build_diff
from ifc_handover.diff.dataset import is_complete_dataset
from ifc_handover.diff.snapshot import InputModifiedError, RepoSnapShot
from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import Repository


def _file_sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class DiffRobustnessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ifc-diff-robust-")
        self.fx = build_diff_fixture(os.path.join(self.tmp, "fixture"))
        self.old_repo = os.path.join(self.tmp, "old.sqlite")
        self.new_repo = os.path.join(self.tmp, "new.sqlite")
        for repo_path, source_id in (
            (self.old_repo, "period-old"),
            (self.new_repo, "period-new"),
        ):
            repo = Repository.create(repo_path)
            for entry in load_manifest(os.path.join(self.fx.root, "manifest.json")):
                if entry.source_id == source_id:
                    SourceIngestor(
                        repo,
                        entry,
                        store_dir=os.path.join(self.tmp, "store-" + source_id),
                    ).run()
            repo.close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_input_repositories_are_not_modified(self) -> None:
        before_old = _file_sha(self.old_repo)
        before_new = _file_sha(self.new_repo)
        build_diff(
            self.old_repo,
            self.new_repo,
            os.path.join(self.tmp, "out"),
            identity_mapping_path=self.fx.mapping_path,
        )
        self.assertEqual(before_old, _file_sha(self.old_repo))
        self.assertEqual(before_new, _file_sha(self.new_repo))

    def test_snapshot_opens_read_only_and_rejects_writes(self) -> None:
        snapshot = RepoSnapShot(self.old_repo, label="left")
        with self.assertRaises(sqlite3.OperationalError):
            snapshot.conn.execute(
                "INSERT INTO meta(key, value) VALUES('x', 'y')"
            )
        snapshot.close()

    def test_invalid_mapping_leaves_no_dataset(self) -> None:
        bad_mapping = os.path.join(self.tmp, "bad.json")
        with open(bad_mapping, "w", encoding="utf-8") as fh:
            json.dump({"mappings": []}, fh)
        # An empty mapping file is structurally valid, so craft a truly bad one.
        with open(bad_mapping, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "mappings": [
                        {
                            "left": {"source_id": "period-old", "global_id": "GHOST"},
                            "right": {
                                "source_id": "period-new",
                                "global_id": "GHOST2",
                            },
                            "rationale": "missing endpoints",
                        }
                    ]
                },
                fh,
            )
        dest = os.path.join(self.tmp, "should-not-exist")
        with self.assertRaises(Exception):
            build_diff(
                self.old_repo,
                self.new_repo,
                dest,
                identity_mapping_path=bad_mapping,
            )
        self.assertFalse(os.path.exists(dest))
        # No staging directory may be left behind in the parent either.
        leftovers = [
            name
            for name in os.listdir(self.tmp)
            if name.startswith(".ifc-diff-")
        ]
        self.assertEqual(leftovers, [])

    def test_input_change_after_snapshot_is_detected(self) -> None:
        snapshot = RepoSnapShot(self.old_repo, label="left")
        conn = sqlite3.connect(self.old_repo)
        conn.execute("INSERT INTO meta(key, value) VALUES('tamper', '1')")
        conn.commit()
        conn.close()
        with self.assertRaises(InputModifiedError):
            snapshot.assert_unchanged()
        snapshot.close()

    def test_input_change_during_comparison_is_rejected(self) -> None:
        """A mutation landing between periodic fingerprint checks aborts the run."""

        from ifc_handover.diff import engine as engine_module
        from ifc_handover.diff.snapshot import RepoSnapShot

        original = RepoSnapShot.assert_unchanged
        calls = {"n": 0}

        def guarded(self):
            calls["n"] += 1
            # Tamper on the second check, i.e. after work has begun.
            if calls["n"] == 2:
                conn = sqlite3.connect(self.path)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('midrun', 'x')"
                )
                conn.commit()
                conn.close()
            return original(self)

        RepoSnapShot.assert_unchanged = guarded
        engine_module.RepoSnapShot.assert_unchanged = guarded
        try:
            dest = os.path.join(self.tmp, "midrun-dataset")
            with self.assertRaises(InputModifiedError):
                build_diff(
                    self.old_repo,
                    self.new_repo,
                    dest,
                    identity_mapping_path=self.fx.mapping_path,
                )
            self.assertFalse(os.path.exists(dest))
        finally:
            RepoSnapShot.assert_unchanged = original
            engine_module.RepoSnapShot.assert_unchanged = original

    def test_fingerprint_stable_under_repack(self) -> None:
        snapshot_a = RepoSnapShot(self.old_repo, label="left")
        fingerprint = snapshot_a.start_fingerprint
        snapshot_a.close()
        # VACUUM repacks the file without changing business rows.
        conn = sqlite3.connect(self.old_repo)
        conn.execute("VACUUM")
        conn.close()
        snapshot_b = RepoSnapShot(self.old_repo, label="left")
        self.assertEqual(fingerprint, snapshot_b.start_fingerprint)
        snapshot_b.close()

    def test_step_renumber_and_reorder_alone_change_nothing(self) -> None:
        """A second repository of the same semantics with reshuffled STEP ids."""

        with open(self.fx.old_path, encoding="utf-8") as fh:
            original = fh.read()
        reordered = reorder_step(renumber_step(original, offset=10000))
        parallel_path = os.path.join(self.tmp, "period-reordered.ifc")
        with open(parallel_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(reordered)
        digest = sha256_bytes(reordered.encode("utf-8"))
        parallel_manifest = os.path.join(self.tmp, "manifest-reordered.json")
        with open(parallel_manifest, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "sources": [
                        {
                            "source_id": "period-old",
                            "path": parallel_path,
                            "sha256": digest,
                        }
                    ]
                },
                fh,
            )
        parallel_repo = os.path.join(self.tmp, "reordered.sqlite")
        repo = Repository.create(parallel_repo)
        for entry in load_manifest(parallel_manifest):
            result = SourceIngestor(
                repo,
                entry,
                store_dir=os.path.join(self.tmp, "store-reordered"),
            ).run()
            self.assertEqual(result.status, "ok", result.error)
        repo.close()
        dest = os.path.join(self.tmp, "reorder-diff")
        build_diff(self.old_repo, parallel_repo, dest)
        with ChangeDataset(dest) as dataset:
            # Identical business content: no semantic, provenance or identity
            # changes may be reported.
            self.assertEqual(dataset.summary()["change_count"], 0)

    def test_dataset_is_self_contained_after_copy(self) -> None:
        source_dest = os.path.join(self.tmp, "original-dataset")
        build_diff(
            self.old_repo,
            self.new_repo,
            source_dest,
            identity_mapping_path=self.fx.mapping_path,
        )
        copied = os.path.join(self.tmp, "copied-dataset")
        shutil.copytree(source_dest, copied)
        self.assertTrue(is_complete_dataset(copied))
        with ChangeDataset(copied) as dataset:
            self.assertTrue(dataset.summary()["change_count"] > 0)
            # JSONL inside the copy is independently readable.
            jsonl_rows = list(dataset.iter_jsonl())
            self.assertEqual(
                len(jsonl_rows), dataset.summary()["change_count"]
            )

    def test_incomplete_directory_is_not_opened_as_dataset(self) -> None:
        fake = os.path.join(self.tmp, "fake-dataset")
        os.makedirs(fake)
        db = os.path.join(fake, "changes.sqlite")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE meta(key TEXT, value TEXT)")
        conn.commit()
        conn.close()
        self.assertFalse(is_complete_dataset(fake))
        with self.assertRaises(Exception):
            ChangeDataset(fake)


if __name__ == "__main__":
    unittest.main()
