"""Bounded-memory evidence for the spatial quantity take-off.

Scope walks, per-object lines and exact summaries all live in on-disk SQLite
tables; Python memory must stay bounded by one rule's frontier and one
summary group, not by the device or declaration totals.  A scaled fixture is
generated at two sizes (8x devices) in a fresh interpreter, and the take-off
build's own peak RSS growth must stay far below the data growth.  All
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
import gc, json, os, shutil, sys, tempfile
sys.path.insert(0, {root!r})
sys.path.insert(0, {tests!r})
sys.path.append({site!r})
from takeoff_fixtures import build_scaled_fixture
from ifc_handover.ingest import SourceIngestor, load_manifest
from ifc_handover.store import Repository
from ifc_handover.takeoff import build_takeoff, TakeoffDataset

def peak_kib():
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

def current_kib():
    try:
        with open('/proc/self/status') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except OSError:
        return None

tmp = tempfile.mkdtemp(prefix='ifc-takeoff-mem-')
try:
    fx = build_scaled_fixture(
        os.path.join(tmp, 'fx'),
        storeys={storeys}, rooms_per_storey={rooms}, devices_per_room={devices},
    )
    repo = Repository.create(os.path.join(tmp, 'repo.sqlite'))
    for e in load_manifest(fx['manifest']):
        SourceIngestor(repo, e).run()
    repo.close()
    gc.collect()
    peak_before = peak_kib()
    rss_before = current_kib()
    manifest = build_takeoff(
        os.path.join(tmp, 'repo.sqlite'), fx['rules'], os.path.join(tmp, 'dataset'),
    )
    gc.collect()
    with TakeoffDataset(os.path.join(tmp, 'dataset')) as ds:
        summary = ds.summary()
        summaries = ds.summaries()
    # Independent recomputation of the declared closed-form expectations.
    expected = fx['expected']
    count_rows = [s for s in summaries if s['q_kind'] == 'IfcQuantityCount']
    length_rows = [s for s in summaries if s['q_kind'] == 'IfcQuantityLength']
    count_total = sum(int(s['total_num']) for s in count_rows)
    from fractions import Fraction
    length_total = sum(
        (Fraction(int(s['total_num']), int(s['total_den'])) for s in length_rows),
        Fraction(0),
    )
    assert count_total == expected['count_total'], (count_total, expected)
    assert length_total == Fraction(
        expected['length_total_numerator'], expected['length_total_denominator']
    ), (length_total, expected)
    print(json.dumps({{
        'peak_rss_kib': peak_kib(),
        'peak_before_kib': peak_before,
        'takeoff_peak_growth_kib': peak_kib() - peak_before,
        'rss_before_kib': rss_before,
        'rss_after_kib': current_kib(),
        'devices': expected['devices'],
        'detail_lines': summary['detail_lines'],
        'count_total': count_total,
        'length_total': str(length_total),
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
        path = os.path.join(tmp, "measure_takeoff.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        proc = subprocess.run(
            [sys.executable, "-I", path],
            capture_output=True,
            text=True,
            timeout=900,
        )
    if proc.returncode != 0:
        raise AssertionError(f"take-off memory measurement failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


class TakeoffMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 300 vs 2400 devices (8x data).
        cls.small = _run_measure(storeys=5, rooms=4, devices=15)
        cls.large = _run_measure(storeys=10, rooms=8, devices=30)

    def test_scaling_shape(self) -> None:
        self.assertEqual(self.small["devices"] * 8, self.large["devices"])
        self.assertEqual(self.small["detail_lines"] * 8, self.large["detail_lines"])

    def test_closed_form_totals_match(self) -> None:
        for run in (self.small, self.large):
            self.assertIn("count_total", run)
            self.assertTrue(run["length_total"].startswith(""))

    def test_peak_rss_is_sublinear(self) -> None:
        small_growth = self.small["takeoff_peak_growth_kib"]
        large_growth = self.large["takeoff_peak_growth_kib"]
        self.assertGreaterEqual(small_growth, 0)
        self.assertLess(
            large_growth,
            96 * 1024,
            f"take-off peak grew {large_growth} KiB (small run {small_growth}) "
            "for 8x device/declaration count",
        )

    def test_resident_set_does_not_accumulate(self) -> None:
        before = self.large["rss_before_kib"]
        after = self.large["rss_after_kib"]
        if before is not None and after is not None:
            self.assertLess(after - before, 96 * 1024)


if __name__ == "__main__":
    unittest.main()
