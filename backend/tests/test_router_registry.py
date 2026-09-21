from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.app_factory import create_app
from app.core.config import get_settings
from app.core.router_registry import get_registered_routers


class RouterRegistryTests(unittest.TestCase):
    def tearDown(self):
        get_settings.cache_clear()

    def _paths(self, **environment: str) -> set[str]:
        get_settings.cache_clear()
        with patch.dict(os.environ, environment, clear=True):
            return {route.path for router in get_registered_routers() for route in router.routes}

    def test_kibak_defaults_to_isolated_route_surface(self):
        paths = self._paths(APP_ENV="development", APP_SLUG="kibak")

        self.assertIn("/communications/workbench", paths)
        self.assertIn("/health/ready", paths)
        self.assertNotIn("/orders", paths)
        self.assertNotIn("/products", paths)
        self.assertNotIn("/mail", paths)
        self.assertNotIn("/workbench", paths)

    def test_legacy_route_surface_requires_explicit_opt_out(self):
        paths = self._paths(APP_ENV="development", APP_SLUG="kibak", KIBAK_ISOLATED_ROUTES="false")

        self.assertIn("/orders", paths)
        self.assertIn("/products", paths)
        self.assertIn("/mail", paths)
        self.assertIn("/workbench", paths)

    def test_mailbox_page_and_administrative_backfill_routes_are_registered(self):
        get_settings.cache_clear()
        with patch.dict(os.environ, {"APP_ENV": "development", "APP_SLUG": "kibak"}, clear=True):
            signatures = {
                (route.path, tuple(sorted(route.methods or [])))
                for router in get_registered_routers()
                for route in router.routes
            }

        self.assertIn(("/settings/mailboxes", ("GET",)), signatures)
        self.assertIn(("/settings/mailboxes", ("POST",)), signatures)
        self.assertIn(("/settings/mailboxes/{mailbox_id}/backfill-once", ("POST",)), signatures)

    def test_mailbox_page_get_reaches_auth_guard_without_session(self):
        get_settings.cache_clear()
        with patch.dict(os.environ, {"APP_ENV": "development", "APP_SLUG": "kibak"}, clear=True):
            get_settings.cache_clear()
            client = TestClient(create_app(), raise_server_exceptions=False)
            response = client.get("/settings/mailboxes", follow_redirects=False)

        self.assertIn(response.status_code, {200, 303})
