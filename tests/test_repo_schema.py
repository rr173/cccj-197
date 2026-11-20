"""Repository schema evolution tests for the physical-quantity tables.

A v1 repository (written before quantities were ingested) must open
unchanged; the v2 quantity tables are created on demand and stay empty, so a
legacy deliverable is readable without rewriting any existing business row.
"""

from __future__ import annotations

import sqlite3
import unittest

import helpers

from ifc_handover.store import SCHEMA_VERSION, Repository

QUANTITY_TABLES = (
    "quantity_set_definitions",
    "quantity_declarations",
    "object_quantity_sets",
    "resolved_quantities",
)


class SchemaEvolutionTests(helpers.TempDirTestCase):
    def test_new_repository_is_version_two(self) -> None:
        repo = Repository.create(self.path("v2.sqlite"))
        try:
            version = repo.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(int(version), SCHEMA_VERSION)
            self.assertEqual(SCHEMA_VERSION, 2)
            names = {
                r[0]
                for r in repo.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in QUANTITY_TABLES:
                self.assertIn(table, names)
        finally:
            repo.close()

    def test_legacy_v1_repository_opens_with_empty_quantity_tables(self) -> None:
        path = self.path("legacy.sqlite")
        repo = Repository.create(path)
        repo.close()
        conn = sqlite3.connect(path)
        for table in QUANTITY_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        conn.commit()
        conn.close()

        legacy = Repository(path)
        try:
            # The version marker is untouched; no business row is rewritten.
            version = legacy.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(int(version), 1)
            for table in QUANTITY_TABLES:
                count = legacy.conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                self.assertEqual(count, 0)
            # The normal repository API still works against the legacy store.
            self.assertEqual(legacy.stats()["resolved_quantities"], 0)
            self.assertEqual(legacy.stream_resolved_quantities().fetchall(), [])
        finally:
            legacy.close()

    def test_unknown_schema_version_is_rejected(self) -> None:
        path = self.path("future.sqlite")
        repo = Repository.create(path)
        repo.close()
        conn = sqlite3.connect(path)
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            Repository(path)


if __name__ == "__main__":
    unittest.main()
