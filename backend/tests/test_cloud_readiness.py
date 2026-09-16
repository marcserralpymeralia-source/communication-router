from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.core.attachment_storage import validate_storage_configuration
from app.core.config import Settings
def staging_env(**overrides: str) -> dict[str, str]:
    values = {
        "APP_ENV": "staging",
        "APP_SLUG": "kibak",
        "APP_URL": "https://pilot.example.com",
        "SECRET_KEY": "kibak-staging-secret-" + "x" * 40,
        "AUTH_SECRET": "kibak-staging-auth-" + "y" * 40,
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "DEBUG": "false",
        "SESSION_COOKIE_SECURE": "true",
        "SESSION_COOKIE_SAMESITE": "lax",
        "ALLOWED_HOSTS": "pilot.example.com",
        "CORS_ALLOWED_ORIGINS": "https://pilot.example.com",
        "TENANT_DB_MODE": "external",
        "MASTER_DATABASE_URL": "postgresql+psycopg://master:password@neon.example/kibak_master",
        "DATABASE_URL": "postgresql+psycopg://tenant:password@neon.example/kibak_tenant_pilot",
        "TENANT_DATABASE_URL": "postgresql+psycopg://tenant:password@neon.example/kibak_tenant_pilot",
        "STORAGE_BACKEND": "s3",
        "S3_BUCKET": "kibak-pilot",
        "S3_ACCESS_KEY_ID": "access",
        "S3_SECRET_ACCESS_KEY": "secret",
        "ENABLE_DEMO_BOOTSTRAP": "false",
        "RUN_WORKERS_IN_WEB": "false",
    }
    values.update(overrides)
    return values


class CloudReadinessTests(unittest.TestCase):
    def test_staging_requires_secure_explicit_runtime(self):
        with patch.dict(os.environ, staging_env(), clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.environment, "staging")
        self.assertTrue(settings.session_cookie_secure)
        self.assertFalse(settings.run_workers_in_web)
        self.assertEqual(settings.allowed_hosts, ["pilot.example.com"])
        self.assertEqual(settings.cors_allowed_origins, ["https://pilot.example.com"])

    def test_staging_rejects_localhost_or_sqlite(self):
        with patch.dict(os.environ, staging_env(APP_URL="http://localhost:8000"), clear=True):
            with self.assertRaisesRegex(ValueError, "APP_URL must be an HTTPS URL"):
                Settings(_env_file=None)
        with patch.dict(os.environ, staging_env(TENANT_DB_MODE="sqlite"), clear=True):
            with self.assertRaisesRegex(ValueError, "external PostgreSQL"):
                Settings(_env_file=None)

    def test_staging_requires_private_object_storage(self):
        with patch.dict(os.environ, staging_env(STORAGE_BACKEND="local"), clear=True):
            with self.assertRaisesRegex(ValueError, "STORAGE_BACKEND must be s3"):
                Settings(_env_file=None)

    def test_storage_validation_is_configuration_only(self):
        settings = SimpleNamespace(
            storage_backend="s3",
            s3_bucket="bucket",
            s3_access_key_id="access",
            s3_secret_access_key=SimpleNamespace(get_secret_value=lambda: "secret"),
        )
        with patch("app.core.attachment_storage.get_settings", return_value=settings):
            result = validate_storage_configuration()
        self.assertEqual(result, {"ok": True, "backend": "s3", "persistent": True, "missing": []})


class DeploymentContractTests(unittest.TestCase):
    def test_dockerfile_uses_render_port_and_worker_command_is_stable(self):
        root = Path(__file__).resolve().parents[2]
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("${PORT:-8000}", dockerfile)
        self.assertIn("python -m app.workers.jobs_worker", (root / "Procfile").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
