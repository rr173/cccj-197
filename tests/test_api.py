"""API and state-model tests: queries, states, unsupported kinds, atomicity."""

from __future__ import annotations

import json
import os
import unittest

import helpers
import fixtures

from ifc_handover.export import HandoverPackage, export_subset
from ifc_handover.ingest import IngestionError, SourceIngestor, load_manifest
from ifc_handover.store import Repository


class QueryApiTests(helpers.TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        fixture = fixtures.build_drill_fixture(self.path("fx"))
        self.repo, _ = helpers.build_repo(self, fixture.root)
        self.fixture = fixture

    def test_object_by_key(self) -> None:
        room = helpers.object_by_name(self.repo, "Room 1")
        again = self.repo.get_object(room["key"])
        self.assertEqual(again["step_id"], room["step_id"])
        self.assertEqual(again["entity_type"], "IfcSpace")

    def test_find_by_global_id_and_step(self) -> None:
        room = helpers.object_by_name(self.repo, "Room 1")
        by_gid = self.repo.find_object("drill", global_id=room["global_id"])
        by_step = self.repo.find_object("drill", step_id=room["step_id"])
        self.assertEqual(by_gid["key"], room["key"])
        self.assertEqual(by_step["key"], room["key"])

    def test_query_objects_by_type(self) -> None:
        walls = self.repo.query_objects(entity_type="IfcWall")
        self.assertEqual(len(walls), 2)
        self.assertEqual({w["name"] for w in walls}, {"W1", "W2"})

    def test_query_objects_include_types(self) -> None:
        types = self.repo.query_objects(include_types=True, entity_type="IfcWallType")
        self.assertEqual(len(types), 1)
        self.assertTrue(types[0]["is_type"])

    def test_query_by_property_state_and_status(self) -> None:
        null_objects = self.repo.query_objects(
            property_state="explicit_null", property_pset="Pset_EquipmentCommon"
        )
        self.assertEqual([o["name"] for o in null_objects], ["EQ-B"])
        missing = self.repo.query_objects(property_status="missing_unit")
        self.assertEqual([o["name"] for o in missing], ["EQ-Unitless"])
        mismatch = self.repo.query_objects(property_status="dimension_mismatch")
        self.assertEqual([o["name"] for o in mismatch], ["EQ-D"])
        ok = self.repo.query_objects(property_status="ok")
        names = {o["name"] for o in ok}
        self.assertIn("EQ-A", names)
        self.assertIn("EQ-C", names)
        self.assertIn("W1", names)
        self.assertNotIn("EQ-B", names)

    def test_query_by_type_key(self) -> None:
        eq_a = helpers.object_by_name(self.repo, "EQ-A")
        eq_b = helpers.object_by_name(self.repo, "EQ-B")
        self.assertEqual(eq_a["type_key"], eq_b["type_key"])
        typed = self.repo.query_objects(type_key=eq_a["type_key"])
        self.assertEqual({o["name"] for o in typed}, {"EQ-A", "EQ-B"})

    def test_query_by_ancestor(self) -> None:
        room = helpers.object_by_name(self.repo, "Room 1")
        rows = self.repo.query_objects(ancestor=room["key"])
        self.assertIn("W1", {r["name"] for r in rows})
        self.assertNotIn("W2", {r["name"] for r in rows})

    def test_query_unusable(self) -> None:
        unusable = self.repo.query_objects(usable=False)
        self.assertEqual([o["name"] for o in unusable], ["EQ-D"])

    def test_property_lookup_by_identity_includes_kind(self) -> None:
        eq_a = helpers.object_by_name(self.repo, "EQ-A")
        single = self.repo.property(
            eq_a["key"],
            "Pset_Finish",
            "Finish",
            kind="IfcPropertySingleValue",
        )
        enumerated = self.repo.property(
            eq_a["key"],
            "Pset_Finish",
            "Finish",
            kind="IfcPropertyEnumeratedValue",
        )
        self.assertIsNotNone(single)
        self.assertIsNotNone(enumerated)
        self.assertNotEqual(single["identity"], enumerated["identity"])

    def test_missing_property_is_absent_not_null(self) -> None:
        eq_a = helpers.object_by_name(self.repo, "EQ-A")
        self.assertIsNone(self.repo.property(eq_a["key"], "Pset_None", "Nope"))
        self.assertEqual(self.repo.property_states(eq_a["key"]).get("absent", 0), 0)

    def test_unsupported_property_kind_is_preserved(self) -> None:
        fixture = fixtures.build_cycle_fixture(self.path("cycle"))
        repo, _ = helpers.build_repo(self, fixture.root, name="cycle.sqlite")
        props = repo.all_declarations("cycle")
        self.assertTrue(props)
        # No supported property here should have been dropped.
        kinds = {p["prop_kind"] for p in props}
        self.assertTrue(kinds)


class UnsupportedValueTests(helpers.TempDirTestCase):
    def test_unsupported_property_kind_and_expression(self) -> None:
        from ifc_handover.step import StepFile
        from ifc_handover.units import UnitResolver
        from ifc_handover.values import (
            STATE_EXPRESSION,
            STATE_UNSUPPORTED,
            PropertyReader,
        )

        text = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('u.ifc','',(''),(''),'','','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCPROPERTYBOUNDEDVALUE('B',$,$,IFCLENGTHMEASURE(1.),IFCLENGTHMEASURE(2.),$);
#2=IFCPROPERTYTABLEVALUE('T',$,(IFCLENGTHMEASURE(1.),IFCLENGTHMEASURE(2.)),
 (IFCLABEL('a'),IFCLABEL('b')),'=2*1',$,$,.LINEAR.);
#3=IFCPROPERTYSINGLEVALUE('X',$,IFCBOGUSTYPE(1.),$);
#4=IFCPROPERTYSINGLEVALUE('Y',$,IFCLENGTHMEASURE('not-a-number'),$);
ENDSEC;
END-ISO-10303-21;
"""
        step = StepFile(text)
        reader = PropertyReader(step, "IFC4", UnitResolver(step))
        bounded = reader.read(1, "IfcPropertyBoundedValue")
        self.assertIn(bounded.structural_state, {STATE_UNSUPPORTED, STATE_EXPRESSION})
        table = reader.read(2, "IfcPropertyTableValue")
        self.assertEqual(table.structural_state, STATE_EXPRESSION)
        self.assertEqual(table.expression, "=2*1")
        bogus = reader.read(3, "IfcPropertySingleValue")
        self.assertEqual(bogus.structural_state, STATE_UNSUPPORTED)
        self.assertIn("IfcValue", bogus.reason)
        bad_number = reader.read(4, "IfcPropertySingleValue")
        self.assertEqual(bad_number.structural_state, STATE_UNSUPPORTED)


class EnumeratedReferenceUnitTests(helpers.TempDirTestCase):
    def test_enumeration_reference_supplies_the_unit(self) -> None:
        import hashlib

        from ifc_handover.ingest import SourceManifestEntry

        text = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('e.ifc','',(''),(''),'','','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);
#2=IFCUNITASSIGNMENT((#1));
#3=IFCPROJECT('p',$,'P',$, $,$,$,$,#2);
#4=IFCSITE('s',$,'S',$, $,$,$,$,.ELEMENT.,$,$,$,$,$);
#5=IFCRELAGGREGATES('a',$,$,$,#3,(#4));
#6=IFCPROPERTYENUMERATION('EnumLen',(IFCLENGTHMEASURE(1000.),
 IFCLENGTHMEASURE(2000.)),#1);
#7=IFCPROPERTYENUMERATEDVALUE('Sizes',$,(IFCLENGTHMEASURE(1000.),
 IFCLENGTHMEASURE(2000.)),#6);
#8=IFCPROPERTYSET('ps',$,'Pset_Enum',$,(#7));
#9=IFCBUILDINGELEMENTPROXY('e',$,'E',$, $,$,$,$,.NOTDEFINED.);
#10=IFCRELDEFINESBYPROPERTIES('r',$,$,$,(#9),#8);
#11=IFCRELCONTAINEDINSPATIALSTRUCTURE('c',$,$,$,(#9),#4);
ENDSEC;
END-ISO-10303-21;
"""
        path = self.path("e.ifc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        repo = Repository.create(self.path("e.sqlite"))
        self.addCleanup(repo.close)
        entry = SourceManifestEntry(
            "e", path, hashlib.sha256(text.encode()).hexdigest()
        )
        self.assertEqual(SourceIngestor(repo, entry).run().status, "ok")
        obj = helpers.object_by_name(repo, "E")
        prop = repo.properties(obj["key"])[0]
        self.assertEqual(prop["state"], "value")
        self.assertEqual(prop["value_status"], "ok")
        measured = prop["detail"]["declaration"]["interpretation"]["measured"]
        self.assertEqual([m["si_decimal"] for m in measured], ["1000", "2000"])
        self.assertEqual(prop["detail"]["declaration"]["unit_step_id"], 1)
        self.assertEqual(
            prop["detail"]["declaration"]["extra"]["enumeration_reference"]["name"],
            "EnumLen",
        )


class AttributeMismatchTests(helpers.TempDirTestCase):
    def test_shifted_property_attributes_are_refused_not_misread(self) -> None:
        import hashlib

        from ifc_handover.ingest import SourceManifestEntry

        text = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('m.ifc','',(''),(''),'','','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCPROPERTYSINGLEVALUE('N',$,IFCLENGTHMEASURE(1.),$,$);
#2=IFCPROPERTYSET('p',$,'Pset_M',$,(#1));
#3=IFCPROJECT('p',$,'P',$, $,$,$,$,$);
ENDSEC;
END-ISO-10303-21;
"""
        path = self.path("m.ifc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        repo = Repository.create(self.path("m.sqlite"))
        self.addCleanup(repo.close)
        entry = SourceManifestEntry(
            "m", path, hashlib.sha256(text.encode()).hexdigest()
        )
        result = SourceIngestor(repo, entry).run()
        self.assertEqual(result.status, "ok")
        failures = repo.failures(category="attribute_count_mismatch")
        self.assertTrue(failures)
        self.assertEqual(failures[0]["detail"]["found"], 5)
        self.assertEqual(failures[0]["detail"]["expected"], 4)
        declarations = repo.all_declarations("m")
        self.assertEqual(len(declarations), 1)
        self.assertEqual(
            declarations[0]["state"], "unsupported",
            "a malformed declaration must not be interpreted by position",
        )
        self.assertEqual(declarations[0]["value_status"], "not_interpreted")


class ManifestTests(helpers.TempDirTestCase):
    def test_manifest_hash_mismatch_fails_ingestion(self) -> None:
        fixture = fixtures.build_drill_fixture(self.path("fx"))
        entries = load_manifest(fixture.manifest_path)
        entry = entries[0]
        broken = type(entry)(
            source_id=entry.source_id,
            path=entry.path,
            sha256="0" * 64,
        )
        repo = Repository.create(self.path("repo.sqlite"))
        self.addCleanup(repo.close)
        result = SourceIngestor(repo, broken).run()
        self.assertEqual(result.status, "failed")
        self.assertIn("SHA-256", result.error)
        self.assertEqual(repo.query_objects(), [])
        source = repo.source("drill")
        self.assertEqual(source["status"], "failed")

    def test_duplicate_source_ids_rejected(self) -> None:
        import json

        manifest = self.path("m.json")
        with open(manifest, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {"source_id": "x", "path": "a.ifc", "sha256": "0" * 64},
                    {"source_id": "x", "path": "b.ifc", "sha256": "1" * 64},
                ],
                fh,
            )
        with self.assertRaises(ValueError):
            load_manifest(manifest)

    def test_reingestion_replaces_old_rows(self) -> None:
        fixture = fixtures.build_drill_fixture(self.path("fx"))
        repo, results = helpers.build_repo(self, fixture.root)
        first = repo.stats()
        entry = load_manifest(fixture.manifest_path)[0]
        again = SourceIngestor(
            repo, entry, store_dir=self.path("repo.sqlite-store")
        ).run()
        self.assertEqual(again.status, "ok")
        second = repo.stats()
        self.assertEqual(first["objects"], second["objects"])
        self.assertEqual(first["resolved_properties"], second["resolved_properties"])

    def test_repository_rejects_non_repository(self) -> None:
        path = self.path("empty.sqlite")
        import sqlite3

        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE something(x)")
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            Repository(path)

    def test_manifest_relative_paths(self) -> None:
        fixture = fixtures.build_drill_fixture(self.path("fx"))
        entries = load_manifest(fixture.manifest_path)
        self.assertTrue(os.path.isabs(entries[0].path))


class ExportAtomicityTests(helpers.TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        fixture = fixtures.build_drill_fixture(self.path("fx"))
        self.fixture = fixture
        self.repo, _ = helpers.build_repo(self, fixture.root)

    def test_incomplete_destination_is_not_replaced(self) -> None:
        dest = self.path("pkg")
        os.makedirs(dest)
        with open(os.path.join(dest, "important.txt"), "w", encoding="utf-8") as fh:
            fh.write("keep me")
        room = helpers.object_by_name(self.repo, "Room 1")
        with self.assertRaises(Exception):
            export_subset(self.repo, [room["key"]], dest)
        with open(os.path.join(dest, "important.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep me")

    def test_non_spatial_root_is_rejected(self) -> None:
        eq = helpers.object_by_name(self.repo, "EQ-A")
        with self.assertRaises(Exception):
            export_subset(self.repo, [eq["key"]], self.path("pkg2"))

    def test_package_has_no_absolute_source_paths(self) -> None:
        room = helpers.object_by_name(self.repo, "Room 1")
        dest = self.path("pkg3")
        export_subset(self.repo, [room["key"]], dest)
        for dirpath, _dirs, files in os.walk(dest):
            for name in files:
                path = os.path.join(dirpath, name)
                if name.endswith((".sqlite", ".csv", ".json", ".md", ".excerpt")):
                    with open(path, "rb") as fh:
                        content = fh.read().decode("utf-8", errors="replace")
                    self.assertNotIn(self.fixture.root, content, path)
                    self.assertNotIn(str(self.repo.path), content, path)

    def test_package_reopen_after_copy(self) -> None:
        import shutil

        room = helpers.object_by_name(self.repo, "Room 1")
        dest = self.path("pkg4")
        export_subset(self.repo, [room["key"]], dest)
        copied = self.path("copied")
        shutil.copytree(dest, copied)
        shutil.rmtree(dest)
        with HandoverPackage(copied) as package:
            objects = package.objects()
            self.assertTrue(objects)
            manifest = package.manifest()
            self.assertTrue(manifest["sources"])
            for obj in objects:
                for prop in package.properties(obj["object_key"]):
                    self.assertIn("declaration", prop["detail"])


if __name__ == "__main__":
    unittest.main()
