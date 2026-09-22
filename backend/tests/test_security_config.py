from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("APP_ENV", "development")

from app.core.app_factory import create_app
from app.core.config import DEV_SECRET_KEY, get_settings
from app.core.encryption import encrypt_secret, mask_secret
from app.settings.service import update_with_form
from app.db.models import EmailSettings
from cryptography.fernet import Fernet


VALID_FERNET_KEY = Fernet.generate_key().decode()


def _load_settings(env: dict[str, str]) -> object:
    baseline = {
        "APP_ENV": "development",
        "ENVIRONMENT": "development",
        "SECRET_KEY": "a-very-strong-secret-key-for-development-123456",
        "ENCRYPTION_KEY": VALID_FERNET_KEY,
        "DEBUG": "false",
        "SESSION_COOKIE_SECURE": "false",
        "SESSION_COOKIE_SAMESITE": "lax",
        "SESSION_MAX_AGE": "604800",
        "ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
        "CORS_ALLOWED_ORIGINS": "http://localhost:8000,http://127.0.0.1:8000",
        "DATABASE_URL": "sqlite:///./anchi_demo.db",
        "MASTER_DATABASE_URL": "sqlite:///./master.db",
        "DEFAULT_ADMIN_EMAIL": "ops@example.com",
        "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
    }
    baseline.update(env)
    if "ENVIRONMENT" not in env and "APP_ENV" in env:
        baseline["ENVIRONMENT"] = env["APP_ENV"]
    runtime_env = baseline.get("APP_ENV", "development")
    if runtime_env == "demo":
        if "TENANT_DB_MODE" not in env:
            baseline["TENANT_DB_MODE"] = "external"
        if "DATABASE_URL" not in env:
            baseline["DATABASE_URL"] = "postgresql+psycopg://user:password@db.example.com:5432/anchi_demo"
        if "TENANT_DATABASE_URL" not in env:
            baseline["TENANT_DATABASE_URL"] = baseline["DATABASE_URL"]
        if "MASTER_DATABASE_URL" not in env:
            baseline["MASTER_DATABASE_URL"] = "postgresql+psycopg://user:password@db.example.com:5432/anchi_master"
    if "ENABLE_DEMO_BOOTSTRAP" not in env and "SEED_DEMO_DATA" not in env:
        baseline["ENABLE_DEMO_BOOTSTRAP"] = "true" if runtime_env == "development" else "false"
    if runtime_env == "production":
        if "SESSION_COOKIE_SECURE" not in env:
            baseline["SESSION_COOKIE_SECURE"] = "true"
        if "ALLOWED_HOSTS" not in env:
            baseline["ALLOWED_HOSTS"] = "app.example.com"
        if "CORS_ALLOWED_ORIGINS" not in env:
            baseline["CORS_ALLOWED_ORIGINS"] = "https://app.example.com"
        if "DATABASE_URL" not in env:
            baseline["DATABASE_URL"] = "postgresql+psycopg://user:password@db.example.com:5432/anchi"
        if "MASTER_DATABASE_URL" not in env:
            baseline["MASTER_DATABASE_URL"] = "postgresql+psycopg://user:password@db.example.com:5432/anchi_master"
        if "DEFAULT_ADMIN_EMAIL" not in env:
            baseline["DEFAULT_ADMIN_EMAIL"] = "ops@example.com"
        if "DEFAULT_ADMIN_PASSWORD" not in env:
            baseline["DEFAULT_ADMIN_PASSWORD"] = "StrongPassw0rd!2026"
    if runtime_env == "staging":
        baseline.update(
            {
                "APP_URL": "https://pilot.example.com",
                "AUTH_SECRET": "kibak-staging-auth-" + "y" * 40,
                "SESSION_COOKIE_SECURE": "true",
                "ALLOWED_HOSTS": "pilot.example.com",
                "CORS_ALLOWED_ORIGINS": "https://pilot.example.com",
                "TENANT_DB_MODE": "external",
                "DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/kibak_tenant_pilot",
                "TENANT_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/kibak_tenant_pilot",
                "MASTER_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/kibak_master",
                "STORAGE_BACKEND": "s3",
                "S3_BUCKET": "kibak-pilot",
                "S3_ACCESS_KEY_ID": "access",
                "S3_SECRET_ACCESS_KEY": "secret",
                "ENABLE_DEMO_BOOTSTRAP": "false",
            }
        )
    with patch.dict(os.environ, baseline, clear=True):
        get_settings.cache_clear()
        return get_settings()


class SecurityConfigurationTests(unittest.TestCase):
    def tearDown(self):
        get_settings.cache_clear()

    def test_environment_accepts_allowed_values(self):
        for value in ["development", "demo", "test", "staging", "production"]:
            settings = _load_settings({"APP_ENV": value})
            self.assertEqual(settings.environment, value)

    def test_environment_rejects_unknown_value(self):
        with patch.dict(os.environ, {"APP_ENV": "unknown"}, clear=True):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "APP_ENV must be development, demo, test, staging or production"):
                get_settings()

    def test_development_keeps_local_defaults(self):
        settings = _load_settings({"APP_ENV": "development"})
        self.assertFalse(settings.session_cookie_secure)
        self.assertEqual(settings.session_cookie_samesite, "lax")
        self.assertGreater(settings.session_max_age, 0)
        self.assertIn("localhost", settings.allowed_hosts)
        self.assertTrue(settings.cors_allowed_origins)
        self.assertTrue(settings.seed_demo_data)

    def test_vercel_runtime_hosts_are_added_without_wildcard(self):
        vercel_env = {
            "APP_ENV": "production",
            "VERCEL": "1",
            "VERCEL_URL": "kibak-pilot-deployment.vercel.app",
            "VERCEL_BRANCH_URL": "kibak-pilot-git-feature.vercel.app",
            "VERCEL_PROJECT_PRODUCTION_URL": "kibak-pilot.vercel.app",
        }
        with patch.dict(os.environ, vercel_env, clear=False):
            settings = _load_settings(vercel_env)
            self.assertIn("kibak-pilot-deployment.vercel.app", settings.allowed_hosts)
            self.assertIn("kibak-pilot-git-feature.vercel.app", settings.allowed_hosts)
            self.assertIn("kibak-pilot.vercel.app", settings.allowed_hosts)
            self.assertNotIn("*", settings.allowed_hosts)

    def test_meta_embedded_signup_requires_https_and_keeps_server_secrets_redacted(self):
        meta_app_secret = "meta-server-secret-for-test"
        verify_token = "meta-global-verify-token-for-test"
        settings = _load_settings(
            {
                "APP_ENV": "development",
                "APP_URL": "https://anchi.example.com",
                "META_APP_ID": "12345000000",
                "META_APP_SECRET": meta_app_secret,
                "META_EMBEDDED_SIGNUP_CONFIG_ID": "22345000000",
                "META_WHATSAPP_VERIFY_TOKEN": verify_token,
            }
        )
        self.assertTrue(settings.meta_whatsapp_embedded_signup_ready)
        self.assertEqual(settings.meta_whatsapp_missing_configuration, [])
        rendered = repr(settings)
        self.assertNotIn(meta_app_secret, rendered)
        self.assertNotIn(verify_token, rendered)
        self.assertIn("[redacted]", rendered)

        local_settings = _load_settings({"APP_ENV": "development"})
        self.assertFalse(local_settings.meta_whatsapp_embedded_signup_ready)
        self.assertIn("APP_URL (HTTPS)", local_settings.meta_whatsapp_missing_configuration)

    def test_embedded_signup_does_not_require_global_legacy_webhook_token(self):
        settings = _load_settings(
            {
                "APP_ENV": "development",
                "APP_URL": "https://anchi.example.com",
                "META_APP_ID": "12345000000",
                "META_APP_SECRET": "meta-server-secret-for-test",
                "META_EMBEDDED_SIGNUP_CONFIG_ID": "22345000000",
            }
        )

        self.assertTrue(settings.meta_whatsapp_embedded_signup_ready)
        self.assertEqual(settings.meta_whatsapp_embedded_signup_missing_configuration, [])
        self.assertIn("META_WHATSAPP_VERIFY_TOKEN", settings.meta_whatsapp_missing_configuration)

    def test_demo_environment_keeps_demo_bootstrap(self):
        settings = _load_settings({"APP_ENV": "demo"})
        self.assertEqual(settings.environment, "demo")
        self.assertFalse(settings.session_cookie_secure)
        self.assertTrue(settings.seed_demo_data)
        self.assertEqual(settings.tenant_db_mode, "external")

    def test_demo_environment_rejects_sqlite_without_external_db(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "demo",
                "ENVIRONMENT": "demo",
                "SECRET_KEY": "a-very-strong-secret-key-for-development-123456",
                "ENCRYPTION_KEY": VALID_FERNET_KEY,
                "DATABASE_URL": "sqlite:///./anchi_demo.db",
                "MASTER_DATABASE_URL": "sqlite:///./master.db",
                "TENANT_DB_MODE": "external",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "MASTER_DATABASE_URL must point to an external database in demo or Vercel"):
                get_settings()

    def test_test_environment_uses_isolated_defaults(self):
        settings = _load_settings({"APP_ENV": "test"})
        self.assertEqual(settings.environment, "test")
        self.assertFalse(settings.debug)
        self.assertFalse(settings.session_cookie_secure)
        self.assertFalse(settings.seed_demo_data)

    def test_production_accepts_safe_configuration(self):
        settings = _load_settings(
            {
                "APP_ENV": "production",
                "ENVIRONMENT": "production",
                "SECRET_KEY": "a-very-strong-secret-key-for-production-123456",
                "ENCRYPTION_KEY": VALID_FERNET_KEY,
                "ENABLE_DEMO_BOOTSTRAP": "false",
                "DEBUG": "false",
                "SESSION_COOKIE_SECURE": "true",
                "SESSION_COOKIE_SAMESITE": "lax",
                "SESSION_MAX_AGE": "604800",
                "ALLOWED_HOSTS": "app.example.com",
                "CORS_ALLOWED_ORIGINS": "https://app.example.com",
                "DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi",
                "MASTER_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi_master",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
            }
        )
        self.assertEqual(settings.environment, "production")
        self.assertTrue(settings.session_cookie_secure)
        self.assertEqual(settings.allowed_hosts, ["app.example.com"])

    def test_production_rejects_insecure_secret_key(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ENVIRONMENT": "production",
                "SECRET_KEY": DEV_SECRET_KEY,
                "ENCRYPTION_KEY": VALID_FERNET_KEY,
                "ENABLE_DEMO_BOOTSTRAP": "false",
                "DEBUG": "false",
                "SESSION_COOKIE_SECURE": "true",
                "ALLOWED_HOSTS": "app.example.com",
                "DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi",
                "MASTER_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi_master",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "Unsafe SECRET_KEY for production"):
                get_settings()

    def test_production_rejects_short_secret_key(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ENVIRONMENT": "production",
                "SECRET_KEY": "short-key",
                "ENCRYPTION_KEY": VALID_FERNET_KEY,
                "ENABLE_DEMO_BOOTSTRAP": "false",
                "DEBUG": "false",
                "SESSION_COOKIE_SECURE": "true",
                "ALLOWED_HOSTS": "app.example.com",
                "DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi",
                "MASTER_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi_master",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "Unsafe SECRET_KEY for production"):
                get_settings()

    def test_production_rejects_invalid_encryption_key(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ENVIRONMENT": "production",
                "SECRET_KEY": "a-very-strong-secret-key-for-production-123456",
                "ENCRYPTION_KEY": "invalid-key",
                "ENABLE_DEMO_BOOTSTRAP": "false",
                "DEBUG": "false",
                "SESSION_COOKIE_SECURE": "true",
                "ALLOWED_HOSTS": "app.example.com",
                "DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi",
                "MASTER_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi_master",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "ENCRYPTION_KEY must be a valid Fernet key"):
                get_settings()

    def test_update_with_form_preserves_secret_when_field_is_missing(self):
        settings = EmailSettings(company_id=1, imap_password_encrypted=encrypt_secret("demo-password"))
        previous = settings.imap_password_encrypted

        update_with_form(settings, {"imap_username": "demo@example.com"}, {"imap_password_encrypted"})

        self.assertEqual(settings.imap_password_encrypted, previous)
        self.assertEqual(mask_secret(settings.imap_password_encrypted), "••••••••")

    def test_production_rejects_demo_bootstrap(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ENVIRONMENT": "production",
                "SECRET_KEY": "a-very-strong-secret-key-for-production-123456",
                "ENCRYPTION_KEY": VALID_FERNET_KEY,
                "ENABLE_DEMO_BOOTSTRAP": "true",
                "DEBUG": "false",
                "SESSION_COOKIE_SECURE": "true",
                "ALLOWED_HOSTS": "app.example.com",
                "DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi",
                "MASTER_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi_master",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "ENABLE_DEMO_BOOTSTRAP cannot be enabled in production"):
                get_settings()

    def test_production_rejects_debug(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ENVIRONMENT": "production",
                "SECRET_KEY": "a-very-strong-secret-key-for-production-123456",
                "ENCRYPTION_KEY": VALID_FERNET_KEY,
                "ENABLE_DEMO_BOOTSTRAP": "false",
                "DEBUG": "true",
                "SESSION_COOKIE_SECURE": "true",
                "ALLOWED_HOSTS": "app.example.com",
                "DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi",
                "MASTER_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi_master",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "DEBUG cannot be enabled in production"):
                get_settings()

    def test_production_rejects_hosts_wildcard(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ENVIRONMENT": "production",
                "SECRET_KEY": "a-very-strong-secret-key-for-production-123456",
                "ENCRYPTION_KEY": VALID_FERNET_KEY,
                "ENABLE_DEMO_BOOTSTRAP": "false",
                "DEBUG": "false",
                "SESSION_COOKIE_SECURE": "true",
                "ALLOWED_HOSTS": "*",
                "CORS_ALLOWED_ORIGINS": "https://app.example.com",
                "DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi",
                "MASTER_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi_master",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "ALLOWED_HOSTS cannot contain \\* in production"):
                get_settings()

    def test_production_rejects_cors_wildcard(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ENVIRONMENT": "production",
                "SECRET_KEY": "a-very-strong-secret-key-for-production-123456",
                "ENCRYPTION_KEY": VALID_FERNET_KEY,
                "ENABLE_DEMO_BOOTSTRAP": "false",
                "DEBUG": "false",
                "SESSION_COOKIE_SECURE": "true",
                "ALLOWED_HOSTS": "app.example.com",
                "CORS_ALLOWED_ORIGINS": "*",
                "DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi",
                "MASTER_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi_master",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "CORS wildcard is not allowed with credentials"):
                get_settings()

    def test_production_rejects_empty_hosts(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ENVIRONMENT": "production",
                "SECRET_KEY": "a-very-strong-secret-key-for-production-123456",
                "ENCRYPTION_KEY": VALID_FERNET_KEY,
                "ENABLE_DEMO_BOOTSTRAP": "false",
                "DEBUG": "false",
                "SESSION_COOKIE_SECURE": "true",
                "ALLOWED_HOSTS": "",
                "DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi",
                "MASTER_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi_master",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "ALLOWED_HOSTS must be explicit in production"):
                get_settings()

    def test_secret_repr_and_masking_do_not_expose_values(self):
        settings = _load_settings({"APP_ENV": "development", "SECRET_KEY": "x" * 48, "ENCRYPTION_KEY": VALID_FERNET_KEY})
        repr_value = repr(settings)
        self.assertNotIn("x" * 48, repr_value)
        self.assertNotIn("admin123", repr_value)
        secret = encrypt_secret("super-secret-value")
        self.assertEqual(mask_secret(secret), "••••••••")

    def test_secret_update_flow_preserves_blank_value(self):
        settings = EmailSettings(company_id=1, imap_password_encrypted=encrypt_secret("old-value"))
        original = settings.imap_password_encrypted
        update_with_form(settings, {"imap_password_encrypted": ""}, {"imap_password_encrypted"})
        self.assertEqual(settings.imap_password_encrypted, original)

    def test_secret_update_flow_preserves_masked_value(self):
        settings = EmailSettings(company_id=1, imap_password_encrypted=encrypt_secret("old-value"))
        original = settings.imap_password_encrypted
        update_with_form(settings, {"imap_password_encrypted": "********"}, {"imap_password_encrypted"})
        self.assertEqual(settings.imap_password_encrypted, original)

    def test_login_template_no_longer_prefills_demo_credentials(self):
        login_template = Path(__file__).resolve().parents[1] / "app" / "templates" / "login.html"
        source = login_template.read_text(encoding="utf-8")
        self.assertNotIn("default_admin_password", source)
        self.assertNotIn("default_admin_email", source)

    def test_create_app_uses_security_middlewares(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "ENVIRONMENT": "development",
                "SECRET_KEY": "a-very-strong-secret-key-for-development-123456",
                "ENABLE_DEMO_BOOTSTRAP": "true",
                "SESSION_COOKIE_SECURE": "false",
                "ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
                "CORS_ALLOWED_ORIGINS": "http://localhost:8000,http://127.0.0.1:8000",
                "DATABASE_URL": "sqlite:///./anchi_demo.db",
                "MASTER_DATABASE_URL": "sqlite:///./master.db",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            app = create_app()
            middleware_names = {middleware.cls.__name__ for middleware in app.user_middleware}
            self.assertIn("SessionMiddleware", middleware_names)
            self.assertIn("TrustedHostMiddleware", middleware_names)
            self.assertIn("CORSMiddleware", middleware_names)
            self.assertFalse(app.debug)

            session_middleware = next(middleware for middleware in app.user_middleware if middleware.cls.__name__ == "SessionMiddleware")
            self.assertFalse(session_middleware.kwargs["https_only"])
            self.assertEqual(session_middleware.kwargs["same_site"], "lax")

    def test_create_app_in_production_uses_secure_session_cookie(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "ENVIRONMENT": "production",
                "SECRET_KEY": "a-very-strong-secret-key-for-production-123456",
                "ENCRYPTION_KEY": VALID_FERNET_KEY,
                "ENABLE_DEMO_BOOTSTRAP": "false",
                "DEBUG": "false",
                "SESSION_COOKIE_SECURE": "true",
                "SESSION_COOKIE_SAMESITE": "lax",
                "ALLOWED_HOSTS": "app.example.com",
                "CORS_ALLOWED_ORIGINS": "https://app.example.com",
                "DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi",
                "MASTER_DATABASE_URL": "postgresql+psycopg://user:password@db.example.com:5432/anchi_master",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "StrongPassw0rd!2026",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            app = create_app()
            middleware_names = {middleware.cls.__name__ for middleware in app.user_middleware}
            self.assertIn("SessionMiddleware", middleware_names)
            self.assertIn("TrustedHostMiddleware", middleware_names)
            self.assertIn("CORSMiddleware", middleware_names)

            session_middleware = next(middleware for middleware in app.user_middleware if middleware.cls.__name__ == "SessionMiddleware")
            self.assertTrue(session_middleware.kwargs["https_only"])
            self.assertEqual(session_middleware.kwargs["same_site"], "lax")
            self.assertFalse(app.debug)


if __name__ == "__main__":
    unittest.main()
