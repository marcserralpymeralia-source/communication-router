from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import Communication, Department, Mailbox, RoutingDecision
from scripts.performance_data import build_performance_fixture, performance_test_client


class KibakProductUxRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = build_performance_fixture("small")
        self.engine = create_engine(self.fixture.tenant_database_url)

    def tearDown(self):
        self.engine.dispose()
        self.fixture.cleanup()

    def test_onboarding_and_operations_are_available_without_legacy_payloads(self):
        with performance_test_client(self.fixture) as client:
            onboarding = client.get("/onboarding")
            operations = client.get("/operations")

        self.assertEqual(onboarding.status_code, 200)
        self.assertIn("Configura tu espacio de trabajo", onboarding.text)
        self.assertIn("Empresa", onboarding.text)
        self.assertIn("Automatización", onboarding.text)
        self.assertEqual(operations.status_code, 200)
        self.assertIn("Centro de operaciones", operations.text)
        self.assertNotIn("payload_json", operations.text)
        self.assertNotIn("stack trace", operations.text.lower())

    def test_kibak_history_filters_by_department_category_and_review_state(self):
        with Session(self.engine) as db:
            department = Department(company_id=1, name="Operaciones", destination_email="ops@example.com")
            mailbox = Mailbox(company_id=1, name="Entrada", email_address="entrada@example.com")
            db.add_all([department, mailbox])
            db.flush()
            communication = Communication(
                company_id=1,
                mailbox_id=mailbox.id,
                external_message_id="<history-filter@example.com>",
                sender_email="cliente@example.com",
                subject="Incidencia de entrega",
                body_text="La entrega necesita revisión.",
                received_at=datetime.now(timezone.utc),
                processing_status="processed",
                routing_status="pending_review",
            )
            db.add(communication)
            db.flush()
            db.add(
                RoutingDecision(
                    company_id=1,
                    communication_id=communication.id,
                    department_id=department.id,
                    category="incidencia_entrega",
                    confidence=0.61,
                    requires_review=True,
                    reason="La información es ambigua.",
                    status="pending_review",
                    source="agent",
                )
            )
            db.commit()

        with performance_test_client(self.fixture) as client:
            with patch("app.pages.routes._is_kibak_runtime", return_value=True):
                response = client.get(
                    "/history?state=review&kind=incidencia_entrega&department_id=1&source=agent&date_range=all"
                )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Incidencia de entrega", response.text)
        self.assertIn("incidencia_entrega", response.text)
        self.assertIn("Operaciones", response.text)


if __name__ == "__main__":
    unittest.main()
