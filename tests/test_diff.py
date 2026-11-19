"""Cross-version diff tests against the hand-authored two-period fixture.

The expected change table is written by hand in
``diff_fixtures.handwritten_expectations``.  These tests do not trust the
diff's own summary: each expected scenario is located by GlobalId and checked
row by row, and every change record's evidence is independently re-read from
the source repositories' ``source_entities`` declarations.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from diff_fixtures import (
    LABEL_BROKEN,
    LABEL_DUP,
    LABEL_MAPPED_NEW,
    LABEL_MAPPED_OLD,
    LABEL_MOVED,
    LABEL_NULL,
    LABEL_PROV,
    LABEL_UNIT,
    LABEL_UNMAPPED_NEW,
    LABEL_UNMAPPED_OLD,
    LABEL_WALL_MAT,
    ROOM_A,
    ROOM_B,
    build_diff_fixture,
)
from fixtures import gid
from ifc_handover.diff import (
    CAT_ADDED,
    CAT_INCOMPARABLE,
    CAT_PROVENANCE_CHANGE,
    CAT_REMOVED,
    CAT_SEMANTIC_CHANGE,
    ChangeDataset,
    build_diff,
)
from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import Repository


class DiffFixtureTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base_tmp = tempfile.mkdtemp(prefix="ifc-diff-test-")
        cls.fixture_root = os.path.join(cls.base_tmp, "fixture")
        cls.fx = build_diff_fixture(cls.fixture_root)
        cls.old_repo_path = os.path.join(cls.base_tmp, "old.sqlite")
        cls.new_repo_path = os.path.join(cls.base_tmp, "new.sqlite")
        cls.dataset_path = os.path.join(cls.base_tmp, "changes")
        for repo_path, source_id in (
            (cls.old_repo_path, "period-old"),
            (cls.new_repo_path, "period-new"),
        ):
            repo = Repository.create(repo_path)
            for entry in load_manifest(os.path.join(cls.fixture_root, "manifest.json")):
                if entry.source_id == source_id:
                    result = SourceIngestor(
                        repo,
                        entry,
                        store_dir=os.path.join(cls.base_tmp, "store-" + source_id),
                    ).run()
                    assert result.status == "ok", result.error
            repo.close()
        cls.manifest = build_diff(
            cls.old_repo_path,
            cls.new_repo_path,
            cls.dataset_path,
            identity_mapping_path=cls.fx.mapping_path,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.base_tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.dataset = ChangeDataset(self.dataset_path)
        self.addCleanup(self.dataset.close)

    # -- hand-written table coverage ------------------------------------
    def test_expected_scenario_count(self) -> None:
        with open(self.fx.expectations_path, encoding="utf-8") as fh:
            expected = json.load(fh)
        self.assertEqual(len(expected["scenarios"]), 11)

    def test_every_handwritten_scenario_matches_actual_rows(self) -> None:
        """Reconcile the whole hand-written table against the dataset output."""

        from diff_fixtures import handwritten_expectations

        for scenario in handwritten_expectations()["scenarios"]:
            labels = [
                label
                for label in (
                    scenario.get("object_label"),
                    scenario.get("object_label_old"),
                    scenario.get("object_label_new"),
                )
                if label
            ]
            actual: list[tuple[str, str]] = []
            for label in labels:
                actual.extend(
                    (r["category"], r["aspect"])
                    for r in self.dataset.query(global_id=gid(label))
                )
            wanted = sorted(
                (entry["category"], entry["aspect"])
                for entry in scenario["expected_records"]
            )
            self.assertEqual(
                sorted(actual),
                wanted,
                f"scenario {scenario['scenario']} does not match its expectation",
            )

    def test_unit_rewrite_is_not_a_business_change(self) -> None:
        # 2 m vs 2000 mm normalise to the same SI value.
        rows = self.dataset.query(global_id=gid(LABEL_UNIT))
        self.assertEqual(rows, [])

    def test_type_to_instance_is_provenance_only(self) -> None:
        rows = self.dataset.query(global_id=gid(LABEL_PROV))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["category"], CAT_PROVENANCE_CHANGE)
        self.assertEqual(row["aspect"], "property")
        self.assertEqual(
            row["before_provenance"]["scope_used"], "type"
        )
        self.assertEqual(
            row["after_provenance"]["scope_used"], "instance"
        )
        # The effective business value is identical on both sides.
        self.assertEqual(row["before"], row["after"])

    def test_explicit_null_replaces_value_with_distinct_states(self) -> None:
        rows = self.dataset.query(global_id=gid(LABEL_NULL))
        pressure = [
            r for r in rows if r["subject"].endswith(
                "Pressure\x1fIfcPropertySingleValue"
            )
        ]
        self.assertEqual(len(pressure), 1)
        row = pressure[0]
        self.assertEqual(row["category"], CAT_SEMANTIC_CHANGE)
        self.assertEqual(row["before"]["state"], "value")
        # Explicit null is a distinct state, never folded to "" or 0.
        self.assertEqual(row["after"]["state"], "explicit_null")
        self.assertNotEqual(row["after"]["state"], "absent")

    def test_room_move_is_located_to_the_spatial_edge(self) -> None:
        rows = self.dataset.query(global_id=gid(LABEL_MOVED))
        spatial = [r for r in rows if r["aspect"] == "spatial_relation"]
        self.assertEqual(len(spatial), 2)
        directions = {r["locator"]["direction"] for r in spatial}
        self.assertEqual(directions, {"parent_added", "parent_removed"})
        removed = next(r for r in spatial
                       if r["locator"]["direction"] == "parent_removed")
        added = next(r for r in spatial
                     if r["locator"]["direction"] == "parent_added")
        parent_gids = {
            removed["before"]["parent"]["global_id"],
            added["after"]["parent"]["global_id"],
        }
        self.assertEqual(parent_gids, {gid(ROOM_A), gid(ROOM_B)})
        for row in spatial:
            self.assertIn("relation_type", row["before"] or row["after"])

    def test_material_layer_change_locates_layers_by_ordinal(self) -> None:
        rows = self.dataset.query(global_id=gid(LABEL_WALL_MAT))
        material = [r for r in rows if r["aspect"] == "material"]
        self.assertEqual(len(material), 1)
        row = material[0]
        old_layers = [layer["material_name"] for layer in row["before"]["layers"]]
        new_layers = [layer["material_name"] for layer in row["after"]["layers"]]
        self.assertEqual(
            old_layers, ["Insulation-Layer", "Concrete-Core", "Plaster-Finish"]
        )
        self.assertEqual(
            new_layers, ["Concrete-Core", "Brick-Backup", "Insulation-Layer"]
        )
        # Every layer keeps its ordinal so the reorder is locatable.
        self.assertEqual(
            [layer["ordinal"] for layer in row["after"]["layers"]], [0, 1, 2]
        )

    def test_mapped_global_id_change_is_bridged_silently(self) -> None:
        new_rows = self.dataset.query(global_id=gid(LABEL_MAPPED_NEW))
        old_rows = self.dataset.query(global_id=gid(LABEL_MAPPED_OLD))
        self.assertEqual(new_rows, [])
        self.assertEqual(old_rows, [])
        # The mapping basis is retained in the dataset.
        mappings = self.dataset.mappings()
        self.assertEqual(len(mappings), 1)
        mapping = mappings[0]
        self.assertEqual(
            mapping["left"]["global_id"], gid(LABEL_MAPPED_OLD)
        )
        self.assertEqual(
            mapping["right"]["global_id"], gid(LABEL_MAPPED_NEW)
        )
        self.assertTrue(mapping["rationale"])

    def test_unmapped_global_id_change_is_removed_plus_added(self) -> None:
        removed = self.dataset.query(
            global_id=gid(LABEL_UNMAPPED_OLD), category=CAT_REMOVED
        )
        added = self.dataset.query(
            global_id=gid(LABEL_UNMAPPED_NEW), category=CAT_ADDED
        )
        self.assertEqual(len(removed), 1)
        self.assertEqual(len(added), 1)
        # It must not have been silently paired as a confident edit.
        semantic = self.dataset.query(global_id=gid(LABEL_UNMAPPED_OLD))
        self.assertTrue(
            all(r["category"] in (CAT_REMOVED, CAT_ADDED) for r in semantic)
        )

    def test_duplicate_global_id_is_incomparable_with_evidence(self) -> None:
        rows = self.dataset.query(global_id=gid(LABEL_DUP))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["category"], CAT_INCOMPARABLE)
        self.assertEqual(row["aspect"], "identity")
        # Both duplicate members are retained as evidence, not collapsed.
        self.assertEqual(len(row["after"]["rows"]), 2)
        self.assertTrue(
            all(not member["usable"] for member in row["after"]["rows"])
        )

    def test_missing_relation_endpoint_is_locally_incomparable(self) -> None:
        rows = self.dataset.query(global_id=gid(LABEL_BROKEN))
        spatial = [r for r in rows if r["aspect"] == "spatial_relation"]
        self.assertEqual(len(spatial), 1)
        self.assertEqual(spatial[0]["category"], CAT_INCOMPARABLE)
        # The object's other semantics stay comparable: it is not globally
        # removed or marked identity-incomparable.
        identity_rows = [r for r in rows if r["aspect"] == "identity"]
        self.assertEqual(identity_rows, [])

    def test_step_reorder_produces_no_business_change(self) -> None:
        rows = self.dataset.query(global_id=gid("diff-eq-stable"))
        self.assertEqual(rows, [])

    def test_genuine_numeric_value_change_is_semantic(self) -> None:
        from diff_fixtures import LABEL_VALUE

        rows = self.dataset.query(global_id=gid(LABEL_VALUE))
        weight = [
            r for r in rows
            if r["subject"].endswith("Weight\x1fIfcPropertySingleValue")
        ]
        self.assertEqual(len(weight), 1)
        row = weight[0]
        self.assertEqual(row["category"], CAT_SEMANTIC_CHANGE)
        # Both sides are valid measured values whose SI fractions differ.
        before_value = row["before"]["values"][0]
        after_value = row["after"]["values"][0]
        self.assertEqual(before_value["status"], "ok")
        self.assertEqual(after_value["status"], "ok")
        self.assertEqual(before_value["si_value"],
                         {"numerator": "10", "denominator": "1"})
        self.assertEqual(after_value["si_value"],
                         {"numerator": "12", "denominator": "1"})
        # The dimension is unchanged; only the value moved.
        self.assertEqual(before_value["dimension"], after_value["dimension"])

    # -- independent evidence verification ------------------------------
    def test_every_evidence_locator_exists_in_source_entities(self) -> None:
        """Re-open the input repos and independently verify each evidence row."""

        old = Repository(self.old_repo_path)
        new = Repository(self.new_repo_path)
        self.addCleanup(old.close)
        self.addCleanup(new.close)
        repos = {"left": old, "right": new}
        for record in self.dataset.query():
            evidence = self.dataset.evidence(record["change_id"])
            self.assertTrue(evidence, record["change_id"])
            for item in evidence:
                repo = repos[item["side"]]
                rows = repo.conn.execute(
                    "SELECT raw_text, line_start, line_end, sha256 FROM source_entities "
                    "WHERE source_id=? AND step_id=? AND role=?",
                    (item["source_id"], item["step_id"], item["role"]),
                ).fetchall()
                self.assertTrue(
                    rows,
                    f"evidence {item} has no source_entities backing row",
                )
                backing = rows[0]
                self.assertEqual(backing["sha256"], item["sha256"])
                self.assertEqual(backing["raw_text"], item["raw_text"])
                self.assertEqual(
                    backing["line_start"], item["line_start"]
                )

    def test_records_carry_both_side_source_and_sha(self) -> None:
        side_sources = {(s["side"], s["source_id"]): s
                        for s in self.dataset.side_sources()}
        for record in self.dataset.query():
            if record["left"] is not None:
                ref = side_sources[("left", record["left"]["source_id"])]
                self.assertEqual(ref["sha256"], record["left"]["sha256"])
            if record["right"] is not None:
                ref = side_sources[("right", record["right"]["source_id"])]
                self.assertEqual(ref["sha256"], record["right"]["sha256"])

    def test_query_filters_by_object_and_category(self) -> None:
        by_category = self.dataset.query(category=CAT_INCOMPARABLE)
        self.assertTrue(by_category)
        self.assertTrue(
            all(r["category"] == CAT_INCOMPARABLE for r in by_category)
        )
        by_object = self.dataset.query(global_id=gid(LABEL_MOVED))
        self.assertTrue(by_object)
        self.assertTrue(
            all(
                gid(LABEL_MOVED)
                in (
                    r["identity"]["left_global_id"],
                    r["identity"]["right_global_id"],
                )
                for r in by_object
            )
        )

    def test_summary_matches_record_counts(self) -> None:
        summary = self.dataset.summary()
        rows = self.dataset.query()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["category"]] = counts.get(row["category"], 0) + 1
        self.assertEqual(summary["categories"], counts)
        self.assertEqual(summary["change_count"], len(rows))


if __name__ == "__main__":
    unittest.main()
