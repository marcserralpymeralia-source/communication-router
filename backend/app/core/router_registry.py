from __future__ import annotations

import os

from app.core.config import get_settings


def _router(module_name: str, attribute: str = "router"):
    module = __import__(module_name, fromlist=[attribute])
    return getattr(module, attribute)


def _optional_router(module_name: str, attribute: str = "router"):
    try:
        return _router(module_name, attribute)
    except Exception:  # pragma: no cover - optional phased modules
        return None


def _kibak_routers() -> list:
    # External KIBAK uses a deliberately small route surface. Legacy routers remain
    # importable for historical SQLite fixtures, but are not part of this runtime.
    modules = (
        ("app.auth.routes", "router"),
        ("app.admin.routes", "router"),
        ("app.dashboard.routes", "router"),
        ("app.pages.routes", "router"),
        ("app.onboarding.routes", "router"),
        ("app.operations.routes", "router"),
        ("app.routing.routes", "router"),
        ("app.mailboxes.routes", "router"),
        ("app.communications.routes", "router"),
        ("app.departments.routes", "router"),
        ("app.setup.routes", "router"),
        ("app.jobs.routes", "router"),
        ("app.legal.routes", "router"),
        ("app.settings.routes", "router"),
        ("app.users.routes", "router"),
        ("app.health.routes", "router"),
        ("app.cron.routes", "router"),
    )
    return [router for module, attribute in modules if (router := _optional_router(module, attribute)) is not None]


def _legacy_routers() -> list:
    imports = (
        ("app.auth.routes", "router"),
        ("app.admin.routes", "router"),
        ("app.dashboard.routes", "router"),
        ("app.pages.routes", "router"),
        ("app.channels.routes", "entries_router"),
        ("app.mail.routes", "router"),
        ("app.channels.routes", "router"),
        ("app.workbench.routes", "router"),
        ("app.onboarding.routes", "router"),
        ("app.operations.routes", "router"),
        ("app.routing.routes", "router"),
        ("app.whatsapp.routes", "router"),
        ("app.whatsapp.inbox_routes", "router"),
        ("app.alerts.routes", "router"),
        ("app.learning.routes", "router"),
        ("app.settings.channels_routes", "router"),
        ("app.mailboxes.routes", "router"),
        ("app.communications.routes", "router"),
        ("app.departments.routes", "router"),
        ("app.setup.routes", "router"),
        ("app.orders.routes", "router"),
        ("app.databases.routes", "router"),
        ("app.customers.routes", "router"),
        ("app.products.routes", "router"),
        ("app.imports.routes", "router"),
        ("app.jobs.routes", "router"),
        ("app.legal.routes", "router"),
        ("app.settings.routes", "router"),
        ("app.users.routes", "router"),
        ("app.logs.routes", "router"),
    )
    routers = [router for module, attribute in imports if (router := _optional_router(module, attribute)) is not None]
    for module, attribute in (("app.health.routes", "router"), ("app.cron.routes", "router")):
        if (router := _optional_router(module, attribute)) is not None:
            routers.append(router)
    return routers


def get_registered_routers() -> list:
    settings = get_settings()
    # Test fixtures intentionally exercise the historical SQLite route surface.
    # Read the process environment here so a test that temporarily clears the
    # settings cache cannot leak its development route choice into later tests.
    legacy_fixture = (
        os.environ.get("APP_ENV", "").strip().lower() == "test"
        or os.environ.get("DATABASE_URL", "").strip().lower().startswith("sqlite")
    )
    if legacy_fixture:
        return _legacy_routers()
    if settings.app_slug.strip().lower() == "kibak" and settings.kibak_isolated_routes:
        return _kibak_routers()
    return _legacy_routers()
