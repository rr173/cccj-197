"""Validation of the explicit one-to-one identity correspondence table.

Structural problems (duplicate rows, missing endpoints, many-to-one reuse,
missing rationale) abort the comparison with IdentityMappingError and write no
dataset.  A mapping that points at an unusable endpoint is not a structural
error: the endpoints exist, so the pair is reported as locally incomparable
with evidence.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from diff_fixtures import (
    LABEL_DUP,
    LABEL_MAPPED_NEW,
    LABEL_MAPPED_OLD,
    build_diff_fixture,
)
from fixtures import gid
from ifc_handover.diff import (
    CAT_INCOMPARABLE,
    ChangeDataset,
    IdentityMappingError,
    build_diff,
    load_identity_mapping,
)
from ifc_handover.diff.snapshot import RepoSnapShot
from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import Repository


class IdentityMappingTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="ifc-diff-map-")
        cls.fx = build_diff_fixture(os.path.join(cls.tmp, "fixture"))
        cls.old_repo = os.path.join(cls.tmp, "old.sqlite")
        cls.new_repo = os.path.join(cls.tmp, "new.sqlite")
        for repo_path, source_id in (
            (cls.old_repo, "period-old"),
            (cls.new_repo, "period-new"),
        ):
            repo = Repository.create(repo_path)
            for entry in load_manifest(os.path.join(cls.fx.root, "manifest.json")):
                if entry.source_id == source_id:
                    SourceIngestor(
                        repo,
                        entry,
                        store_dir=os.path.join(cls.tmp, "store-" + source_id),
                    ).run()
            repo.close()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _write_mapping(self, name: str, payload: object) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def _entry(self, left_gid: str, right_gid: str, rationale: str = "test") -> dict:
        return {
            "left": {"source_id": "period-old", "global_id": left_gid},
            "right": {"source_id": "period-new", "global_id": right_gid},
            "rationale": rationale,
        }

    def _load(self, path: str):
        left = RepoSnapShot(self.old_repo, label="left")
        right = RepoSnapShot(self.new_repo, label="right")
        try:
            return load_identity_mapping(path, left, right)
        finally:
            left.close()
            right.close()

    def test_valid_mapping_loads(self) -> None:
        path = self._write_mapping(
            "ok.json",
            {"mappings": [self._entry(gid(LABEL_MAPPED_OLD), gid(LABEL_MAPPED_NEW))]},
        )
        mapping = self._load(path)
        self.assertEqual(len(mapping.pairs), 1)

    def test_duplicate_mapping_row_rejected(self) -> None:
        entry = self._entry(gid(LABEL_MAPPED_OLD), gid(LABEL_MAPPED_NEW))
        path = self._write_mapping("dup.json", {"mappings": [entry, dict(entry)]})
        with self.assertRaises(IdentityMappingError) as ctx:
            self._load(path)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_missing_left_endpoint_rejected(self) -> None:
        path = self._write_mapping(
            "missing-left.json",
            {"mappings": [self._entry("DOES-NOT-EXIST", gid(LABEL_MAPPED_NEW))]},
        )
        with self.assertRaises(IdentityMappingError) as ctx:
            self._load(path)
        self.assertIn("does not exist", str(ctx.exception))

    def test_missing_right_endpoint_rejected(self) -> None:
        path = self._write_mapping(
            "missing-right.json",
            {"mappings": [self._entry(gid(LABEL_MAPPED_OLD), "NO-SUCH-GID")]},
        )
        with self.assertRaises(IdentityMappingError):
            self._load(path)

    def test_many_to_one_reused_right_endpoint_rejected(self) -> None:
        entries = [
            self._entry(gid(LABEL_MAPPED_OLD), gid(LABEL_MAPPED_NEW)),
            self._entry(gid("diff-eq-stable"), gid(LABEL_MAPPED_NEW)),
        ]
        path = self._write_mapping("m21.json", {"mappings": entries})
        with self.assertRaises(IdentityMappingError) as ctx:
            self._load(path)
        self.assertIn("one-to-one", str(ctx.exception))

    def test_reused_left_endpoint_rejected(self) -> None:
        entries = [
            self._entry(gid(LABEL_MAPPED_OLD), gid(LABEL_MAPPED_NEW)),
            self._entry(gid(LABEL_MAPPED_OLD), gid("diff-eq-stable")),
        ]
        path = self._write_mapping("m12.json", {"mappings": entries})
        with self.assertRaises(IdentityMappingError):
            self._load(path)

    def test_missing_rationale_rejected(self) -> None:
        entry = self._entry(gid(LABEL_MAPPED_OLD), gid(LABEL_MAPPED_NEW))
        entry["rationale"] = "   "
        path = self._write_mapping("norat.json", {"mappings": [entry]})
        with self.assertRaises(IdentityMappingError):
            self._load(path)

    def test_mapping_to_duplicate_global_id_is_locally_incomparable(self) -> None:
        # The endpoint exists but is unusable: the pair is reported, not rejected.
        path = self._write_mapping(
            "to-dup.json",
            {
                "mappings": [
                    self._entry(gid("diff-eq-stable"), gid(LABEL_DUP))
                ]
            },
        )
        # The mapping itself loads because the endpoint exists.
        mapping = self._load(path)
        self.assertEqual(len(mapping.pairs), 1)
        dest = os.path.join(self.tmp, "dataset-dup")
        build_diff(self.old_repo, self.new_repo, dest, identity_mapping_path=path)
        with ChangeDataset(dest) as dataset:
            rows = dataset.query(global_id=gid(LABEL_DUP), category=CAT_INCOMPARABLE)
            self.assertTrue(rows)
            self.assertTrue(
                any(
                    r["identity"]["kind"] == "explicit_mapping" for r in rows
                )
            )


if __name__ == "__main__":
    unittest.main()
