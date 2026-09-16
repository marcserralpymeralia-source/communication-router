from __future__ import annotations

import unittest

from sqlalchemy import create_engine, inspect

from app.migrations.registry import _apply_kibak_master_auth_throttles


class MigrationTests(unittest.TestCase):
    def test_kibak_auth_throttle_migration_is_incremental_and_idempotent(self):
        engine = create_engine("sqlite://")
        try:
            first = _apply_kibak_master_auth_throttles(engine, dry_run=False)
            second = _apply_kibak_master_auth_throttles(engine, dry_run=False)
            self.assertEqual(first, ["CREATE TABLE auth_throttles (...)"])
            self.assertEqual(second, [])
            self.assertIn("auth_throttles", inspect(engine).get_table_names())
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
