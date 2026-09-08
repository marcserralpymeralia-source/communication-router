from __future__ import annotations

import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch

from app.core.app_factory import create_app
from app.db.models import (
    Communication,
    CommunicationAttachment,
    Company,
    Department,
    RoutingAction,
    RoutingCorrection,
    RoutingDecision,
    User,
)
from app.core import lifespan as lifespan_module
from scripts.performance_data import build_performance_fixture, temporary_performance_environment, performance_test_client


class CommunicationsWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = build_performance_fixture("small")
        self.engine = create_engine(self.fixture.tenant_database_url)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.engine.dispose()
        self.fixture.cleanup()

    def _seed_workspace(self):
        with self.session_factory() as db:
            user = db.scalar(select(User).where(User.company_id == 1).order_by(User.id))
            department_a = Department(company_id=1, name="Logística", destination_email="logistica@example.com", active=True)
            department_b = Department(company_id=1, name="Comercial", destination_email="comercial@example.com", active=True)
            db.add_all([department_a, department_b])
            db.flush()
            from app.db.models import Mailbox

            mailbox = Mailbox(company_id=1, name="Entrada principal", email_address="entrada@example.com", enabled=True)
            db.add(mailbox)
            db.flush()
            db.commit()
            return user.id if user else None, department_a.id, department_b.id, mailbox.id

    def _communication(self, db, mailbox_id, subject, *, processing_status="processed", routing_status="unclassified"):
        communication = Communication(
            company_id=1,
            mailbox_id=mailbox_id,
            external_message_id=f"<{subject.replace(' ', '-')}@test>",
            provider="imap",
            sender_name="Marta Pérez",
            sender_email="marta@example.com",
            to_recipients='["entrada@example.com"]',
            subject=subject,
            body_text="La comunicación contiene el contexto operativo.",
            received_at=datetime(2026, 9, 7, 10, 42, tzinfo=timezone.utc),
            processing_status=processing_status,
            routing_status=routing_status,
        )
        db.add(communication)
        db.flush()
        return communication

    def test_workbench_renders_business_columns_and_search(self):
        _user_id, department_id, _alternative_id, mailbox_id = self._seed_workspace()
        with self.session_factory() as db:
            communication = self._communication(db, mailbox_id, "Entrega retrasada", routing_status="pending_review")
            db.add(
                RoutingDecision(
                    company_id=1,
                    communication_id=communication.id,
                    department_id=department_id,
                    category="incidencia_entrega",
                    confidence=0.94,
                    requires_review=True,
                    reason="La mercancía no ha llegado al destino previsto.",
                    status="pending_review",
                    source="agent",
                )
            )
            db.commit()

        with performance_test_client(self.fixture) as client:
            response = client.get("/communications/workbench?q=Logística")
            empty_response = client.get("/communications/workbench?q=NoExisteEnLaBandeja")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Buzón receptor", response.text)
        self.assertIn("Entrega retrasada", response.text)
        self.assertIn("Logística", response.text)
        self.assertIn("Pendiente de revisión", response.text)
        self.assertIn("94%", response.text)
        self.assertIn("Buscar por remitente, asunto o departamento", response.text)
        self.assertIn("No hay comunicaciones aquí", empty_response.text)

    def test_filters_cover_automatic_reviewed_unclassified_and_error(self):
        _user_id, department_id, _alternative_id, mailbox_id = self._seed_workspace()
        with self.session_factory() as db:
            automatic = self._communication(db, mailbox_id, "Ruta automática", routing_status="routed")
            db.add(
                RoutingDecision(
                    company_id=1,
                    communication_id=automatic.id,
                    department_id=department_id,
                    final_department_id=department_id,
                    category="consulta",
                    confidence=0.96,
                    requires_review=False,
                    reason="Propuesta clara.",
                    status="routed",
                    source="agent",
                )
            )
            reviewed = self._communication(db, mailbox_id, "Cambio revisado", routing_status="routed")
            db.add(
                RoutingDecision(
                    company_id=1,
                    communication_id=reviewed.id,
                    department_id=department_id,
                    final_department_id=department_id,
                    category="consulta",
                    confidence=0.72,
                    requires_review=True,
                    reason="Revisada por una persona.",
                    status="confirmed",
                    source="agent",
                )
            )
            self._communication(db, mailbox_id, "Sin clasificar")
            self._communication(db, mailbox_id, "Error de entrada", processing_status="error", routing_status="routing_error")
            db.commit()

        for status, expected in (
            ("automatic", "Ruta automática"),
            ("reviewed", "Cambio revisado"),
            ("unclassified", "Sin clasificar"),
            ("error", "Error de entrada"),
        ):
            with performance_test_client(self.fixture) as client:
                response = client.get(f"/communications/workbench?status={status}")
            self.assertEqual(response.status_code, 200)
            self.assertIn(expected, response.text)

    def test_detail_shows_original_message_analysis_correction_timeline_and_forwarding(self):
        user_id, department_id, alternative_id, mailbox_id = self._seed_workspace()
        with self.session_factory() as db:
            communication = self._communication(db, mailbox_id, "Incidencia de entrega", routing_status="routed")
            communication.attachments.append(
                CommunicationAttachment(
                    company_id=1,
                    filename="albaran.pdf",
                    mime_type="application/pdf",
                    size_bytes=2048,
                    storage_ref="local://albaran.pdf",
                    extracted_text="Texto extraído del albarán.",
                )
            )
            decision = RoutingDecision(
                company_id=1,
                communication_id=communication.id,
                department_id=department_id,
                alternative_department_id=alternative_id,
                final_department_id=alternative_id,
                category="incidencia_entrega",
                confidence=0.94,
                requires_review=False,
                reason="La mercancía que debía entregarse todavía no ha llegado.",
                status="corrected",
                source="agent",
                reviewed_by_user_id=user_id,
                reviewed_at=datetime.now(timezone.utc),
            )
            db.add(decision)
            db.flush()
            db.add(
                RoutingCorrection(
                    company_id=1,
                    communication_id=communication.id,
                    routing_decision_id=decision.id,
                    original_department_id=department_id,
                    corrected_department_id=alternative_id,
                    original_category="incidencia_entrega",
                    corrected_category="incidencia_entrega",
                    reason="El cliente solicita una respuesta comercial.",
                    corrected_by_user_id=user_id,
                )
            )
            db.add(
                RoutingAction(
                    company_id=1,
                    communication_id=communication.id,
                    routing_decision_id=decision.id,
                    department_id=alternative_id,
                    triggered_by_user_id=user_id,
                    destination_email="comercial@example.com",
                    status="sent",
                    source="auto",
                    idempotency_key="test-forwarding-1",
                    provider_message_id="provider-1",
                )
            )
            db.commit()
            communication_id = communication.id

        with performance_test_client(self.fixture) as client:
            response = client.get(f"/communications/workbench/{communication_id}")

        self.assertEqual(response.status_code, 200)
        for expected in (
            "Entrada original",
            "Qué entiende KIBAK",
            "Incidencia Entrega",
            "Logística",
            "Comercial",
            "94%",
            "albaran.pdf",
            "Departamento cambiado",
            "Derivada automáticamente",
            "Enviado",
            "Texto extraído del albarán.",
        ):
            self.assertIn(expected, response.text)
        self.assertIn(f'value="{alternative_id}"', response.text)

    def test_workbench_detail_is_tenant_safe(self):
        _user_id, _department_id, _alternative_id, mailbox_id = self._seed_workspace()
        with self.session_factory() as db:
            self._communication(db, mailbox_id, "Privado del tenant", routing_status="unclassified")
            db.add(Company(id=2, name="Otro tenant"))
            from app.db.models import Mailbox

            other_mailbox = Mailbox(company_id=2, name="Otro buzón", email_address="otro@example.com")
            db.add(other_mailbox)
            db.flush()
            other_communication = Communication(
                company_id=2,
                mailbox_id=other_mailbox.id,
                external_message_id="<other-tenant@test>",
                provider="imap",
                subject="No debe aparecer",
                routing_status="unclassified",
            )
            db.add(other_communication)
            db.commit()
            other_communication_id = other_communication.id

        with temporary_performance_environment(self.fixture):
            from fastapi.testclient import TestClient

            app = create_app()
            with patch.object(lifespan_module, "start_email_sync_worker", lambda: None), patch.object(lifespan_module, "start_job_worker", lambda: None):
                with TestClient(app, raise_server_exceptions=False) as client:
                    login = client.post("/login", data={"email": "admin@anchi.local", "password": "admin123"}, follow_redirects=False)
                    self.assertIn(login.status_code, (302, 303))
                    response = client.get(f"/communications/workbench/{other_communication_id}")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("No debe aparecer", response.text)


if __name__ == "__main__":
    unittest.main()
