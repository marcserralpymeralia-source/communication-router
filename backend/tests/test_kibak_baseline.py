from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.encryption import decrypt_secret  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import MasterTenantDatabase  # noqa: E402
from app.migrations.kibak_baseline import (  # noqa: E402
    KIBAK_BASELINE_CONFIRMATION,
    KIBAK_MASTER_TABLES,
    KIBAK_TENANT_TABLES,
    KibakBaselineError,
    create_kibak_master_schema,
    create_kibak_tenant_schema,
)


class KibakBaselineTests(unittest.TestCase):
    def setUp(self):
        self.engines = []
        self.env = patch.dict(
            os.environ,
            {"APP_ENV": "test", "APP_SLUG": "kibak", "SECRET_KEY": "kibak-test-secret-" + "x" * 40},
            clear=False,
        )
        self.env.start()
        get_settings.cache_clear()

    def tearDown(self):
        for engine in self.engines:
            engine.dispose()
        get_settings.cache_clear()
        self.env.stop()

    def test_empty_master_and_tenant_baselines_exclude_legacy_tables(self):
        with tempfile.TemporaryDirectory() as tempdir:
            master_engine = create_engine(f"sqlite:///{Path(tempdir, 'master.db')}")
            tenant_engine = create_engine(f"sqlite:///{Path(tempdir, 'tenant.db')}")
            self.engines.extend((master_engine, tenant_engine))

            master_result = create_kibak_master_schema(master_engine, confirmation=KIBAK_BASELINE_CONFIRMATION)
            tenant_result = create_kibak_tenant_schema(
                tenant_engine,
                company_id=7,
                company_name="KIBAK Test",
                confirmation=KIBAK_BASELINE_CONFIRMATION,
            )

            master_tables = set(inspect(master_engine).get_table_names())
            tenant_tables = set(inspect(tenant_engine).get_table_names())
            self.assertEqual(master_tables, KIBAK_MASTER_TABLES)
            self.assertEqual(tenant_tables, KIBAK_TENANT_TABLES)
            self.assertNotIn("orders", tenant_tables)
            self.assertNotIn("products", tenant_tables)
            self.assertNotIn("customers", tenant_tables)
            self.assertNotIn("emails", tenant_tables)
            self.assertNotIn("inbound_messages", tenant_tables)
            self.assertEqual(master_result["version"], "kibak.master.1")
            self.assertEqual(tenant_result["version"], "kibak.tenant.1")

    def test_baseline_rejects_non_empty_or_anchi_target(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine = create_engine(f"sqlite:///{Path(tempdir, 'tenant.db')}")
            self.engines.append(engine)
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE existing_table (id INTEGER PRIMARY KEY)"))
            with self.assertRaises(KibakBaselineError):
                create_kibak_master_schema(engine, confirmation=KIBAK_BASELINE_CONFIRMATION)

            anchi_engine = create_engine(f"sqlite:///{Path(tempdir, 'anchi_kibak_test.db')}")
            self.engines.append(anchi_engine)
            try:
                with self.assertRaises(KibakBaselineError):
                    create_kibak_master_schema(anchi_engine, confirmation=KIBAK_BASELINE_CONFIRMATION)
            finally:
                anchi_engine.dispose()

    def test_database_url_is_encrypted_at_rest_and_read_compatibly(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine = create_engine(f"sqlite:///{Path(tempdir, 'master.db')}")
            self.engines.append(engine)
            MasterBase.metadata.create_all(engine, tables=[MasterBase.metadata.tables["companies"], MasterBase.metadata.tables["tenant_databases"]])
            Session = sessionmaker(bind=engine)
            db = Session()
            db.add(MasterTenantDatabase(company_id=1, database_key="kibak", database_url="postgresql://user:secret@localhost/kibak"))
            db.commit()
            db.close()

            with engine.connect() as conn:
                raw = conn.execute(text("SELECT database_url FROM tenant_databases")).scalar_one()
            self.assertNotIn("secret", raw)
            self.assertEqual(decrypt_secret(raw), "postgresql://user:secret@localhost/kibak")


if __name__ == "__main__":
    unittest.main()
