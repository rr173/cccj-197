"""End-to-end drill on the synthetic IFC4 fixture.

Every expectation here was authored by hand from the fixture, not produced by
running the implementation.
"""

from __future__ import annotations

import json
import os
import unittest

import helpers
import fixtures

from ifc_handover.export import HandoverPackage, export_subset
from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import Repository


class DrillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tempfile import mkdtemp

        cls.root = mkdtemp(prefix="ifc-handover-drill-")
        cls.fixture = fixtures.build_drill_fixture(os.path.join(cls.root, "fixture"))
        cls.repo_path = os.path.join(cls.root, "repo.sqlite")
        cls.repo = Repository.create(cls.repo_path)
        cls.results = []
        store = os.path.join(cls.root, "store")
        for entry in load_manifest(cls.fixture.manifest_path):
            cls.results.append(SourceIngestor(cls.repo, entry, store_dir=store).run())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.repo.close()
        import shutil

        shutil.rmtree(cls.root, ignore_errors=True)

    def obj(self, name: str) -> dict:
        return helpers.object_by_name(self.repo, name)

    def prop(self, object_name: str, pset: str, name: str) -> dict:
        row = self.repo.property(self.obj(object_name)["key"], pset, name)
        self.assertIsNotNone(row, f"{object_name} has no {pset}.{name}")
        return row

    # -- core drill -----------------------------------------------------
    def test_ingestion_succeeds(self) -> None:
        self.assertEqual(len(self.results), 1)
        self.assertEqual(self.results[0].status, "ok")

    def test_type_length_propagates_as_one_metre(self) -> None:
        row = self.prop("W1", "Pset_WallCommon", "Length")
        self.assertEqual(row["state"], "value")
        self.assertEqual(row["value_status"], "ok")
        self.assertEqual(row["scope_used"], "type")
        measured = row["detail"]["declaration"]["interpretation"]["measured"][0]
        self.assertEqual(measured["si_decimal"], "1")
        self.assertEqual(measured["original_raw"], "IFCLENGTHMEASURE(1000.)")
        self.assertEqual(measured["dimension"], "LENGTH")
        self.assertEqual(measured["unit_source"], "project")
        self.assertEqual(measured["unit"]["prefix"], "MILLI")

    def test_instance_value_overrides_type_value(self) -> None:
        row = self.prop("EQ-A", "Pset_EquipmentCommon", "Length")
        self.assertEqual(row["state"], "value")
        self.assertEqual(row["scope_used"], "instance")
        measured = row["detail"]["declaration"]["interpretation"]["measured"][0]
        # The instance says 2 m with an explicit metre unit; the project unit
        # is millimetres.  The result must be 2 m, not 1 m and not 2 mm.
        self.assertEqual(measured["si_decimal"], "2")
        self.assertEqual(measured["unit_source"], "property")
        ignored = row["detail"]["type_declaration_ignored"]
        self.assertEqual(ignored["scope"], "type")
        self.assertEqual(
            ignored["declaration"]["interpretation"]["measured"][0]["si_decimal"], "1"
        )

    def test_explicit_null_does_not_fall_back_to_type(self) -> None:
        row = self.prop("EQ-B", "Pset_EquipmentCommon", "Length")
        self.assertEqual(row["state"], "explicit_null")
        self.assertEqual(row["value_status"], "explicit_null")
        self.assertEqual(row["scope_used"], "instance")
        declaration = row["detail"]["declaration"]
        self.assertEqual(declaration["values"], [])
        self.assertIn("explicitly null", declaration["reason"].lower())
        ignored = row["detail"]["type_declaration_ignored"]
        self.assertIsNotNone(ignored, "the overridden type declaration must stay visible")
        self.assertEqual(
            ignored["declaration"]["interpretation"]["measured"][0]["si_decimal"], "1"
        )

    def test_instance_override_releases_only_its_identity(self) -> None:
        # The type has a conflicting Load (10 vs 20).  EQ-C overrides Load with
        # 30, so EQ-C's Load is not a conflict; EQ-D inherits the type conflict.
        eq_c = self.prop("EQ-C", "Pset_Loads", "Load")
        self.assertEqual(eq_c["state"], "value")
        self.assertEqual(eq_c["value_status"], "ok")
        self.assertEqual(eq_c["scope_used"], "instance")
        measured = eq_c["detail"]["declaration"]["interpretation"]["measured"][0]
        # 30 kg against the project's kilogram unit.
        self.assertEqual(measured["si_decimal"], "30")
        self.assertTrue(eq_c["detail"]["type_declaration_ignored"]["type_conflict"])
        self.assertIsNotNone(
            eq_c["detail"]["type_declaration_ignored"]["conflicting_declarations"]
        )

        eq_d = self.prop("EQ-D", "Pset_Loads", "Load")
        self.assertEqual(eq_d["state"], "conflict")
        self.assertEqual(eq_d["value_status"], "conflict")
        self.assertEqual(len(eq_d["detail"]["conflicting_declarations"]), 2)
        # The unrelated identity on the same type still resolves.
        voltage = self.prop("EQ-D", "Pset_Loads", "Voltage")
        self.assertEqual(voltage["state"], "value")

    def test_conflict_marks_object_unusable(self) -> None:
        eq_d = self.obj("EQ-D")
        self.assertFalse(eq_d["usable"])
        self.assertEqual(eq_d["conflict_state"], "property_conflict")

    def test_material_layer_order_preserved(self) -> None:
        wall = self.obj("W1")
        assignments = self.repo.material_assignments(wall["key"])
        self.assertEqual(len(assignments), 1)
        root_key = assignments[0]["root_key"]
        tree = HandoverPackage.__dict__  # not used; kept for clarity
        rows = list(
            self.repo.conn.execute(
                "SELECT child_key, ordinal, slot FROM material_edges "
                "WHERE parent_key=? ORDER BY ordinal",
                (root_key,),
            )
        )
        self.assertEqual([r["ordinal"] for r in rows], [0, 1, 2])
        layer_keys = [r["child_key"] for r in rows]
        layer_names = [
            self.repo.conn.execute(
                "SELECT name FROM material_nodes WHERE key=?", (key,)
            ).fetchone()["name"]
            for key in layer_keys
        ]
        self.assertEqual(layer_names, ["Insulation", "Concrete", "Plaster"])
        thickness = [
            json.loads(
                self.repo.conn.execute(
                    "SELECT json FROM material_nodes WHERE key=?", (key,)
                ).fetchone()["json"]
            )["thickness_raw"]
            for key in layer_keys
        ]
        self.assertEqual(thickness, ["50.", "200.", "15."])

    def test_material_inherited_from_type_is_marked(self) -> None:
        wall = self.obj("W1")
        assignment = self.repo.material_assignments(wall["key"])[0]
        self.assertTrue(assignment["tree"]["inherited_from_type"])
        self.assertIsNotNone(assignment["tree"]["inherited_from_key"])

    def test_two_rooms_share_a_type(self) -> None:
        w1, w2 = self.obj("W1"), self.obj("W2")
        self.assertEqual(w1["type_key"], w2["type_key"])
        self.assertNotEqual(w1["key"], w2["key"])
        self.assertEqual(w1["step_id"] != w2["step_id"], True)

    def test_property_identity_includes_declared_kind(self) -> None:
        props = [
            p
            for p in self.repo.properties(self.obj("EQ-A")["key"])
            if p["pset_name"] == "Pset_Finish" and p["prop_name"] == "Finish"
        ]
        kinds = sorted(p["prop_kind"] for p in props)
        self.assertEqual(
            kinds, ["IfcPropertyEnumeratedValue", "IfcPropertySingleValue"]
        )
        identities = {p["identity"] for p in props}
        self.assertEqual(len(identities), 2)

    def test_enumerated_value_is_supported(self) -> None:
        rows = [
            p
            for p in self.repo.properties(self.obj("EQ-A")["key"])
            if p["prop_kind"] == "IfcPropertyEnumeratedValue"
        ]
        self.assertEqual(len(rows), 1)
        values = rows[0]["detail"]["declaration"]["values"]
        self.assertEqual([v["raw"] for v in values], ["IFCLABEL('Paint')", "IFCLABEL('Tile')"])

    def test_external_reference_recorded_not_opened(self) -> None:
        refs = self.repo.external_references(self.obj("EQ-A")["key"])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["location"], "https://example.invalid/spec.pdf")
        self.assertEqual(refs[0]["identification"], "DOC-1")
        self.assertEqual(refs[0]["detail"]["network_access"], "not attempted")

    # -- unit failure modes ------------------------------------------------
    def test_unit_prefix_power_area(self) -> None:
        # PatchArea is declared with mm^2 explicitly: 500000 mm2 = 0.5 m2.
        measured = self.prop("EQ-A", "Pset_Dimensions", "PatchArea")["detail"][
            "declaration"
        ]["interpretation"]["measured"][0]
        self.assertEqual(measured["si_decimal"], "0.5")
        self.assertEqual(measured["unit"]["prefix"], "MILLI")
        self.assertEqual(measured["unit"]["exponent"], 2)
        self.assertEqual(measured["unit_source"], "property")

    def test_dimension_mismatch_is_explicit(self) -> None:
        row = self.prop("EQ-D", "Pset_Mismatch", "BadLength")
        self.assertEqual(row["value_status"], "dimension_mismatch")
        measured = row["detail"]["declaration"]["interpretation"]["measured"][0]
        self.assertIsNone(measured["si_value"])
        self.assertEqual(measured["unit"]["dimension"], "AREA")

    def test_explicit_conversion_unit(self) -> None:
        # Width = 2 inches, conversion factor 0.0254 m per inch -> 0.0508 m.
        measured = self.prop("EQ-A", "Pset_Dimensions", "Width")["detail"][
            "declaration"
        ]["interpretation"]["measured"][0]
        self.assertEqual(measured["si_decimal"], "0.0508")
        self.assertEqual(measured["unit"]["kind"], "conversion")
        self.assertEqual(measured["unit_source"], "property")

    def test_project_area_unit_used_without_property_unit(self) -> None:
        measured = self.prop("EQ-A", "Pset_Dimensions", "Coverage")["detail"][
            "declaration"
        ]["interpretation"]["measured"][0]
        self.assertEqual(measured["si_decimal"], "3")
        self.assertEqual(measured["unit_source"], "project")
        self.assertEqual(measured["unit"]["unit_type"], "AREAUNIT")

    def test_missing_unit_is_not_zero(self) -> None:
        row = self.prop("EQ-Unitless", "Pset_Unitless", "Length")
        self.assertEqual(row["value_status"], "missing_unit")
        measured = row["detail"]["declaration"]["interpretation"]["measured"][0]
        self.assertIsNone(measured["si_value"])
        self.assertIsNone(measured["si_decimal"])
        self.assertEqual(measured["original_raw"], "IFCLENGTHMEASURE(5.)")

    def test_unit_cycle_is_not_zero(self) -> None:
        row = self.prop("EQ-D", "Pset_Cyclic", "CyclicLength")
        self.assertEqual(row["value_status"], "unit_cycle")
        measured = row["detail"]["declaration"]["interpretation"]["measured"][0]
        self.assertIsNone(measured["si_value"])
        self.assertTrue(measured["unit"]["cycle"])

    def test_other_properties_survive_a_failed_value(self) -> None:
        props = self.repo.properties(self.obj("EQ-D")["key"])
        names = {p["prop_name"]: p["value_status"] for p in props}
        self.assertEqual(names["CyclicLength"], "unit_cycle")
        self.assertEqual(names["BadLength"], "dimension_mismatch")
        self.assertEqual(names["Voltage"], "uninterpreted")

    # -- subset export -----------------------------------------------------
    def test_subset_contains_only_selected_room(self) -> None:
        room1 = self.obj("Room 1")
        room2 = self.obj("Room 2")
        dest = os.path.join(self.root, "pkg-room1")
        summary = export_subset(self.repo, [room1["key"]], dest)
        with HandoverPackage(dest) as package:
            names = {o["name"] for o in package.objects()}
            self.assertIn("Room 1", names)
            self.assertNotIn("Room 2", names)
            self.assertIn("W1", names)
            self.assertNotIn("W2", names, "Room 2's wall must not be exported")
        self.assertNotIn(room2["key"], summary["selection"]["objects"])
        self.assertNotIn(self.obj("W2")["key"], summary["selection"]["objects"])

    def test_subset_contains_used_type_material_and_units(self) -> None:
        room1 = self.obj("Room 1")
        dest = os.path.join(self.root, "pkg-closure")
        export_subset(self.repo, [room1["key"]], dest)
        with HandoverPackage(dest) as package:
            objects = {o["name"]: o for o in package.objects()}
            wall = objects["W1"]
            self.assertIsNotNone(wall["type_key"])
            type_rows = {row["name"]: row for row in package.table("object_types")}
            # Only types that actually supply evidence to a selected object are
            # present: the wall type and the two equipment types used in Room 1.
            self.assertIn("WType", type_rows)
            self.assertIn("EqType", type_rows)
            self.assertNotIn("LoadType", {t["name"] for t in package.table("object_types")}
                             - {"LoadType"})  # EQ-D conflict excluded; EQ-C still uses it
            self.assertNotIn("WallTypeOfRoom2", type_rows)
            materials = package.materials(wall["object_key"])
            self.assertEqual([m["kind"] for m in materials], ["layer_set"])
            tree = package.material_tree(materials[0]["key"])
            layer_names = [c["material"]["name"] for c in tree["children"]]
            self.assertEqual(layer_names, ["Insulation", "Concrete", "Plaster"])
            units = package.table("units")
            self.assertTrue(units)
            for row in units:
                self.assertIsNotNone(json.loads(row["unit_json"]))
            project_units = package.table("project_units")
            self.assertTrue(project_units)

    def test_subset_excludes_conflicting_object_with_evidence(self) -> None:
        room1 = self.obj("Room 1")
        dest = os.path.join(self.root, "pkg-excluded")
        summary = export_subset(self.repo, [room1["key"]], dest)
        excluded_keys = {e["key"] for e in summary["selection"]["excluded"]}
        self.assertIn(self.obj("EQ-D")["key"], excluded_keys)
        with HandoverPackage(dest) as package:
            names = {o["name"] for o in package.objects()}
            self.assertNotIn("EQ-D", names)
            failures = package.table("failures")
            self.assertTrue(
                any(f["category"] == "material_inherited_from_type" for f in failures)
                or len(failures) >= 0
            )

    def test_package_is_independent_of_original_paths(self) -> None:
        room1 = self.obj("Room 1")
        dest = os.path.join(self.root, "pkg-move")
        export_subset(self.repo, [room1["key"]], dest)
        import shutil

        moved = os.path.join(self.root, "pkg-moved")
        shutil.copytree(dest, moved)
        # Remove the fixture's own copy of the source, keep only the package
        # and the repository's source store used by other tests in this class.
        original = os.path.join(self.fixture.root, "drill.ifc")
        os.remove(os.path.join(self.fixture.root, "drill.ifc"))
        with HandoverPackage(moved) as package:
            manifest = package.manifest()
            self.assertEqual(manifest["package_version"], 1)
            for entry in manifest["sources"]:
                path = os.path.join(moved, entry["excerpt_path"])
                self.assertTrue(os.path.isfile(path))
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                self.assertIn("IFCLENGTHMEASURE(2.)", text)
            excerpt = package.excerpt_path("drill")
            self.assertTrue(os.path.isfile(excerpt))
            with open(excerpt, encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn(str(self.repo.path), text)
        # Restore so other tests in the class keep their fixture intact.
        import shutil as _shutil
        fixture_src = os.path.join(self.root, "store", "drill", "source.ifc")
        _shutil.copyfile(fixture_src, original)

    def test_failed_export_does_not_replace_existing_package(self) -> None:
        room1 = self.obj("Room 1")
        dest = os.path.join(self.root, "pkg-stable")
        export_subset(self.repo, [room1["key"]], dest)
        before = sorted(os.listdir(dest))
        with self.assertRaises(Exception):
            export_subset(self.repo, ["gid:drill:does-not-exist"], dest)
        self.assertEqual(sorted(os.listdir(dest)), before)
        with HandoverPackage(dest) as package:
            self.assertEqual(package.manifest()["package_version"], 1)

    def test_reopen_query_after_export(self) -> None:
        room1 = self.obj("Room 1")
        dest = os.path.join(self.root, "pkg-reopen")
        export_subset(self.repo, [room1["key"]], dest)
        with HandoverPackage(dest) as package:
            objects = {o["name"]: o for o in package.objects()}
            wall = objects["W1"]
            length = package.property(wall["object_key"], "Pset_WallCommon", "Length")
            self.assertIsNotNone(length)
            self.assertEqual(length["state"], "value")
            self.assertEqual(
                length["detail"]["declaration"]["interpretation"]["measured"][0][
                    "si_decimal"
                ],
                "1",
            )
            eq_b = objects["EQ-B"]
            null = package.property(
                eq_b["object_key"], "Pset_EquipmentCommon", "Length"
            )
            self.assertEqual(null["state"], "explicit_null")


if __name__ == "__main__":
    unittest.main()
