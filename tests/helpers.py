"""Shared test helpers.

Tests may run under ``python3 -I`` (isolated mode), so this file adds the
repository root and the locally installed IfcOpenShell to ``sys.path`` explicitly
before importing the package.  No test imports a module from an untrusted
source directory.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

IFC_OPEN_SHELL_HINTS = [
    "/home/node/.local/lib/python3.11/site-packages",
    "/tmp/ifcos_site",
]
for hint in IFC_OPEN_SHELL_HINTS:
    if os.path.isdir(os.path.join(hint, "ifcopenshell")) and hint not in sys.path:
        sys.path.append(hint)


def have_ifcopenshell() -> bool:
    try:
        import ifcopenshell  # noqa: F401

        return True
    except Exception:
        return False


class TempDirTestCase(unittest.TestCase):
    """A test case with a temporary directory cleaned up afterwards."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ifc-handover-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)


def build_repo(testcase: TempDirTestCase, fixture_root: str, *, name: str = "repo.sqlite"):
    """Ingest a generated fixture and return an open repository."""

    from ifc_handover.ingest import SourceIngestor, load_manifest
    from ifc_handover.store import Repository

    repo = Repository.create(testcase.path(name))
    testcase.addCleanup(repo.close)
    store_dir = testcase.path(name + "-store")
    results = []
    for entry in load_manifest(os.path.join(fixture_root, "manifest.json")):
        results.append(SourceIngestor(repo, entry, store_dir=store_dir).run())
    return repo, results


def object_by_name(repo, name: str):
    matches = [o for o in repo.query_objects() if o["name"] == name]
    if len(matches) != 1:
        raise AssertionError(f"expected one object named {name!r}, found {len(matches)}")
    return matches[0]
