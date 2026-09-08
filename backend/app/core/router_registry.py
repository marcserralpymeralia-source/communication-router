from __future__ import annotations

from app.auth.routes import router as auth_router
from app.admin.routes import router as admin_router
from app.dashboard.routes import router as dashboard_router
from app.alerts.routes import router as alerts_router
from app.channels.routes import entries_router, router as channels_router
from app.customers.routes import router as customers_router
from app.databases.routes import router as databases_router
from app.imports.routes import router as imports_router
from app.jobs.routes import router as jobs_router
from app.legal.routes import router as legal_router
from app.logs.routes import router as logs_router
from app.mail.routes import router as mail_router
from app.learning.routes import router as learning_router
from app.pages.routes import router as pages_router
from app.orders.routes import router as orders_router
from app.products.routes import router as products_router
from app.whatsapp.routes import router as whatsapp_router
from app.whatsapp.inbox_routes import router as whatsapp_inbox_router
from app.settings.channels_routes import router as channels_settings_router
from app.mailboxes.routes import router as mailboxes_router
from app.communications.routes import router as communications_router
from app.departments.routes import router as departments_router
from app.settings.routes import router as settings_router
from app.setup.routes import router as setup_router
from app.users.routes import router as users_router
from app.workbench.routes import router as workbench_router
from app.onboarding.routes import router as onboarding_router
from app.operations.routes import router as operations_router
from app.routing.routes import router as routing_router

try:  # pragma: no cover - optional during phased extraction
    from app.health.routes import router as health_router
except Exception:  # noqa: BLE001
    health_router = None

try:  # pragma: no cover - optional during phased extraction
    from app.cron.routes import router as cron_router
except Exception:  # noqa: BLE001
    cron_router = None


def get_registered_routers() -> list:
    routers = [
        auth_router,
        admin_router,
        dashboard_router,
        pages_router,
        entries_router,
        mail_router,
        channels_router,
        workbench_router,
        onboarding_router,
        operations_router,
        routing_router,
        whatsapp_router,
        whatsapp_inbox_router,
        alerts_router,
        learning_router,
        channels_settings_router,
        mailboxes_router,
        communications_router,
        departments_router,
        setup_router,
        orders_router,
        databases_router,
        customers_router,
        products_router,
        imports_router,
        jobs_router,
        legal_router,
        settings_router,
        users_router,
        logs_router,
    ]
    if health_router is not None:
        routers.append(health_router)
    if cron_router is not None:
        routers.append(cron_router)
    return routers
