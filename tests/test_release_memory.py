"""Bounded-memory evidence for release verification.

Zone walks, the ledger index, coverage targets and per-checkpoint results all
live in on-disk SQLite tables; Python memory must stay bounded by one zone's
frontier and one matter's evidence, not by the device, revision or coverage
totals.  These tests generate the scaled fixture at two sizes (8x devices and
revisions), run the verification in a fresh interpreter, and assert that the
verification's own peak RSS growth stays far below the data growth.  All
generated files are throw-away; nothing large is committed.
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
from release_fixtures import build_scaled_fixture
from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import Repository
from ifc_handover.release import build_release_verification, ReleaseDataset

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

tmp = tempfile.mkdtemp(prefix='ifc-release-mem-')
try:
    fx = build_scaled_fixture(
        os.path.join(tmp, 'fx'), seed=20261119,
        storeys={storeys}, rooms={rooms}, devices={devices},
    )
    repo = Repository.create(os.path.join(tmp, 'repo.sqlite'))
    for e in load_manifest(fx['manifest']):
        SourceIngestor(repo, e).run()
    repo.close()
    # Measure the verification in isolation: ingestion peak must not be
    # credited to it.
    gc.collect()
    rss_before = current_kib()
    peak_before = peak_kib()
    manifest = build_release_verification(
        os.path.join(tmp, 'repo.sqlite'), fx['rules'], fx['ledger'],
        fx['checkpoints'], os.path.join(tmp, 'dataset'),
    )
    gc.collect()
    with ReleaseDataset(os.path.join(tmp, 'dataset')) as ds:
        summary = ds.summary()
    print(json.dumps({{
        'peak_rss_kib': peak_kib(),
        'peak_before_kib': peak_before,
        'verify_peak_growth_kib': peak_kib() - peak_before,
        'rss_before_kib': rss_before,
        'rss_after_kib': current_kib(),
        'matters': manifest['run']['matters'],
        'revisions': manifest['ledger']['revisions'],
        'statuses': summary['matter_statuses'],
    }}))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
"""


def _run_measure(storeys: int, rooms: int, devices: int) -> dict:
    site = "/home/node/.local/lib/python3.11/site-packages"
    if not os.path.isdir(os.path.join(site, "ifcopenshell")):
        site = "/tmp/ifcos_site"
    script = MEASURE_SCRIPT.format(
        root=helpers.ROOT,
        tests=helpers.TESTS,
        site=site,
        storeys=storeys,
        rooms=rooms,
        devices=devices,
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "measure_release.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        proc = subprocess.run(
            [sys.executable, "-I", path],
            capture_output=True,
            text=True,
            timeout=900,
        )
    if proc.returncode != 0:
        raise AssertionError(f"release memory measurement failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


class ReleaseMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 300 vs 2400 devices (8x data, matching the ingestion memory test's
        # scaling ratio).
        cls.small = _run_measure(storeys=5, rooms=4, devices=15)
        cls.large = _run_measure(storeys=10, rooms=8, devices=30)

    def test_measurements_ran(self) -> None:
        self.assertEqual(self.small["matters"], 5 * 4 * 15)
        self.assertEqual(self.large["matters"], 10 * 8 * 30)
        self.assertEqual(self.large["matters"], 8 * self.small["matters"])

    def test_counts_are_scale_invariant_in_shape(self) -> None:
        for run in (self.small, self.large):
            statuses = run["statuses"]
            total = run["matters"]
            self.assertEqual(statuses["sc-cp1"].get("blocked", 0), total)
            self.assertEqual(statuses["sc-cp2"].get("ready", 0), total)
            self.assertEqual(
                statuses["sc-cp3"].get("ready", 0) + statuses["sc-cp3"].get("blocked", 0),
                total,
            )

    def test_peak_rss_is_sublinear(self) -> None:
        # The verification's own peak growth (excluding ingestion) must stay
        # bounded as devices and revisions grow 8x.
        small_growth = self.small["verify_peak_growth_kib"]
        large_growth = self.large["verify_peak_growth_kib"]
        self.assertGreaterEqual(small_growth, 0)
        self.assertLess(
            large_growth,
            96 * 1024,
            f"verification peak grew {large_growth} KiB (small run {small_growth}) "
            "for 8x device/revision count",
        )

    def test_resident_set_does_not_accumulate(self) -> None:
        before = self.large["rss_before_kib"]
        after = self.large["rss_after_kib"]
        if before is not None and after is not None:
            self.assertLess(after - before, 96 * 1024)


if __name__ == "__main__":
    unittest.main()
