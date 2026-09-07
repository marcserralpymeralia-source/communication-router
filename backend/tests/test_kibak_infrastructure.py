from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy.dialects import postgresql

from app.core.config import DEV_SECRET_KEY, Settings
from app.core.encryption import encrypt_secret
from app.master.models import EncryptedDatabaseUrl, MasterTenantDatabase
from app.core.storage import resolve_temp_storage_dir


class KibakInfrastructureTests(unittest.TestCase):
    def tearDown(self):
        from app.core.config import get_settings

        get_settings.cache_clear()

    def test_settings_defaults_are_kibak_scoped(self):
        with patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.app_name, "KIBAK")
        self.assertEqual(settings.app_slug, "kibak")
        self.assertEqual(settings.database_url, "sqlite:///./kibak_local.db")
        self.assertEqual(settings.master_database_url, "sqlite:///./kibak_master.db")
        self.assertEqual(settings.session_cookie, "kibak_session")
        self.assertEqual(settings.default_company_name, "KIBAK Test")
        self.assertEqual(settings.default_admin_email, "admin@kibak.local")
        self.assertEqual(settings.default_admin_password, "")
        self.assertNotEqual(DEV_SECRET_KEY, "")
        self.assertNotIn("anchi", repr(settings).lower())

    def test_local_storage_defaults_to_backend_storage(self):
        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": "", "VERCEL": ""}, clear=False):
            expected = Path(__file__).resolve().parents[1] / "storage" / "communications" / "attachments"
            self.assertEqual(resolve_temp_storage_dir("communications", "attachments"), expected)

    def test_vercel_storage_default_is_kibak_scoped(self):
        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": "", "VERCEL": "1"}, clear=False):
            self.assertEqual(resolve_temp_storage_dir("communications", "previews"), Path("/tmp/kibak/communications/previews"))

    def test_compose_contains_independent_databases_and_storage(self):
        compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")

        for value in (
            "5433:5432",
            "kibak_local",
            "kibak_master",
            "kibak_tenant_test",
            "kibak_postgres_data",
            "kibak_local_network",
            "docker/postgres/init",
        ):
            self.assertIn(value, compose)
        self.assertNotIn("/anchi", compose)
        self.assertNotIn("postgres:postgres", compose)

    def test_tenant_database_url_contract_supports_legacy_and_encrypted_values(self):
        url = "postgresql+psycopg://kibak_local:dev@localhost:5433/kibak_tenant_test"
        key = Fernet.generate_key().decode()
        with patch.dict(
            os.environ,
            {"APP_ENV": "test", "SECRET_KEY": "kibak-test-secret-" + "x" * 40, "ENCRYPTION_KEY": key},
            clear=False,
        ):
            from app.core.config import get_settings

            get_settings.cache_clear()
            encrypted = encrypt_secret(url)
            mapped_type = EncryptedDatabaseUrl()
            self.assertEqual(mapped_type.process_result_value(encrypted, postgresql.dialect()), url)
            self.assertEqual(mapped_type.process_result_value(url, postgresql.dialect()), url)
            tenant = MasterTenantDatabase(company_id=1, database_key="kibak", database_url=url)
            self.assertEqual(tenant.get_database_url(), url)

            other_key = Fernet.generate_key().decode()
            with patch.dict(os.environ, {"ENCRYPTION_KEY": other_key}, clear=False):
                get_settings.cache_clear()
                with self.assertRaisesRegex(ValueError, "Cannot decrypt tenant database URL"):
                    mapped_type.process_result_value(encrypted, postgresql.dialect())

            with self.assertRaisesRegex(ValueError, "Cannot decrypt tenant database URL"):
                mapped_type.process_result_value("gAAAA-malformed-token", postgresql.dialect())


if __name__ == "__main__":
    unittest.main()
