"""Identity, merging, conflicts and cross-file independence."""

from __future__ import annotations

import json
import os
import unittest

import helpers
import fixtures

from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import AmbiguousIdentity, Repository


class IdentityTests(helpers.TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        fixture = fixtures.build_drill_fixture(self.path("fx"))
        repo, results = helpers.build_repo(self, fixture.root)
        self.repo = repo
        self.results = results
        self.fixture = fixture

    def test_global_id_identity_is_source_scoped(self) -> None:
        fixture = fixtures.build_multi_fixture(self.path("multi"))
        repo, results = helpers.build_repo(self, fixture.root, name="multi.sqlite")
        self.assertEqual([r.status for r in results], ["ok", "ok"])
        walls = repo.query_objects(entity_type="IfcBuildingElementProxy")
        self.assertEqual(len(walls), 2)
        keys = {w["key"] for w in walls}
        self.assertEqual(len(keys), 2, "same GlobalId in two sources must not merge")
        gids = {w["global_id"] for w in walls}
        self.assertEqual(len(gids), 1, "fixture uses the same GlobalId on purpose")
        self.assertEqual({w["source_id"] for w in walls}, {"alpha", "beta"})
        for wall in walls:
            props = repo.properties(wall["key"])
            self.assertEqual(len(props), 1)
            measured = props[0]["detail"]["declaration"]["interpretation"]["measured"][0]
            expected = "1" if wall["source_id"] == "alpha" else "2"
            self.assertEqual(measured["si_decimal"], expected)

    def test_step_identity_for_nodes_without_global_id(self) -> None:
        # A non-IfcRoot node has no GlobalId and uses (source, sha, step).
        rows = list(self.repo.conn.execute("SELECT * FROM nodes LIMIT 5"))
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["key"].startswith("step:drill:"))
            self.assertIn(":", row["key"])

    def test_no_invented_global_ids(self) -> None:
        rows = list(
            self.repo.conn.execute(
                "SELECT key_kind, COUNT(*) c FROM roots GROUP BY key_kind"
            )
        )
        kinds = {row["key_kind"]: row["c"] for row in rows}
        self.assertEqual(kinds.get("gid", 0) > 0, True)
        # Every step-kind root must actually have a null GlobalId.
        for row in self.repo.conn.execute(
            "SELECT global_id, key FROM roots WHERE key_kind='step'"
        ):
            self.assertIsNone(row["global_id"])

    def test_same_value_declarations_merge_with_two_sources(self) -> None:
        fixture = fixtures.build_cycle_fixture(self.path("merge"))
        repo, _ = helpers.build_repo(self, fixture.root, name="merge.sqlite")
        wall = helpers.object_by_name(repo, "W")
        length = repo.property(wall["key"], "Pset_Test", "Length")
        self.assertIsNotNone(length)
        self.assertEqual(length["state"], "value")
        self.assertEqual(length["detail"]["merged_source_count"], 2)
        self.assertEqual(len(length["detail"]["sources"]), 2)
        self.assertEqual(
            {s["step_id"] for s in length["detail"]["sources"]}, {17, 18}
        )
        # The differing FireRating declarations stay a conflict.
        fire = repo.property(wall["key"], "Pset_Test", "FireRating")
        self.assertEqual(fire["state"], "conflict")
        self.assertEqual(len(fire["detail"]["conflicting_declarations"]), 2)

    def test_same_identity_same_value_does_not_conflict(self) -> None:
        fixture = fixtures.build_cycle_fixture(self.path("merge2"))
        repo, _ = helpers.build_repo(self, fixture.root, name="merge2.sqlite")
        wall = helpers.object_by_name(repo, "W")
        length = repo.property(wall["key"], "Pset_Test", "Length")
        self.assertFalse(length["is_conflict"])
        self.assertEqual(length["value_status"], "ok")


class FailureEvidenceTests(helpers.TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        fixture = fixtures.build_cycle_fixture(self.path("fx"))
        self.repo, self.results = helpers.build_repo(self, fixture.root)

    def test_duplicate_global_id(self) -> None:
        failures = self.repo.failures(category="duplicate_global_id")
        self.assertEqual(len(failures), 1)
        detail = failures[0]["detail"]
        self.assertEqual(len(detail["step_ids"]), 2)
        self.assertIn("business_key", detail)
        d = helpers.object_by_name(self.repo, "D")
        e = helpers.object_by_name(self.repo, "E-duplicate")
        self.assertFalse(d["usable"])
        self.assertFalse(e["usable"])
        self.assertEqual(d["conflict_state"], "duplicate_global_id")
        self.assertEqual(e["conflict_state"], "duplicate_global_id")

    def test_spatial_cycle_marks_both_rooms(self) -> None:
        failures = self.repo.failures(category="spatial_cycle")
        self.assertEqual(len(failures), 1)
        cycle = failures[0]["detail"]["cycle"]
        self.assertGreaterEqual(len(cycle), 3)
        a = [
            o
            for o in self.repo.query_objects(entity_type="IfcSpace")
            if o["name"] == "A"
        ]
        self.assertEqual(len(a), 1)
        b = [
            o
            for o in self.repo.query_objects(entity_type="IfcSpace")
            if o["name"] == "B"
        ]
        self.assertEqual(len(b), 1)
        self.assertFalse(a[0]["usable"])
        self.assertFalse(b[0]["usable"])
        ancestors = self.repo.ancestors(a[0]["key"])
        self.assertEqual(ancestors["status"], "conflict")
        self.assertTrue(ancestors["cycles"])

    def test_missing_endpoint_recorded(self) -> None:
        failures = self.repo.failures(category="missing_endpoint")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["detail"]["step_id"], 5025)
        # The relation itself is marked missing rather than silently dropped.
        relation = self.repo.conn.execute(
            "SELECT * FROM relations WHERE state='missing'"
        ).fetchone()
        self.assertIsNotNone(relation)
        self.assertEqual(relation["relation_type"], "IfcRelAggregates")

    def test_descendants_report_blocked_edges(self) -> None:
        b = [
            o
            for o in self.repo.query_objects(entity_type="IfcSpace")
            if o["name"] == "B"
        ][0]
        detail = self.repo.descendants_detail(b["key"])
        self.assertEqual(detail["status"], "conflict")
        blocked = detail["blocked_edges"]
        self.assertTrue(blocked)
        self.assertEqual(blocked[0]["edge_state"], "conflict")
        self.assertIn("cycle", blocked[0]["reason"])
        # A normal traversal does not walk through the conflicted edge.
        self.assertEqual(self.repo.descendants(b["key"]), [])

    def test_relation_query_reports_conflict_or_missing(self) -> None:
        room_a = helpers.object_by_name(self.repo, "A")
        relations = self.repo.relations_for(room_a["key"])
        # The cycle relation exists and is preserved.
        self.assertTrue(any(r["relation_type"] == "IfcRelAggregates" for r in relations))

    def test_conflicted_objects_excluded_from_usable_set(self) -> None:
        usable = self.repo.query_objects(usable=True)
        names = {o["name"] for o in usable}
        self.assertNotIn("A", names)
        self.assertNotIn("E-duplicate", names)
        all_objects = self.repo.query_objects()
        self.assertIn("A", {o["name"] for o in all_objects})

    def test_ambiguous_global_id_lookup_is_refused(self) -> None:
        d = helpers.object_by_name(self.repo, "D")
        with self.assertRaises(AmbiguousIdentity) as caught:
            self.repo.find_object("cycle", global_id=d["global_id"])
        self.assertEqual(sorted(caught.exception.step_ids), sorted(
            [d["step_id"], helpers.object_by_name(self.repo, "E-duplicate")["step_id"]]
        ))
        conflicts = self.repo.identity_conflicts("cycle")
        self.assertEqual(len(conflicts), 2)
        # The full object table still exposes both as conflict rows.
        rows = [
            o for o in self.repo.query_objects(usable=False)
            if o["conflict_state"] == "duplicate_global_id"
        ]
        self.assertEqual(len(rows), 2)

    def test_cycle_relation_state_is_conflict(self) -> None:
        rows = list(
            self.repo.conn.execute(
                "SELECT state FROM relations WHERE relation_type='IfcRelAggregates'"
            )
        )
        states = [r["state"] for r in rows]
        self.assertIn("conflict", states)
        failing = list(
            self.repo.conn.execute(
                "SELECT edge_state, reason FROM relation_edges "
                "WHERE edge_state='conflict'"
            )
        )
        self.assertTrue(failing)
        self.assertIn("cycle", failing[0]["reason"])

    def test_dangling_reference_is_not_dropped_by_ifcopenshell(self) -> None:
        # IfcOpenShell returns an empty list for the dangling endpoint; the
        # repository must still retain it.
        relation = self.repo.conn.execute(
            "SELECT json FROM relations WHERE state='missing'"
        ).fetchone()
        payload = json.loads(relation["json"])
        self.assertIn(5025, payload["raw"]["related_step_ids"])


class ForwardReferenceTests(helpers.TempDirTestCase):
    def test_forward_reference_resolves(self) -> None:
        # The drill fixture already contains forward references after a
        # reorder; here we assert the raw parser indexed them regardless of
        # whether the reference appears before or after its target.
        from ifc_handover.step import StepFile

        text = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('f.ifc','',(''),(''),'','','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#3=IFCRELAGGREGATES('1',$,$,$,#2,(#1));
#1=IFCSPACE('a',$,'A',$,$,$,$,$,.ELEMENT.,.SPACE.,$);
#2=IFCSITE('b',$,'S',$,$,$,$,$,.ELEMENT.,$,$,$,$,$);
ENDSEC;
END-ISO-10303-21;
"""
        step = StepFile(text)
        self.assertEqual(step.refs(3), [2, 1])
        self.assertIn(1, step.entities)


if __name__ == "__main__":
    unittest.main()
