"""Bounded-memory measurement for multi-file ingestion.

The application may fully load one source file.  It must not keep the whole
collection's property graph and spatial paths in memory.  These tests run the
pipeline in a fresh interpreter so allocator state from earlier tests cannot
distort the numbers, and they check scaling rather than absolute values.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

import helpers

from ifc_handover.memory import build_large_corpus


MEASURE_SCRIPT = r"""
import json, os, shutil, sys, tempfile
sys.path.insert(0, {root!r})
sys.path.insert(0, {tests!r})
sys.path.append({site!r})
from ifc_handover.memory import build_large_corpus, ingest_with_memory
from ifc_handover.store import Repository
from ifc_handover.ingest import load_manifest

tmp = tempfile.mkdtemp(prefix="ifc-handover-mem-")
try:
    entries, expectations = build_large_corpus(
        os.path.join(tmp, "fx"),
        sources={sources},
        rooms_per_source=30,
        elements_per_room=10,
        properties_per_element=5,
    )
    repo = Repository.create(os.path.join(tmp, "repo.sqlite"))
    report = ingest_with_memory(
        repo, load_manifest(os.path.join(tmp, "fx", "manifest.json")),
        store_dir=os.path.join(tmp, "store"),
    )
    print(json.dumps({{
        "report": report.as_json(),
        "stats": repo.stats(),
        "expectations": expectations,
    }}))
finally:
    repo.close()
    shutil.rmtree(tmp, ignore_errors=True)
"""


def _run_measure(sources: int) -> dict:
    root = helpers.ROOT
    site = "/home/node/.local/lib/python3.11/site-packages"
    if not os.path.isdir(os.path.join(site, "ifcopenshell")):
        site = "/tmp/ifcos_site"
    script = MEASURE_SCRIPT.format(
        root=root, tests=helpers.TESTS, site=site, sources=sources
    )
    with open(os.path.join(helpers.TESTS, ".measure_tmp.py"), "w", encoding="utf-8") as fh:
        fh.write(script)
    try:
        proc = subprocess.run(
            [sys.executable, "-I", os.path.join(helpers.TESTS, ".measure_tmp.py")],
            capture_output=True,
            text=True,
            timeout=600,
        )
    finally:
        try:
            os.remove(os.path.join(helpers.TESTS, ".measure_tmp.py"))
        except OSError:
            pass
    if proc.returncode != 0:
        raise AssertionError(f"measurement failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


class MemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.one = _run_measure(1)
        cls.many = _run_measure(8)

    def test_source_views_are_released(self) -> None:
        for payload in (self.one, self.many):
            self.assertEqual(
                payload["report"]["source_views_live_after_ingest"], 0
            )

    def test_all_sources_ingested(self) -> None:
        stats = self.many["stats"]
        self.assertEqual(stats["sources"], 8)
        self.assertEqual(stats["sources_failed"], 0)
        expected_objects = sum(e["elements"] + e["rooms"] for e in self.many["expectations"])
        # Every generated room, element and site is an IfcObject in IFC4
        # (IfcSite is a spatial IfcProduct).  Each source contributes one site.
        extra_sites = len(self.many["expectations"])
        self.assertEqual(stats["objects"], expected_objects + extra_sites)

    def test_memory_is_bounded_across_sources(self) -> None:
        one = self.one["report"]
        many = self.many["report"]
        one_bytes = one["total_bytes"]
        many_bytes = many["total_bytes"]
        self.assertGreaterEqual(many_bytes, one_bytes * 7)
        # Eight times the data must not mean eight times the peak.  Allow a
        # fixed overhead for Python, IfcOpenShell, SQLite and allocator reuse;
        # what matters is that growth is far below data growth.
        growth_kib = many["final_peak_rss_kib"] - one["final_peak_rss_kib"]
        self.assertLess(
            growth_kib,
            96 * 1024,
            f"peak RSS grew by {growth_kib} KiB for {many_bytes/one_bytes:.1f}x data",
        )

    def test_property_count_matches_authored_expectation(self) -> None:
        expected = sum(e["properties"] for e in self.many["expectations"])
        self.assertEqual(
            self.many["stats"]["resolved_properties"],
            expected,
            "every authored property must be resolved exactly once",
        )


if __name__ == "__main__":
    unittest.main()
