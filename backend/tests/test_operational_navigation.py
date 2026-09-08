from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from scripts.performance_data import build_performance_fixture, performance_test_client, temporary_performance_environment  # noqa: E402
from app.main import app  # noqa: E402
from app.core.app_factory import create_app  # noqa: E402
from app.core import lifespan as lifespan_module  # noqa: E402
from app.db.models import Email, Order  # noqa: E402


class OperationalNavigationTests(unittest.TestCase):
    def test_canonical_operator_routes_are_registered(self):
        def _collect_routes(app_or_router, prefix=""):
            collected = set()
            for route in getattr(app_or_router, "routes", []):
                if hasattr(route, "path") and hasattr(route, "methods"):
                    collected.add((prefix + route.path, tuple(sorted(route.methods or []))))
                elif hasattr(route, "original_router"):
                    p = getattr(route.include_context, "prefix", "") or ""
                    collected.update(_collect_routes(route.original_router, prefix=prefix + p))
            return collected

        routes = _collect_routes(create_app())
        expected = {
            ("/entries", ("GET",)),
            ("/entries/{entry_id}", ("GET",)),
            ("/entries/{entry_id}/process", ("POST",)),
            ("/entries/{entry_id}/resolve", ("GET",)),
            ("/orders/{order_id}/save", ("POST",)),
            ("/orders/{order_id}/reprocess", ("POST",)),
            ("/orders/{order_id}/validate", ("POST",)),
            ("/orders/{order_id}/export", ("POST",)),
            ("/orders/{order_id}/discard", ("POST",)),
            ("/orders/{order_id}/archive", ("POST",)),
            ("/orders/{order_id}/unarchive", ("POST",)),
            ("/knowledge", ("GET",)),
        }
        for item in expected:
            self.assertIn(item, routes)

    def test_main_navigation_exposes_only_operational_sections(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/")

            self.assertEqual(response.status_code, 200)
            nav_match = re.search(r'<nav class="nav">.*?</nav>', response.text, re.S)
            self.assertIsNotNone(nav_match, "No se encontró el bloque principal de navegación")
            nav_html = nav_match.group(0)

            for label in ("Dashboard", "Comunicaciones", "Departamentos", "Buzones", "Historial", "Configuración"):
                self.assertIn(f'class="nav-label">{label}</span>', nav_html)
            self.assertEqual(
                re.findall(r'class="nav-label">([^<]+)</span>', nav_html),
        ["Dashboard", "Comunicaciones", "Departamentos", "Buzones", "Historial", "Operaciones", "Configuración"],
            )

            for hidden_label in ("Pedidos", "Archivos", "Entradas", "Jobs", "Logs", "Bases de datos", "Diagnóstico", "Aprendizaje", "Canales", "Clientes", "Productos", "WhatsApp"):
                self.assertNotIn(hidden_label, nav_html)
        finally:
            fixture.cleanup()

    def test_entries_canonical_route_is_available_and_knowledge_redirects_to_customers(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                entries = client.get("/entries", follow_redirects=False)
                knowledge = client.get("/knowledge", follow_redirects=False)
                redirected_dashboard = client.get("/")

            self.assertEqual(entries.status_code, 303)
            self.assertEqual(entries.headers["location"], "/")
            self.assertEqual(redirected_dashboard.status_code, 200)
            self.assertIn("Dashboard", redirected_dashboard.text)
            self.assertIn('href="/communications/workbench"', redirected_dashboard.text)
            self.assertNotIn("Vista técnica", redirected_dashboard.text)
            self.assertEqual(knowledge.status_code, 303)
            self.assertEqual(knowledge.headers["location"], "/customers?view=knowledge")
        finally:
            fixture.cleanup()

    def test_history_route_renders_history_page(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                history = client.get("/history", follow_redirects=False)
                legacy_pedidos = client.get("/pedidos", follow_redirects=False)

            self.assertEqual(history.status_code, 200)
            self.assertIn("Buzón de correo", history.text)
            self.assertIn("Bandeja de mensajes", history.text)
            self.assertIn(legacy_pedidos.status_code, (200, 303))
            if legacy_pedidos.status_code == 303:
                self.assertEqual(legacy_pedidos.headers["location"], "/history")
        finally:
            fixture.cleanup()

    def test_products_and_customers_use_compact_list_toolbar(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                products = client.get("/products")
                customers = client.get("/customers?view=list")
                customer_knowledge = client.get("/customers?view=knowledge&status=all&page=1")

            self.assertEqual(products.status_code, 200)
            self.assertIn("<span>Nuevo producto</span>", products.text)
            self.assertIn("Buscar por código, referencia o nombre", products.text)
            self.assertIn(">Importar<", products.text)
            self.assertIn("Más filtros", products.text)
            self.assertIn("database-table-compact", products.text)
            self.assertIn("data-table__primary", products.text)
            self.assertIn("data-table__actions", products.text)
            self.assertIn('data-column="price"', products.text)
            self.assertNotIn("<h3>Importación</h3>", products.text)

            self.assertEqual(customers.status_code, 200)
            self.assertIn("<span>Nuevo cliente</span>", customers.text)
            self.assertIn("Buscar por nombre, código, email o teléfono", customers.text)
            self.assertIn(">Importar<", customers.text)
            self.assertIn("Más filtros", customers.text)
            self.assertIn("database-table-compact", customers.text)
            self.assertIn(">Contacto<", customers.text)
            self.assertIn(">Localidad<", customers.text)
            self.assertIn("data-table__actions", customers.text)
            self.assertNotIn("<span>Importación</span>", customers.text)

            self.assertEqual(customer_knowledge.status_code, 200)
            self.assertIn("database-compact-page", customer_knowledge.text)
            self.assertIn("data-dock", customer_knowledge.text)
            self.assertIn('id="customers-knowledge-filter-form"', customer_knowledge.text)
            self.assertIn("Más filtros", customer_knowledge.text)
            self.assertIn("knowledge-folder-grid", customer_knowledge.text)
            self.assertNotIn('class="panel stack compact-filter-panel"', customer_knowledge.text)
        finally:
            fixture.cleanup()

    def test_settings_checklist_exposes_pending_configuration_modules(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/settings")

            self.assertEqual(response.status_code, 200)
            self.assertIn("1 de 8 requisitos requieren atención", response.text)
            self.assertIn("FTP/SFTP configurado", response.text)
            self.assertIn('data-step-state="pending"', response.text)
            self.assertIn('data-open-settings="ftp"', response.text)
        finally:
            fixture.cleanup()

    def test_entries_redirects_to_login_without_session(self):
        fixture = build_performance_fixture("small")
        try:
            with temporary_performance_environment(fixture):
                test_app = create_app()
                with patch.object(lifespan_module, "start_email_sync_worker", lambda: None), patch.object(lifespan_module, "start_job_worker", lambda: None):
                    with TestClient(test_app, raise_server_exceptions=False) as client:
                        response = client.get("/entries", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/login?next=%2Fentries")
        finally:
            fixture.cleanup()

    def test_entries_keeps_tenant_scope(self):
        fixture = build_performance_fixture("small")
        engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        try:
            with SessionLocal() as db:
                db.add(
                    Email(
                        company_id=999,
                        sender="tenant-b@example.com",
                        subject="Pedido tenant B invisible",
                        body="No debe aparecer en tenant A",
                        status="pending",
                        agent_status="not_processed",
                    )
                )
                db.commit()
            with performance_test_client(fixture) as client:
                response = client.get("/entries", follow_redirects=True)

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("Pedido tenant B invisible", response.text)
        finally:
            engine.dispose()
            fixture.cleanup()

    def test_resolve_entry_with_order_renders_review_screen(self):
        fixture = build_performance_fixture("small")
        engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        try:
            with SessionLocal() as db:
                order = db.get(Order, fixture.order_ids[0])
                self.assertIsNotNone(order)
                email_id = order.email_id
            with performance_test_client(fixture) as client:
                response = client.get(f"/entries/email-{email_id}/resolve", follow_redirects=False)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Confianza del pedido", response.text)
            self.assertIn("Correo", response.text)
        finally:
            engine.dispose()
            fixture.cleanup()

    def test_primary_operational_buttons_do_not_use_empty_links(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                dashboard = client.get("/")
                entries = client.get("/entries")

            self.assertEqual(dashboard.status_code, 200)
            self.assertEqual(entries.status_code, 200)
            self.assertNotIn('href="#"', dashboard.text)
            self.assertNotIn('href="#"', entries.text)
            self.assertNotIn('action=""', dashboard.text)
            self.assertNotIn('action=""', entries.text)
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
