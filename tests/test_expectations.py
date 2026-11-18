"""The drill table in docs/drill-expectations.md is asserted against the code."""

from __future__ import annotations

import os
import unittest

import helpers
import fixtures

from ifc_handover.export import HandoverPackage, export_subset
from ifc_handover.ingest import load_manifest
from ifc_handover.store import Repository


class ExpectationsTableTests(helpers.TempDirTestCase):
    """Checks the hand-authored table, one row group at a time."""

    @classmethod
    def setUpClass(cls) -> None:
        from tempfile import mkdtemp

        cls.root = mkdtemp(prefix="ifc-handover-expect-")
        cls.fixture = fixtures.build_drill_fixture(os.path.join(cls.root, "fx"))
        cls.repo = Repository.create(os.path.join(cls.root, "repo.sqlite"))
        from ifc_handover.ingest import SourceIngestor

        cls.store = os.path.join(cls.root, "store")
        for entry in load_manifest(cls.fixture.manifest_path):
            SourceIngestor(cls.repo, entry, store_dir=cls.store).run()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.repo.close()
        import shutil

        shutil.rmtree(cls.root, ignore_errors=True)

    def _row(self, obj: str, pset: str, name: str, kind: str | None = None) -> dict:
        row = helpers.object_by_name(self.repo, obj)
        prop = self.repo.property(row["key"], pset, name, kind=kind)
        self.assertIsNotNone(prop, f"missing {obj} {pset}.{name}")
        return prop

    def _si(self, prop: dict):
        measured = prop["detail"]["declaration"]["interpretation"]["measured"]
        return [m["si_decimal"] if m else None for m in measured]

    def test_table_rows(self) -> None:
        expectations = [
            ("W1", "Pset_WallCommon", "Length", "value", "ok", ["1"]),
            ("W1", "Pset_WallCommon", "FireRating", "value", "uninterpreted", [None]),
            ("W1", "Pset_WallCommon", "AcousticRating", "value", "uninterpreted", [None]),
            ("W2", "Pset_WallCommon", "Length", "value", "ok", ["1"]),
            ("EQ-A", "Pset_EquipmentCommon", "Length", "value", "ok", ["2"]),
            ("EQ-A", "Pset_Dimensions", "Width", "value", "ok", ["0.0508"]),
            ("EQ-A", "Pset_Dimensions", "Coverage", "value", "ok", ["3"]),
            ("EQ-A", "Pset_Dimensions", "PatchArea", "value", "ok", ["0.5"]),
            ("EQ-C", "Pset_Loads", "Load", "value", "ok", ["30"]),
            ("EQ-C", "Pset_Loads", "Voltage", "value", "uninterpreted", [None]),
        ]
        for obj, pset, name, state, status, si in expectations:
            if name == "Length" and obj == "W1":
                prop = self._row(obj, pset, name)
                self.assertEqual(prop["scope_used"], "type")
            else:
                prop = self._row(obj, pset, name)
            self.assertEqual(prop["state"], state, f"{obj} {name}")
            self.assertEqual(prop["value_status"], status, f"{obj} {name}")
            self.assertEqual(self._si(prop), si, f"{obj} {name}")

    def test_explicit_null_row(self) -> None:
        prop = self._row("EQ-B", "Pset_EquipmentCommon", "Length")
        self.assertEqual(prop["state"], "explicit_null")
        self.assertEqual(prop["value_status"], "explicit_null")
        self.assertEqual(prop["scope_used"], "instance")

    def test_conflict_row(self) -> None:
        prop = self._row("EQ-D", "Pset_Loads", "Load")
        self.assertEqual(prop["state"], "conflict")
        self.assertEqual(prop["value_status"], "conflict")
        declarations = prop["detail"]["conflicting_declarations"]
        self.assertEqual(len(declarations), 2)
        values = {
            d["declaration"]["values"][0]["raw"] for d in declarations
        }
        self.assertEqual(
            values, {"IFCMASSMEASURE(10.)", "IFCMASSMEASURE(20.)"}
        )

    def test_other_rows(self) -> None:
        cyclic = self._row("EQ-D", "Pset_Cyclic", "CyclicLength")
        self.assertEqual(cyclic["value_status"], "unit_cycle")
        mismatch = self._row("EQ-D", "Pset_Mismatch", "BadLength")
        self.assertEqual(mismatch["value_status"], "dimension_mismatch")
        missing = self._row("EQ-Unitless", "Pset_Unitless", "Length")
        self.assertEqual(missing["value_status"], "missing_unit")
        for prop in (cyclic, mismatch, missing):
            self.assertTrue(all(v is None for v in self._si(prop)))
            # The original spelling survives even when no SI value exists.
            raw = prop["detail"]["declaration"]["values"][0]["raw"]
            self.assertTrue(raw.startswith("IFC"))

    def test_finish_kinds(self) -> None:
        row = helpers.object_by_name(self.repo, "EQ-A")
        props = [
            p
            for p in self.repo.properties(row["key"])
            if p["pset_name"] == "Pset_Finish"
        ]
        self.assertEqual(
            sorted(p["prop_kind"] for p in props),
            ["IfcPropertyEnumeratedValue", "IfcPropertySingleValue"],
        )

    def test_expected_subset_membership(self) -> None:
        room = helpers.object_by_name(self.repo, "Room 1")
        dest = os.path.join(self.root, "expect-pkg")
        summary = export_subset(self.repo, [room["key"]], dest)
        selected = set(summary["selection"]["objects"])
        excluded = {e["key"]: e["reason"] for e in summary["selection"]["excluded"]}
        eq_d = helpers.object_by_name(self.repo, "EQ-D")
        self.assertIn(eq_d["key"], excluded)
        # Selection reaches EQ-D but it is unusable, so it is excluded from the
        # exported object table and from the package object count.
        selected_names = {
            self.repo.get_object(key)["name"]
            for key in selected
            if self.repo.get_object(key) is not None
            and self.repo.get_object(key)["usable"]
        }
        self.assertEqual(selected_names, {"Room 1", "W1", "EQ-A", "EQ-B", "EQ-C"})
        with HandoverPackage(dest) as package:
            type_names = {t["name"] for t in package.table("object_types")}
            self.assertEqual(type_names, {"WType", "EqType", "LoadType"})
            material = package.materials(
                helpers.object_by_name(self.repo, "W1")["key"]
            )
            self.assertEqual(len(material), 1)
            tree = package.material_tree(material[0]["key"])
            layers = [
                (c["material"]["name"], c["material"]["detail"]["thickness_raw"])
                for c in tree["children"]
            ]
            self.assertEqual(
                layers, [("Insulation", "50."), ("Concrete", "200."), ("Plaster", "15.")]
            )
            # Only one material root is stored, shared by both walls.
            self.assertEqual(len(package.table("materials")), 7)
            dimensions = {
                row["dimension"]: row for row in package.table("project_units")
            }
            self.assertEqual(set(dimensions), {"LENGTH", "AREA", "MASS"})


if __name__ == "__main__":
    unittest.main()
