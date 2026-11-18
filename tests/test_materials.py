"""Material hierarchy shapes: layer sets, material lists, constituents."""

from __future__ import annotations

import json
import os
import unittest

import helpers

from ifc_handover.export import HandoverPackage, export_subset
from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import Repository
from ifc_handover.step import StepFile
from ifc_handover.units import UnitResolver

MATERIAL_FILE = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('m.ifc','',(''),(''),'','','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);
#2=IFCUNITASSIGNMENT((#1));
#3=IFCPROJECT('p',$,'P',$,$,$,$,$,#2);
#4=IFCSITE('s',$,'S',$,$,$,$,$,.ELEMENT.,$,$,$,$,$);
#5=IFCRELAGGREGATES('a1',$,$,$,#3,(#4));
#10=IFCMATERIAL('Steel',$,$);
#11=IFCMATERIAL('Timber',$,$);
#12=IFCMATERIAL('Glass',$,$);
#13=IFCMATERIALLIST((#12,#10,#11));
#20=IFCBUILDINGELEMENTPROXY('e1',$,'EMatList',$,$,$,$,$,.NOTDEFINED.);
#21=IFCRELCONTAINEDINSPATIALSTRUCTURE('c1',$,$,$,(#20),#4);
#22=IFCRELASSOCIATESMATERIAL('am1',$,$,$,(#20),#13);
#30=IFCMATERIAL('Alu',$,$);
#31=IFCMATERIAL('Foam',$,$);
#32=IFCMATERIALCONSTITUENT('Core',$,#30,0.7,'LoadBearing');
#33=IFCMATERIALCONSTITUENT('Insulation',$,#31,0.3,'Thermal');
#34=IFCMATERIALCONSTITUENTSET('Panel',$,(#32,#33));
#40=IFCBUILDINGELEMENTPROXY('e2',$,'EConstituents',$,$,$,$,$,.NOTDEFINED.);
#41=IFCRELCONTAINEDINSPATIALSTRUCTURE('c2',$,$,$,(#40),#4);
#42=IFCRELASSOCIATESMATERIAL('am2',$,$,$,(#40),#34);
ENDSEC;
END-ISO-10303-21;
"""


class MaterialShapeTests(helpers.TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        root = self.path("fx")
        os.makedirs(root)
        with open(os.path.join(root, "m.ifc"), "w", encoding="utf-8") as fh:
            fh.write(MATERIAL_FILE)
        import hashlib

        with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "sources": [
                        {
                            "source_id": "m",
                            "path": "m.ifc",
                            "sha256": hashlib.sha256(
                                MATERIAL_FILE.encode()
                            ).hexdigest(),
                        }
                    ]
                },
                fh,
            )
        self.repo, results = helpers.build_repo(self, root)
        self.assertEqual([r.status for r in results], ["ok"])

    def test_material_list_keeps_order_and_distinct_nodes(self) -> None:
        element = helpers.object_by_name(self.repo, "EMatList")
        assignment = self.repo.material_assignments(element["key"])[0]
        root_key = assignment["root_key"]
        node = self.repo.conn.execute(
            "SELECT kind FROM material_nodes WHERE key=?", (root_key,)
        ).fetchone()
        self.assertEqual(node["kind"], "material_list")
        edges = list(
            self.repo.conn.execute(
                "SELECT child_key, ordinal, slot FROM material_edges "
                "WHERE parent_key=? ORDER BY ordinal",
                (root_key,),
            )
        )
        self.assertEqual([e["ordinal"] for e in edges], [0, 1, 2])
        names = [
            self.repo.conn.execute(
                "SELECT name FROM material_nodes WHERE key=?", (e["child_key"],)
            ).fetchone()["name"]
            for e in edges
        ]
        self.assertEqual(names, ["Glass", "Steel", "Timber"])
        self.assertEqual(len(set(r["child_key"] for r in edges)), 3)

    def test_constituent_set_preserves_fraction_and_names(self) -> None:
        element = helpers.object_by_name(self.repo, "EConstituents")
        assignment = self.repo.material_assignments(element["key"])[0]
        root_key = assignment["root_key"]
        edges = list(
            self.repo.conn.execute(
                "SELECT child_key, ordinal FROM material_edges "
                "WHERE parent_key=? ORDER BY ordinal",
                (root_key,),
            )
        )
        self.assertEqual(len(edges), 2)
        names = [
            self.repo.conn.execute(
                "SELECT name FROM material_nodes WHERE key=?", (e["child_key"],)
            ).fetchone()["name"]
            for e in edges
        ]
        self.assertEqual(names, ["Core", "Insulation"])

    def test_export_keeps_material_hierarchy(self) -> None:
        element = helpers.object_by_name(self.repo, "EMatList")
        site = helpers.object_by_name(self.repo, "S")
        dest = self.path("pkg")
        export_subset(self.repo, [site["key"]], dest)
        with HandoverPackage(dest) as package:
            materials = package.materials(element["key"])
            self.assertEqual(len(materials), 1)
            tree = package.material_tree(materials[0]["key"])
            self.assertEqual(tree["kind"], "material_list")
            children = [c["material"]["name"] for c in tree["children"]]
            self.assertEqual(children, ["Glass", "Steel", "Timber"])


if __name__ == "__main__":
    unittest.main()
