from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.encryption import encrypt_secret  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import (  # noqa: E402
    BackgroundJob,
    Communication,
    Company,
    Department,
    LLMSettings,
    Mailbox,
    RoutingAction,
    RoutingDecision,
)
from app.routing.auto import enqueue_forwarding_for_decision  # noqa: E402
from app.routing.forwarding import ForwardingDeliveryError, _build_message, forward_communication  # noqa: E402
from app.routing.organization_config import OrganizationConfigError, validate_organization_config  # noqa: E402
from app.routing.policy import RoutingPolicy, RoutingPolicyError, build_kibak_readiness, parse_policy_form  # noqa: E402


class KibakSafetyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add_all([
                Company(id=1, name="Tenant A"),
                Company(id=2, name="Tenant B"),
                Mailbox(id=1, company_id=1, name="Entrada A", email_address="entrada@a.test"),
                Department(id=10, company_id=1, name="Logística", destination_email="logistica@a.test", active=True),
                Department(id=20, company_id=2, name="Compras", destination_email="compras@b.test", active=True),
                LLMSettings(company_id=1, auto_routing_enabled=True, auto_forwarding_enabled=True, simulation_mode=True, agent_enabled=True, api_key_encrypted=encrypt_secret("key")),
            ])
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def _decision(self, db, *, requires_review=False, company_id=1):
        communication = Communication(
            company_id=company_id,
            mailbox_id=1,
            external_message_id=f"msg-{company_id}-{datetime.now(timezone.utc).timestamp()}",
            processing_status="processed",
            routing_status="routed",
            subject="Consulta",
            body_text="Contenido",
        )
        db.add(communication)
        db.flush()
        decision = RoutingDecision(
            company_id=company_id,
            communication_id=communication.id,
            department_id=10,
            final_department_id=10,
            category="consulta",
            confidence=0.98,
            requires_review=requires_review,
            reason="Motivo",
            status="pending_review" if requires_review else "routed",
            source="auto",
        )
        db.add(decision)
        db.flush()
        return decision

    def test_simulation_is_idempotent_and_never_queues_smtp(self):
        with self.session_factory() as db:
            decision = self._decision(db)
            self.assertIsNone(enqueue_forwarding_for_decision(db, company_id=1, decision=decision, source="automatic"))
            action = db.scalar(select(RoutingAction).where(RoutingAction.company_id == 1))
            self.assertEqual(action.status, "simulated")
            self.assertEqual(action.action_type, "simulated_forward")
            self.assertEqual(action.source, "simulation")
            self.assertEqual(db.query(BackgroundJob).count(), 0)
            self.assertIsNone(enqueue_forwarding_for_decision(db, company_id=1, decision=decision, source="automatic"))
            self.assertEqual(db.query(RoutingAction).count(), 1)

    def test_direct_forwarding_service_is_also_blocked_by_simulation(self):
        with self.session_factory() as db:
            decision = self._decision(db)
            action = forward_communication(
                db,
                company_id=1,
                communication_id=decision.communication_id,
                routing_decision_id=decision.id,
                department_id=10,
                smtp_client_factory=lambda _mailbox: (_ for _ in ()).throw(AssertionError("SMTP no debe invocarse")),
            )
            self.assertEqual(action.status, "simulated")

    def test_manual_confirmation_is_recorded_as_simulation_without_smtp(self):
        with self.session_factory() as db:
            decision = self._decision(db)
            decision.status = "confirmed"
            decision.final_department_id = 10
            self.assertIsNone(enqueue_forwarding_for_decision(
                db,
                company_id=1,
                decision=decision,
                source="human",
                human_confirmed=True,
            ))
            action = db.scalar(select(RoutingAction).where(RoutingAction.company_id == 1))
            self.assertEqual(action.status, "simulated")
            self.assertEqual(action.source, "simulation")

    def test_review_requirement_blocks_simulated_action(self):
        with self.session_factory() as db:
            decision = self._decision(db, requires_review=True)
            self.assertIsNone(enqueue_forwarding_for_decision(db, company_id=1, decision=decision, source="automatic"))
            self.assertEqual(db.query(RoutingAction).count(), 0)

    def test_threshold_validation_is_centralized(self):
        with self.assertRaises(RoutingPolicyError):
            parse_policy_form({"routing_review_threshold": "0.95", "routing_auto_threshold": "0.80"}, RoutingPolicy())

    def test_readiness_is_tenant_scoped(self):
        with self.session_factory() as db:
            readiness = build_kibak_readiness(db, 1)
            self.assertEqual(readiness["company_id"], 1)
            self.assertNotIn("Compras", " ".join(item["message"] for item in readiness["checks"]))

    def test_smtp_subject_crlf_is_rejected(self):
        communication = Communication(company_id=1, mailbox_id=1, subject="Hola\nBcc: attacker@example.com", body_text="Contenido")
        department = Department(company_id=1, name="Logística", destination_email="logistica@a.test")
        mailbox = Mailbox(company_id=1, name="Entrada", email_address="entrada@a.test", from_email="entrada@a.test")
        action = RoutingAction(company_id=1, communication_id=1, routing_decision_id=1, department_id=10, destination_email="logistica@a.test", idempotency_key="test")
        with self.assertRaises(ForwardingDeliveryError) as error:
            _build_message(communication, department, mailbox, action)
        self.assertEqual(error.exception.error_code, "invalid_header")

    def test_organization_import_rejects_unknown_knowledge_type(self):
        with self.assertRaises(OrganizationConfigError):
            validate_organization_config({
                "version": "kibak.organization.v1",
                "departments": [{"name": "Logística"}],
                "knowledge": [{"department_name": "Logística", "title": "T", "content": "C", "knowledge_type": "orders"}],
                "raci": [],
            })


if __name__ == "__main__":
    unittest.main()
