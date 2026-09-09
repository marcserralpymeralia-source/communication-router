from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import Settings, get_settings
from app.core.database_urls import resolve_tenant_database_url
from app.tenancy.database import get_tenant_engine


class TenantDatabaseResolutionTests(unittest.TestCase):
    def tearDown(self):
        get_tenant_engine.cache_clear()
        get_settings.cache_clear()

    def test_host_url_remains_canonical_without_runtime_override(self):
        settings = Settings(_env_file=None)
        url = "postgresql+psycopg://kibak_local:dev@localhost:5433/kibak_tenant_pilot"
        self.assertEqual(resolve_tenant_database_url(url, settings=settings), url)

    def test_docker_runtime_override_preserves_database_identity(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "TENANT_DATABASE_RUNTIME_HOST": "postgres",
                "TENANT_DATABASE_RUNTIME_PORT": "5432",
            },
            clear=False,
        ):
            settings = Settings(_env_file=None)
            canonical = "postgresql+psycopg://kibak_local:dev@localhost:5433/kibak_tenant_pilot"
            resolved = make_url(resolve_tenant_database_url(canonical, settings=settings))
            self.assertEqual(resolved.host, "postgres")
            self.assertEqual(resolved.port, 5432)
            self.assertEqual(resolved.database, "kibak_tenant_pilot")
            self.assertEqual(resolved.username, "kibak_local")
            self.assertEqual(resolved.password, "dev")

    def test_tenant_engine_uses_resolved_runtime_endpoint(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "TENANT_DATABASE_RUNTIME_HOST": "postgres",
                "TENANT_DATABASE_RUNTIME_PORT": "5432",
            },
            clear=False,
        ):
            get_settings.cache_clear()
            get_tenant_engine.cache_clear()
            engine = get_tenant_engine("postgresql+psycopg://kibak_local:dev@localhost:5433/kibak_tenant_pilot")
            self.assertEqual(engine.url.host, "postgres")
            self.assertEqual(engine.url.port, 5432)
            self.assertEqual(engine.url.database, "kibak_tenant_pilot")
            engine.dispose()

    def test_web_and_worker_declare_the_same_docker_runtime_contract(self):
        compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
        service_blocks = {}
        for service, next_service in (("web", "worker"), ("worker", "postgres")):
            match = re.search(
                rf"(?ms)^  {service}:\n(.*?)(?=^  {next_service}:\n)",
                compose,
            )
            self.assertIsNotNone(match)
            service_blocks[service] = match.group(1)
        for block in service_blocks.values():
            self.assertIn("TENANT_DATABASE_RUNTIME_HOST: postgres", block)
            self.assertIn("TENANT_DATABASE_RUNTIME_PORT: 5432", block)
        self.assertEqual(
            service_blocks["web"].count("TENANT_DATABASE_RUNTIME_HOST: postgres"),
            1,
        )
        self.assertEqual(
            service_blocks["worker"].count("TENANT_DATABASE_RUNTIME_HOST: postgres"),
            1,
        )

    def test_sqlite_urls_are_never_rewritten(self):
        settings = Settings(_env_file=None)
        url = "sqlite:////tmp/kibak_tenant_pilot.db"
        self.assertEqual(resolve_tenant_database_url(url, settings=settings), url)


if __name__ == "__main__":
    unittest.main()
