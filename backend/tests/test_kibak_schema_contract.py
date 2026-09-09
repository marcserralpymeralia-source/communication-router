from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.db.database import Base
from app.db.models import Company, TenantSchemaMigration
from app.migrations.kibak_baseline import (
    KIBAK_BASELINE_CONFIRMATION,
    KIBAK_TENANT_BASELINE_VERSION,
    KIBAK_TENANT_TABLES,
    create_kibak_tenant_schema,
)
from app.migrations.registry import KIBAK_TENANT_SCHEMA_MIGRATIONS, CURRENT_KIBAK_TENANT_SCHEMA_VERSION
from app.migrations.runner import run_migration_plan
from app.tenancy.database import _validate_kibak_baseline


class KibakSchemaContractTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {"APP_ENV": "test", "APP_SLUG": "kibak", "SECRET_KEY": "kibak-schema-test-" + "x" * 40},
            clear=False,
        )
        self.env.start()
        get_settings.cache_clear()
        self.engines = []

    def tearDown(self):
        for engine in self.engines:
            engine.dispose()
        get_settings.cache_clear()
        self.env.stop()

    def test_clean_baseline_contains_complete_kibak_schema(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine = create_engine(f"sqlite:///{Path(tempdir, 'tenant.db')}")
            self.engines.append(engine)
            result = create_kibak_tenant_schema(
                engine,
                company_id=7,
                company_name="KIBAK Test",
                confirmation=KIBAK_BASELINE_CONFIRMATION,
            )
            self.assertEqual(set(inspect(engine).get_table_names()), KIBAK_TENANT_TABLES)
            self.assertIn("worker_heartbeats", KIBAK_TENANT_TABLES)
            self.assertEqual(result["version"], KIBAK_TENANT_BASELINE_VERSION)
            self.assertEqual(
                _validate_kibak_baseline(engine, 7)["is_current"],
                True,
            )

    def test_baseline_validation_detects_schema_drift(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine = create_engine(f"sqlite:///{Path(tempdir, 'tenant.db')}")
            self.engines.append(engine)
            create_kibak_tenant_schema(
                engine,
                company_id=7,
                company_name="KIBAK Test",
                confirmation=KIBAK_BASELINE_CONFIRMATION,
            )
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP TABLE worker_heartbeats")
            report = _validate_kibak_baseline(engine, 7)
            self.assertFalse(report["is_current"])
            self.assertEqual(report["missing_tables"], ["worker_heartbeats"])

    def test_incremental_kibak_migrations_upgrade_previous_baseline(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine = create_engine(f"sqlite:///{Path(tempdir, 'tenant.db')}")
            self.engines.append(engine)
            old_tables = KIBAK_TENANT_TABLES - {"worker_heartbeats"}
            Base.metadata.create_all(bind=engine, tables=[Base.metadata.tables[name] for name in old_tables])
            Session = sessionmaker(bind=engine)
            with Session.begin() as db:
                db.add(Company(id=7, name="KIBAK Test", active=True))
                db.add(
                    TenantSchemaMigration(
                        company_id=7,
                        version=KIBAK_TENANT_BASELINE_VERSION,
                        name="KIBAK tenant baseline",
                        checksum=KIBAK_TENANT_BASELINE_VERSION,
                        status="current",
                    )
                )
            with Session() as db:
                result = run_migration_plan(
                    engine,
                    db,
                    TenantSchemaMigration,
                    KIBAK_TENANT_SCHEMA_MIGRATIONS,
                    company_id=7,
                    allowed_legacy_versions={KIBAK_TENANT_BASELINE_VERSION},
                )
            self.assertEqual(result["version"], CURRENT_KIBAK_TENANT_SCHEMA_VERSION)
            self.assertTrue(result["is_current"])
            self.assertIn("worker_heartbeats", inspect(engine).get_table_names())
            self.assertNotIn("orders", inspect(engine).get_table_names())


if __name__ == "__main__":
    unittest.main()
