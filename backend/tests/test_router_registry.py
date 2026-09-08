from __future__ import annotations

import os
import unittest
from unittest.mock import patch

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
