"""Test runner that works under ``python3 -I``.

Isolated mode ignores PYTHONPATH, so this script inserts the repository root,
the tests directory and the locally installed IfcOpenShell explicitly, then
runs unittest discovery.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TESTS = os.path.join(ROOT, "tests")
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
for hint in (
    "/home/node/.local/lib/python3.11/site-packages",
    "/tmp/ifcos_site",
):
    if os.path.isdir(os.path.join(hint, "ifcopenshell")) and hint not in sys.path:
        sys.path.append(hint)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbosity = 1
    pattern = "test_*.py"
    names: list[str] = []
    for arg in argv:
        if arg in {"-v", "--verbose"}:
            verbosity = 2
        elif arg.startswith("-"):
            continue
        else:
            names.append(arg)
    loader = unittest.TestLoader()
    if names:
        suite = unittest.TestSuite(
            loader.loadTestsFromName(name) for name in names
        )
    else:
        suite = loader.discover(TESTS, pattern=pattern, top_level_dir=TESTS)
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
