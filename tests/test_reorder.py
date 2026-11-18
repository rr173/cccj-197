"""STEP reordering and renumbering must not change business values or relations.

Source coordinates (line numbers, entity ids in raw text) may differ; the
business projection must be identical, and each business value must still point
back to the corresponding original declaration.
"""

from __future__ import annotations

import json
import os
import unittest

import helpers
import fixtures

from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import Repository


def _business_projection(repo: Repository) -> dict:
    """A projection that deliberately excludes STEP ids and line numbers."""

    objects = {}
    for row in repo.query_objects():
        key = f"{row['source_id']}:{row['global_id'] or row['entity_type'] + '@' + (row['name'] or str(row['step_id']))}"
        objects[key] = {
            "entity_type": row["entity_type"],
            "name": row["name"],
            "usable": row["usable"],
            "conflict_state": row["conflict_state"],
            "properties": {},
            "materials": [],
        }
        for prop in repo.properties(row["key"]):
            measured = (
                prop["detail"].get("declaration", {})
                .get("interpretation", {})
                .get("measured", [])
            )
            objects[key]["properties"][
                (prop["pset_name"], prop["prop_name"], prop["prop_kind"])
            ] = {
                "state": prop["state"],
                "value_status": prop["value_status"],
                "scope": prop["scope_used"],
                "si": [m.get("si_decimal") if m else None for m in measured],
                "merged": prop["detail"].get("merged_source_count"),
                "type_conflict": (
                    prop["detail"].get("type_declaration_ignored") or {}
                ).get("type_conflict"),
            }
        for assignment in repo.material_assignments(row["key"]):
            tree = assignment["tree"] or {}
            names = []
            queue = [assignment["root_key"]]
            seen = set()
            while queue:
                current = queue.pop(0)
                if current is None or current in seen:
                    continue
                seen.add(current)
                node = repo.conn.execute(
                    "SELECT kind, name FROM material_nodes WHERE key=?", (current,)
                ).fetchone()
                if node:
                    names.append((node["kind"], node["name"]))
                for edge in repo.conn.execute(
                    "SELECT child_key FROM material_edges WHERE parent_key=? "
                    "ORDER BY slot, ordinal",
                    (current,),
                ):
                    queue.append(edge["child_key"])
            objects[key]["materials"].append(
                {"inherited": tree.get("inherited_from_type"), "nodes": names}
            )
    relations = []
    for row in repo.conn.execute(
        "SELECT relation_type, state FROM relations ORDER BY relation_type, state"
    ):
        relations.append((row["relation_type"], row["state"]))
    failures = sorted(
        (f["category"], f["severity"]) for f in repo.failures()
    )
    return {"objects": objects, "relations": relations, "failures": failures}


class ReorderTests(helpers.TempDirTestCase):
    def _ingest(self, root: str, name: str):
        repo, results = helpers.build_repo(self, root, name=name)
        self.assertEqual([r.status for r in results], ["ok"])
        return repo

    def test_reordered_file_has_same_business_projection(self) -> None:
        base = fixtures.build_drill_fixture(self.path("fx"))
        text = open(base.files["drill"], encoding="utf-8").read()
        reordered = fixtures.reorder_step(text)
        self.assertNotEqual(text, reordered)
        alt_root = self.path("alt")
        os.makedirs(alt_root)
        with open(os.path.join(alt_root, "drill.ifc"), "w", encoding="utf-8") as fh:
            fh.write(reordered)
        import hashlib

        with open(os.path.join(alt_root, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "sources": [
                        {
                            "source_id": "drill",
                            "path": "drill.ifc",
                            "sha256": hashlib.sha256(reordered.encode()).hexdigest(),
                        }
                    ]
                },
                fh,
            )
        repo_a = self._ingest(base.root, "a.sqlite")
        repo_b = self._ingest(alt_root, "b.sqlite")
        projection_a = _business_projection(repo_a)
        projection_b = _business_projection(repo_b)
        self.assertEqual(projection_a, projection_b)
        # Sources are honestly reported as different bytes.
        self.assertNotEqual(
            repo_a.source("drill")["sha256"], repo_b.source("drill")["sha256"]
        )

    def test_renumbered_step_ids_keep_business_values(self) -> None:
        base = fixtures.build_drill_fixture(self.path("fx"))
        text = open(base.files["drill"], encoding="utf-8").read()
        renumbered = fixtures.renumber_step(text, offset=5000)
        alt_root = self.path("alt")
        os.makedirs(alt_root)
        with open(os.path.join(alt_root, "drill.ifc"), "w", encoding="utf-8") as fh:
            fh.write(renumbered)
        import hashlib

        with open(os.path.join(alt_root, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "sources": [
                        {
                            "source_id": "drill",
                            "path": "drill.ifc",
                            "sha256": hashlib.sha256(renumbered.encode()).hexdigest(),
                        }
                    ]
                },
                fh,
            )
        repo_a = self._ingest(base.root, "a.sqlite")
        repo_b = self._ingest(alt_root, "b.sqlite")
        self.assertEqual(_business_projection(repo_a), _business_projection(repo_b))
        # But the raw coordinates really are different.
        step_a = repo_a.conn.execute(
            "SELECT step_id FROM roots WHERE name='W1'"
        ).fetchone()["step_id"]
        step_b = repo_b.conn.execute(
            "SELECT step_id FROM roots WHERE name='W1'"
        ).fetchone()["step_id"]
        self.assertEqual(step_b, step_a + 5000)

    def test_source_locators_point_back_to_the_same_declaration(self) -> None:
        base = fixtures.build_drill_fixture(self.path("fx"))
        text = open(base.files["drill"], encoding="utf-8").read()
        reordered = fixtures.reorder_step(text)
        alt_root = self.path("alt")
        os.makedirs(alt_root)
        with open(os.path.join(alt_root, "drill.ifc"), "w", encoding="utf-8") as fh:
            fh.write(reordered)
        import hashlib

        with open(os.path.join(alt_root, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "sources": [
                        {
                            "source_id": "drill",
                            "path": "drill.ifc",
                            "sha256": hashlib.sha256(reordered.encode()).hexdigest(),
                        }
                    ]
                },
                fh,
            )
        repo_a = self._ingest(base.root, "a.sqlite")
        repo_b = self._ingest(alt_root, "b.sqlite")

        def locate(repo: Repository, prop_name: str) -> str:
            """Resolve a property's business row to the exact source line."""

            prop = None
            for obj in repo.query_objects():
                for candidate in repo.properties(obj["key"]):
                    if candidate["prop_name"] == prop_name and candidate["pset_name"] == "Pset_Dimensions":
                        prop = candidate
            self.assertIsNotNone(prop)
            step_id = prop["detail"]["declaration"]["prop_step_id"]
            source_id = prop["source_id"]
            source = repo.source(source_id)
            path = source["stored_path"]
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            row = repo.conn.execute(
                "SELECT line_start, line_end, raw_text FROM source_entities "
                "WHERE source_id=? AND step_id=? AND role='property'",
                (source_id, step_id),
            ).fetchone()
            self.assertIsNotNone(row)
            line = lines[row["line_start"] - 1]
            return line

        width_a = locate(repo_a, "Width")
        width_b = locate(repo_b, "Width")
        self.assertIn("IFCLENGTHMEASURE(2.)", width_a)
        self.assertEqual(width_a, width_b)
        patch_a = locate(repo_a, "PatchArea")
        patch_b = locate(repo_b, "PatchArea")
        self.assertIn("IFCAREAMEASURE(500000.)", patch_a)
        self.assertEqual(patch_a, patch_b)

    def test_multi_file_collection_memory_is_bounded(self) -> None:
        from ifc_handover.memory import build_large_corpus, ingest_with_memory

        corpus = self.path("large")
        entries, expectations = build_large_corpus(
            corpus,
            sources=3,
            rooms_per_source=8,
            elements_per_room=4,
            properties_per_element=3,
        )
        repo = Repository.create(self.path("large.sqlite"))
        self.addCleanup(repo.close)
        manifest = load_manifest(os.path.join(corpus, "manifest.json"))
        report = ingest_with_memory(
            repo, manifest, store_dir=self.path("large-store")
        )
        # No source view survives ingestion.
        self.assertEqual(report.source_views_live_after_ingest, 0)
        self.assertEqual(len(report.sources), 3)
        self.assertTrue(all(s.status == "ok" for s in report.sources))
        # Peak RSS must not scale with the number of sources: compare against
        # the largest single source with a generous fixed overhead allowance.
        overhead_kib = 180 * 1024
        largest_kib = report.max_single_file_bytes / 1024
        self.assertLess(
            report.final_peak_rss_kib,
            largest_kib * 40 + overhead_kib,
            "peak RSS grew more than expected for a bounded per-source pipeline",
        )
        # And the repository really contains every source.
        self.assertEqual(repo.source_count(), 3)
        self.assertGreater(len(repo.query_objects()), 3 * 8)

    def test_property_state_queries(self) -> None:
        fixture = fixtures.build_drill_fixture(self.path("fx"))
        repo, _ = helpers.build_repo(self, fixture.root)
        null_objects = repo.query_objects(
            property_pset="Pset_EquipmentCommon",
            property_name="Length",
            property_state="explicit_null",
        )
        self.assertEqual([o["name"] for o in null_objects], ["EQ-B"])
        cycle_objects = repo.query_objects(property_status="unit_cycle")
        self.assertEqual([o["name"] for o in cycle_objects], ["EQ-D"])
        conflict_objects = repo.query_objects(
            property_pset="Pset_Loads",
            property_name="Load",
            property_state="conflict",
        )
        self.assertEqual([o["name"] for o in conflict_objects], ["EQ-D"])

    def test_ancestor_query_does_not_use_cached_paths(self) -> None:
        fixture = fixtures.build_drill_fixture(self.path("fx"))
        repo, _ = helpers.build_repo(self, fixture.root)
        room = helpers.object_by_name(repo, "Room 1")
        rows = repo.query_objects(ancestor=room["key"])
        names = {row["name"] for row in rows}
        self.assertIn("W1", names)
        self.assertIn("EQ-A", names)
        self.assertNotIn("W2", names)
        # There is no materialised path table in the repository.
        tables = {
            row[0]
            for row in repo.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertNotIn("object_paths", tables)
        self.assertNotIn("spatial_paths", tables)


if __name__ == "__main__":
    unittest.main()
