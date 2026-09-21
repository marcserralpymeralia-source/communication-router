from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.core.attachment_storage import validate_storage_configuration
from app.core.config import Settings
from app.core.config import effective_email_batch_limit
from app.db.models import EmailSettings, LLMSettings
from app.routing.policy import RoutingPolicy
from app.settings.service import update_with_form
from app.core import lifespan as lifespan_module
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
    def test_vercel_git_sha_takes_precedence_over_legacy_release_sha(self):
        with patch.dict(
            os.environ,
            staging_env(VERCEL="1", VERCEL_GIT_COMMIT_SHA="deployed-sha", RELEASE_SHA="legacy-sha"),
            clear=True,
        ):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.release_sha, "deployed-sha")

    def test_release_sha_is_used_without_vercel_metadata(self):
        with patch.dict(
            os.environ,
            staging_env(VERCEL_GIT_COMMIT_SHA="", RELEASE_SHA="configured-sha"),
            clear=True,
        ):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.release_sha, "configured-sha")

    def test_non_vercel_runtime_ignores_vercel_metadata(self):
        with patch.dict(
            os.environ,
            staging_env(VERCEL_GIT_COMMIT_SHA="ignored-sha", RELEASE_SHA="configured-sha"),
            clear=True,
        ):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.release_sha, "configured-sha")

    def test_release_sha_falls_back_to_unknown(self):
        with patch.dict(
            os.environ,
            staging_env(VERCEL="1", VERCEL_GIT_COMMIT_SHA="", RELEASE_SHA=""),
            clear=True,
        ):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.release_sha, "unknown")

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

    def test_free_pilot_requires_staging_and_internal_worker(self):
        with patch.dict(os.environ, staging_env(DEPLOYMENT_MODE="free_pilot", PILOT_FREE_MODE="true", RUN_WORKERS_IN_WEB="true"), clear=True):
            settings = Settings(_env_file=None)
        self.assertTrue(settings.pilot_free_mode)
        self.assertEqual(settings.deployment_mode, "free_pilot")
        self.assertTrue(settings.run_workers_in_web)
        self.assertEqual(settings.pilot_batch_default, 10)
        self.assertEqual(settings.pilot_batch_max, 20)

        with patch.dict(os.environ, staging_env(DEPLOYMENT_MODE="free_pilot", PILOT_FREE_MODE="true", RUN_WORKERS_IN_WEB="false"), clear=True):
            with self.assertRaisesRegex(ValueError, "RUN_WORKERS_IN_WEB"):
                Settings(_env_file=None)

        with patch.dict(os.environ, {"APP_ENV": "production", "PILOT_FREE_MODE": "true"}, clear=True):
            with self.assertRaisesRegex(ValueError, "PILOT_FREE_MODE requires APP_ENV=staging"):
                Settings(_env_file=None)

    def test_free_pilot_policy_and_settings_force_safe_flags(self):
        llm = LLMSettings(company_id=1, auto_forwarding_enabled=True, simulation_mode=False, routing_review_threshold=0.75, routing_auto_threshold=0.9)
        with patch("app.routing.policy.get_settings", return_value=SimpleNamespace(is_pilot_runtime=True)):
            policy = RoutingPolicy.from_settings(llm)
            fallback_policy = RoutingPolicy.from_settings(None)
        self.assertFalse(policy.auto_forwarding_enabled)
        self.assertTrue(policy.simulation_mode)
        self.assertFalse(fallback_policy.auto_forwarding_enabled)
        self.assertTrue(fallback_policy.simulation_mode)

        email = EmailSettings(company_id=1, smtp_enabled=True, auto_sync_enabled=True, mark_as_read_after_import=True)
        with patch("app.settings.service.get_settings", return_value=SimpleNamespace(is_pilot_runtime=True)):
            update_with_form(email, {"smtp_enabled": "on", "auto_sync_enabled": "on", "mark_as_read_after_import": "on"})
        self.assertFalse(email.smtp_enabled)
        self.assertFalse(email.auto_sync_enabled)
        self.assertFalse(email.mark_as_read_after_import)

    def test_free_pilot_batch_limit_defaults_to_ten_and_caps_at_twenty(self):
        settings = SimpleNamespace(is_pilot_runtime=True, pilot_batch_default=10, pilot_batch_max=20)
        with patch("app.core.config.get_settings", return_value=settings):
            self.assertEqual(effective_email_batch_limit(None, standard_default=100, standard_max=100), 10)
            self.assertEqual(effective_email_batch_limit(99, standard_default=100, standard_max=100), 20)
            self.assertEqual(effective_email_batch_limit(7, standard_default=100, standard_max=100), 7)

    def test_vercel_pilot_is_staging_serverless_and_bounded(self):
        with patch.dict(
            os.environ,
            staging_env(
                DEPLOYMENT_MODE="vercel_pilot",
                PILOT_FREE_MODE="false",
                RUN_WORKERS_IN_WEB="false",
            ),
            clear=True,
        ):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.deployment_mode, "vercel_pilot")
        self.assertTrue(settings.is_vercel_pilot)
        self.assertTrue(settings.is_pilot_runtime)
        self.assertFalse(settings.pilot_free_mode)
        self.assertFalse(settings.run_workers_in_web)
        self.assertEqual(settings.pilot_batch_default, 10)
        self.assertEqual(settings.pilot_batch_max, 20)

        with patch.dict(
            os.environ,
            staging_env(DEPLOYMENT_MODE="vercel_pilot", RUN_WORKERS_IN_WEB="true"),
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "RUN_WORKERS_IN_WEB"):
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

    def test_vercel_entrypoint_and_function_budget_are_explicit(self):
        import json

        root = Path(__file__).resolve().parents[2]
        entrypoint = (root / "api" / "index.py").read_text(encoding="utf-8")
        config = json.loads((root / "vercel.json").read_text(encoding="utf-8"))
        self.assertIn("from app.main import app", entrypoint)
        self.assertEqual(config["functions"]["api/index.py"]["maxDuration"], 800)
        self.assertNotIn("crons", config)


class FreePilotLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_free_pilot_starts_jobs_only_and_skips_continuous_email_listener(self):
        master_db = SimpleNamespace(
            scalars=lambda _statement: SimpleNamespace(all=lambda: []),
            close=lambda: None,
        )
        settings = SimpleNamespace(
            app_slug="kibak",
            is_vercel_pilot=False,
            pilot_free_mode=True,
            run_workers_in_web=True,
            environment="staging",
            release_sha="test-release",
            enable_legacy_sync=False,
        )
        with patch.object(lifespan_module, "get_settings", return_value=settings), patch.object(lifespan_module, "init_master_db"), patch.object(lifespan_module, "MasterSessionLocal", return_value=master_db), patch.object(lifespan_module, "start_email_sync_worker") as email_worker, patch.object(lifespan_module, "start_job_worker") as job_worker:
            async with lifespan_module.app_lifespan(SimpleNamespace()):
                pass
        email_worker.assert_not_called()
        job_worker.assert_called_once_with()

    async def test_vercel_pilot_does_not_initialize_schema_or_start_workers(self):
        master_db = SimpleNamespace(
            scalars=lambda _statement: SimpleNamespace(all=lambda: []),
            close=lambda: None,
        )
        settings = SimpleNamespace(
            app_slug="kibak",
            is_vercel_pilot=True,
            pilot_free_mode=False,
            run_workers_in_web=False,
            environment="staging",
            release_sha="test-release",
            enable_legacy_sync=False,
        )
        with patch.object(lifespan_module, "get_settings", return_value=settings), patch.object(lifespan_module, "init_master_db") as init_master, patch.object(lifespan_module, "MasterSessionLocal", return_value=master_db), patch.object(lifespan_module, "start_email_sync_worker") as email_worker, patch.object(lifespan_module, "start_job_worker") as job_worker:
            async with lifespan_module.app_lifespan(SimpleNamespace()):
                pass
        init_master.assert_not_called()
        email_worker.assert_not_called()
        job_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
