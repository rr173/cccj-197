"""Explicit-null and conflict ordering rules, with no dependence on read order."""

from __future__ import annotations

import hashlib
import json
import os
import unittest

import helpers

from ifc_handover.export import export_subset, HandoverPackage
from ifc_handover.ingest import SourceIngestor, SourceManifestEntry
from ifc_handover.store import Repository

# A type with two competing Length declarations plus an unrelated Good value;
# one occurrence overrides Length with an explicit null, another leaves it
# inherited, a third overrides with a value.
TYPE_OVERRIDE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('t.ifc','',(''),(''),'','','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);
#2=IFCUNITASSIGNMENT((#1));
#3=IFCPROJECT('p',$,'P',$,$,$,$,$,#2);
#4=IFCSITE('s',$,'S',$,$,$,$,$,.ELEMENT.,$,$,$,$,$);
#5=IFCRELAGGREGATES('a',$,$,$,#3,(#4));
#10=IFCPROPERTYSINGLEVALUE('Length',$,IFCLENGTHMEASURE(1000.),$);
#11=IFCPROPERTYSINGLEVALUE('Length',$,IFCLENGTHMEASURE(2000.),$);
#12=IFCPROPERTYSINGLEVALUE('Good',$,IFCLENGTHMEASURE(3000.),$);
#13=IFCPROPERTYSET('tps',$,'Pset_T',$,(#10,#11,#12));
#14=IFCBUILDINGELEMENTPROXYTYPE('t',$,'T',$,$,(#13),$,$,$,.NOTDEFINED.);
#20=IFCBUILDINGELEMENTPROXY('e1',$,'Nulled',$,$,$,$,$,.NOTDEFINED.);
#21=IFCBUILDINGELEMENTPROXY('e2',$,'Inherited',$,$,$,$,$,.NOTDEFINED.);
#22=IFCBUILDINGELEMENTPROXY('e3',$,'Overridden',$,$,$,$,$,.NOTDEFINED.);
#23=IFCRELDEFINESBYTYPE('rt',$,$,$,(#20,#21,#22),#14);
#24=IFCRELCONTAINEDINSPATIALSTRUCTURE('c',$,$,$,(#20,#21,#22),#4);
#30=IFCPROPERTYSINGLEVALUE('Length',$,IFCLENGTHMEASURE(4000.),$);
#31=IFCPROPERTYSET('ips1',$,'Pset_T',$,(#30));
#32=IFCRELDEFINESBYPROPERTIES('rp1',$,$,$,(#22),#31);
#40=IFCPROPERTYSINGLEVALUE('Length',$, $, $);
#41=IFCPROPERTYSET('ips2',$,'Pset_T',$,(#40));
#42=IFCRELDEFINESBYPROPERTIES('rp2',$,$,$,(#20),#41);
ENDSEC;
END-ISO-10303-21;
"""

# Two declarations in one set with identical values and different raw spellings:
# 1.0 m and 100 cm are equal in SI but NOT the same declaration.
SAME_VALUE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('s.ifc','',(''),(''),'','','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);
#2=IFCUNITASSIGNMENT((#1));
#3=IFCPROJECT('p',$,'P',$, $,$,$,$,#2);
#4=IFCSITE('s',$,'S',$, $,$,$,$,.ELEMENT.,$,$,$,$,$);
#5=IFCRELAGGREGATES('a',$,$,$,#3,(#4));
#10=IFCPROPERTYSINGLEVALUE('Len',$,IFCLENGTHMEASURE(1000.),$);
#11=IFCPROPERTYSINGLEVALUE('Len',$,IFCLENGTHMEASURE(1000.0),$);
#12=IFCPROPERTYSET('ps',$,'Pset_S',$,(#10,#11));
#13=IFCBUILDINGELEMENTPROXY('e',$,'E',$, $,$,$,$,.NOTDEFINED.);
#14=IFCRELDEFINESBYPROPERTIES('r',$,$,$,(#13),#12);
#15=IFCRELCONTAINEDINSPATIALSTRUCTURE('c',$,$,$,(#13),#4);
ENDSEC;
END-ISO-10303-21;
"""


class OverrideRuleTests(helpers.TempDirTestCase):
    def _ingest_text(self, text: str, source_id: str, name: str = "repo.sqlite"):
        root = self.path(source_id + "-src")
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, f"{source_id}.ifc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        entry = SourceManifestEntry(
            source_id=source_id,
            path=path,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
        repo = Repository.create(self.path(name))
        self.addCleanup(repo.close)
        result = SourceIngestor(repo, entry).run()
        self.assertEqual(result.status, "ok", result.error)
        return repo

    def setUp(self) -> None:
        super().setUp()
        self.repo = self._ingest_text(TYPE_OVERRIDE, "t")

    def test_explicit_null_overrides_type_conflict(self) -> None:
        nulled = helpers.object_by_name(self.repo, "Nulled")
        prop = self.repo.property(nulled["key"], "Pset_T", "Length")
        self.assertEqual(prop["state"], "explicit_null")
        self.assertEqual(prop["value_status"], "explicit_null")
        self.assertEqual(prop["scope_used"], "instance")
        ignored = prop["detail"]["type_declaration_ignored"]
        self.assertTrue(ignored["type_conflict"])
        self.assertEqual(len(ignored["conflicting_declarations"]), 2)
        # The unrelated Good value still comes from the type.
        good = self.repo.property(nulled["key"], "Pset_T", "Good")
        self.assertEqual(good["state"], "value")
        self.assertEqual(
            good["detail"]["declaration"]["interpretation"]["measured"][0][
                "si_decimal"
            ],
            "3",
        )

    def test_inherited_type_conflict_is_not_picked(self) -> None:
        inherited = helpers.object_by_name(self.repo, "Inherited")
        prop = self.repo.property(inherited["key"], "Pset_T", "Length")
        self.assertEqual(prop["state"], "conflict")
        values = {
            d["declaration"]["values"][0]["raw"]
            for d in prop["detail"]["conflicting_declarations"]
        }
        self.assertEqual(
            values, {"IFCLENGTHMEASURE(1000.)", "IFCLENGTHMEASURE(2000.)"}
        )
        self.assertFalse(inherited["usable"])
        self.assertEqual(inherited["conflict_state"], "property_conflict")
        # Other identities from the same conflicting type stay resolved.
        good = self.repo.property(inherited["key"], "Pset_T", "Good")
        self.assertEqual(good["state"], "value")

    def test_instance_override_masks_type_conflict_for_that_identity_only(self) -> None:
        overridden = helpers.object_by_name(self.repo, "Overridden")
        prop = self.repo.property(overridden["key"], "Pset_T", "Length")
        self.assertEqual(prop["state"], "value")
        self.assertEqual(prop["scope_used"], "instance")
        self.assertEqual(
            prop["detail"]["declaration"]["interpretation"]["measured"][0][
                "si_decimal"
            ],
            "4",
        )
        self.assertTrue(prop["detail"]["type_declaration_ignored"]["type_conflict"])
        self.assertTrue(overridden["usable"])

    def test_conflict_object_and_dependents_are_excluded_from_export(self) -> None:
        inherited = helpers.object_by_name(self.repo, "Inherited")
        site = helpers.object_by_name(self.repo, "S")
        dest = self.path("pkg")
        summary = export_subset(self.repo, [site["key"]], dest)
        excluded = {e["key"] for e in summary["selection"]["excluded"]}
        self.assertIn(inherited["key"], excluded)
        with HandoverPackage(dest) as package:
            names = {o["name"] for o in package.objects()}
            self.assertNotIn("Inherited", names)
            self.assertIn("Nulled", names)
            self.assertIn("Overridden", names)
            excluded_rows = package.table("excluded_objects")
            self.assertTrue(any(r["name"] == "Inherited" for r in excluded_rows))
            evidence = package.table("excluded_properties")
            self.assertTrue(
                any(
                    r["prop_name"] == "Length" and r["state"] == "conflict"
                    for r in evidence
                )
            )


class DeclarationEqualityTests(helpers.TempDirTestCase):
    def test_equal_numeric_values_merge_regardless_of_spelling(self) -> None:
        root = self.path("s-src")
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, "s.ifc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(SAME_VALUE)
        entry = SourceManifestEntry(
            source_id="s", path=path, sha256=hashlib.sha256(SAME_VALUE.encode()).hexdigest()
        )
        repo = Repository.create(self.path("s.sqlite"))
        self.addCleanup(repo.close)
        self.assertEqual(SourceIngestor(repo, entry).run().status, "ok")
        element = helpers.object_by_name(repo, "E")
        prop = repo.property(element["key"], "Pset_S", "Len")
        self.assertEqual(prop["state"], "value")
        self.assertEqual(prop["detail"]["merged_source_count"], 2)

    def test_identical_values_with_different_explicit_units_do_not_merge(self) -> None:
        text = SAME_VALUE.replace(
            "#11=IFCPROPERTYSINGLEVALUE('Len',$,IFCLENGTHMEASURE(1000.0),$);",
            "#11=IFCPROPERTYSINGLEVALUE('Len',$,IFCLENGTHMEASURE(1000.),#6);\n"
            "#6=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);",
        )
        root = self.path("u-src")
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, "u.ifc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        entry = SourceManifestEntry(
            source_id="u", path=path, sha256=hashlib.sha256(text.encode()).hexdigest()
        )
        repo = Repository.create(self.path("u.sqlite"))
        self.addCleanup(repo.close)
        self.assertEqual(SourceIngestor(repo, entry).run().status, "ok")
        element = helpers.object_by_name(repo, "E")
        prop = repo.property(element["key"], "Pset_S", "Len")
        # Same numeric text but one declaration carries an explicit unit.  The
        # signatures differ, so this is a conflict rather than a silent merge.
        self.assertEqual(prop["state"], "conflict")


if __name__ == "__main__":
    unittest.main()
