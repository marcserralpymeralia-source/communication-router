from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.dashboard.kibak import kibak_dashboard_summary
from app.db.database import Base
from app.db.models import Communication, Company, Department, Mailbox, RoutingAction, RoutingDecision
from scripts.performance_data import build_performance_fixture, performance_test_client


class KibakDashboardSummaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_summary_calculates_kpis_and_excludes_other_tenants(self):
        received_at = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
        with Session(self.engine) as db:
            db.add_all([Company(id=1, name="KIBAK A"), Company(id=2, name="KIBAK B")])
            db.add_all([
                Mailbox(id=1, company_id=1, name="General", email_address="a@example.com"),
                Mailbox(id=2, company_id=2, name="General", email_address="b@example.com"),
            ])
            db.add_all([
                Department(id=1, company_id=1, name="Finanzas"),
                Department(id=2, company_id=2, name="Finanzas"),
            ])
            automatic = Communication(
                id=1, company_id=1, mailbox_id=1, external_message_id="a-1", subject="Factura", received_at=received_at, routing_status="routed"
            )
            pending = Communication(
                id=2, company_id=1, mailbox_id=1, external_message_id="a-2", subject="Consulta", received_at=received_at, routing_status="pending_review"
            )
            other_tenant = Communication(
                id=3, company_id=2, mailbox_id=2, external_message_id="b-1", subject="Privado", received_at=received_at, routing_status="routed"
            )
            db.add_all([automatic, pending, other_tenant])
            db.flush()
            db.add_all([
                RoutingDecision(
                    id=1, company_id=1, communication_id=1, department_id=1, category="factura", confidence=0.9,
                    requires_review=False, reason="Responsabilidad financiera", status="proposed", source="agent", analysis_number=1,
                    created_at=received_at + timedelta(minutes=12),
                ),
                RoutingDecision(
                    id=2, company_id=1, communication_id=2, department_id=None, category="consulta", confidence=0.55,
                    requires_review=True, reason="Necesita revisión", status="pending_review", source="agent", analysis_number=1,
                    created_at=received_at + timedelta(minutes=4),
                ),
                RoutingDecision(
                    id=3, company_id=2, communication_id=3, department_id=2, category="privado", confidence=0.99,
                    requires_review=False, reason="Otro tenant", status="proposed", source="agent", analysis_number=1,
                ),
            ])
            db.add(
                RoutingAction(
                    id=1, company_id=1, communication_id=1, routing_decision_id=1, department_id=1,
                    action_type="forward", source="automatic", destination_email="finanzas@example.com", status="sent", idempotency_key="a-1-forward",
                )
            )
            db.commit()

            summary = kibak_dashboard_summary(db, company_id=1)

        self.assertEqual(summary["kpis"][0]["value"], 2)
        self.assertEqual(summary["kpis"][1]["value"], 1)
        self.assertEqual(summary["kpis"][2]["value"], 1)
        self.assertEqual(summary["kpis"][4]["value"], "50%")
        self.assertEqual(summary["kpis"][5]["value"], "73%")
        self.assertEqual(summary["kpis"][6]["value"], "8 min")
        self.assertEqual({item["label"] for item in summary["status_distribution"]}, {"Reenviada", "Pendiente de revisión"})
        self.assertEqual([item["subject"] for item in summary["recent"]], ["Consulta", "Factura"])

    def test_empty_summary_is_explicitly_renderable(self):
        with Session(self.engine) as db:
            db.add(Company(id=1, name="KIBAK"))
            db.commit()
            summary = kibak_dashboard_summary(db, company_id=1)

        self.assertFalse(summary["has_data"])
        self.assertEqual(summary["kpis"][0]["value"], 0)
        self.assertEqual(summary["kpis"][4]["value"], "0%")
        self.assertEqual(summary["kpis"][5]["value"], "--")


class KibakDashboardRenderTests(unittest.TestCase):
    def test_dashboard_renders_kibak_empty_state_and_navigation(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                with patch("app.pages.routes._is_postgresql_session", return_value=True):
                    response = client.get("/")
        finally:
            fixture.cleanup()

        self.assertEqual(response.status_code, 200)
        self.assertIn("KIBAK", response.text)
        self.assertIn("No hay comunicaciones todavía.", response.text)
        self.assertIn("Conecta un buzón o carga datos demo para empezar.", response.text)
        self.assertIn('href="/settings/mailboxes"', response.text)
        nav_html = response.text.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
        self.assertEqual(
            nav_html.count('class="nav-label"'),
            6,
        )
        for legacy_label in ("Pedidos", "Productos", "Clientes", "WhatsApp", "Importaciones"):
            self.assertNotIn(f'class="nav-label">{legacy_label}</span>', nav_html)


if __name__ == "__main__":
    unittest.main()
