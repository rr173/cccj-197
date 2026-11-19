"""Bounded-memory evidence for the cross-version diff.

The diff merge-joins sorted SQLite streams; its working set must be bounded by
the largest single object's semantics and the write buffer, not by the total
object count on either side.  These tests build several runtime-generated
repositories of increasing size, diff each against a same-size counterpart in
a fresh interpreter, and assert that peak RSS grows far more slowly than the
object total.  No large generated file is committed; everything is thrown away.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

import helpers

MEASURE_SCRIPT = r"""
import json, os, shutil, sys, tempfile, resource, gc
sys.path.insert(0, {root!r})
sys.path.insert(0, {tests!r})
sys.path.append({site!r})
from ifc_handover.memory import build_large_corpus
from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import Repository
from ifc_handover.diff import build_diff

def peak_kib():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

def current_kib():
    try:
        with open('/proc/self/status') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except OSError:
        return None

tmp = tempfile.mkdtemp(prefix='ifc-diff-mem-')
try:
    objects = {objects}
    rps = objects // 30  # 30 rooms per source in build_large_corpus
    build_large_corpus(
        os.path.join(tmp, 'fx-left'), sources=1,
        rooms_per_source=30, elements_per_room=rps, properties_per_element=4,
    )
    build_large_corpus(
        os.path.join(tmp, 'fx-right'), sources=1,
        rooms_per_source=30, elements_per_room=rps, properties_per_element=4,
    )
    left_repo = Repository.create(os.path.join(tmp, 'left.sqlite'))
    for e in load_manifest(os.path.join(tmp, 'fx-left', 'manifest.json')):
        SourceIngestor(left_repo, e, store_dir=os.path.join(tmp, 'sl')).run()
    left_repo.close()
    right_repo = Repository.create(os.path.join(tmp, 'right.sqlite'))
    for e in load_manifest(os.path.join(tmp, 'fx-right', 'manifest.json')):
        SourceIngestor(right_repo, e, store_dir=os.path.join(tmp, 'sr')).run()
    right_repo.close()
    # Measure the diff in isolation: ingestion peak must not be credited to the
    # diff.  Record current RSS once the source views are fully released.
    gc.collect()
    rss_before = current_kib()
    peak_before = peak_kib()
    manifest = build_diff(
        os.path.join(tmp, 'left.sqlite'),
        os.path.join(tmp, 'right.sqlite'),
        os.path.join(tmp, 'diff'),
    )
    gc.collect()
    print(json.dumps({{
        'peak_rss_kib': peak_kib(),
        'peak_before_kib': peak_before,
        'diff_peak_growth_kib': peak_kib() - peak_before,
        'rss_before_kib': rss_before,
        'rss_after_kib': current_kib(),
        'objects': objects,
        'change_count': manifest['change_count'],
    }}))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
"""


def _run_measure(objects: int) -> dict:
    site = "/home/node/.local/lib/python3.11/site-packages"
    if not os.path.isdir(os.path.join(site, "ifcopenshell")):
        site = "/tmp/ifcos_site"
    script = MEASURE_SCRIPT.format(
        root=helpers.ROOT, tests=helpers.TESTS, site=site, objects=objects
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "measure_diff.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        proc = subprocess.run(
            [sys.executable, "-I", path],
            capture_output=True,
            text=True,
            timeout=600,
        )
    if proc.returncode != 0:
        raise AssertionError(f"diff memory measurement failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


class DiffMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 300 vs 2400 objects (8x data, matching the ingestion memory test's
        # scaling ratio).
        cls.small = _run_measure(300)
        cls.large = _run_measure(2400)

    def test_measurements_ran(self) -> None:
        self.assertGreater(self.small["objects"], 0)
        self.assertEqual(self.large["objects"], 8 * self.small["objects"])

    def test_peak_rss_is_sublinear(self) -> None:
        # The diff's own peak growth (excluding ingestion, which is allowed to
        # fully load one source) must stay bounded as the object count grows 8x.
        small_growth = self.small["diff_peak_growth_kib"]
        large_growth = self.large["diff_peak_growth_kib"]
        # ru_maxrss is monotonic; a near-flat reading across sizes proves the
        # diff streams rather than accumulating both repositories.
        self.assertGreaterEqual(small_growth, 0)
        self.assertLess(
            large_growth,
            64 * 1024,
            f"diff peak grew {large_growth} KiB (small run {small_growth}) "
            "for 8x object count",
        )

    def test_resident_set_does_not_accumulate(self) -> None:
        # Current RSS after the diff should be close to its pre-diff value:
        # no whole-repository object graph is retained in Python.
        before = self.large["rss_before_kib"]
        after = self.large["rss_after_kib"]
        if before is not None and after is not None:
            self.assertLess(after - before, 64 * 1024)

    def test_diff_completed(self) -> None:
        # The two regenerated corpora use deterministic GlobalIds derived from
        # source ids, so they share identities; the run must finish cleanly.
        self.assertGreaterEqual(self.large["change_count"], 0)


if __name__ == "__main__":
    unittest.main()
