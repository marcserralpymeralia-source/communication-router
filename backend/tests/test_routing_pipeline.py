from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Communication, Company, Department, Mailbox, Role, User, RoutingCorrection, RoutingDecision
from app.routing.service import (
    RoutingValidationError,
    analyze_communication,
    confirm_routing_decision,
    correct_routing_decision,
)


class FakeRuntime:
    def __init__(self, *responses):
        self.responses = list(responses)

    def complete(self, *, system_prompt, user_prompt, output_schema):  # noqa: ANN001
        del system_prompt, user_prompt, output_schema
        return self.responses.pop(0)


class RoutingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add_all([
                Company(id=1, name="Tenant A"),
                Company(id=2, name="Tenant B"),
                Role(id=1, company_id=1, name="Administrador"),
                Role(id=2, company_id=2, name="Administrador"),
                User(id=101, company_id=1, role_id=1, email="admin@a.test", name="Admin A", password_hash="x"),
                User(id=202, company_id=2, role_id=2, email="admin@b.test", name="Admin B", password_hash="x"),
                Mailbox(id=1, company_id=1, name="Entrada A", email_address="entrada@a.test"),
                Mailbox(id=2, company_id=2, name="Entrada B", email_address="entrada@b.test"),
                Department(id=10, company_id=1, name="Comercial", active=True),
                Department(id=11, company_id=1, name="Logistica", active=True),
                Department(id=20, company_id=2, name="Privado", active=True),
            ])
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def _communication(self, db, company_id=1, mailbox_id=1):
        item = Communication(
            company_id=company_id,
            mailbox_id=mailbox_id,
            external_message_id=f"<message-{company_id}@test>",
            provider="imap",
            sender_email="sender@test",
            to_recipients=json.dumps(["entrada@test"]),
            subject="Consulta",
            body_text="Necesito ayuda",
            received_at=datetime.now(timezone.utc),
        )
        db.add(item)
        db.flush()
        return item

    @staticmethod
    def _proposal(department_id, confidence, requires_review=False):
        return {
            "proposed_department_id": department_id,
            "category": "consulta",
            "confidence": confidence,
            "requires_review": requires_review,
            "reason": "La intención corresponde al departamento.",
            "alternative_department_id": None,
            "ambiguity_reason": None,
        }

    def test_pipeline_persists_routed_decision_and_preserves_reanalysis_history(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            runtime = FakeRuntime(self._proposal(10, 0.96), self._proposal(11, 0.55, True))

            first = analyze_communication(db, 1, communication.id, runtime, user_id=101)
            second = analyze_communication(db, 1, communication.id, runtime, user_id=101)
            db.commit()

            self.assertEqual(first.status, "superseded")
            self.assertEqual(second.status, "pending_review")
            self.assertEqual(second.analysis_number, 2)
            self.assertEqual(communication.routing_status, "pending_review")
            self.assertEqual(db.scalar(select(RoutingDecision).where(RoutingDecision.id == first.id)).status, "superseded")
            self.assertEqual(db.query(RoutingDecision).count(), 2)

    def test_human_confirmation_keeps_proposal_and_marks_communication_routed(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            decision = analyze_communication(db, 1, communication.id, FakeRuntime(self._proposal(10, 0.60, True)))

            confirmed = confirm_routing_decision(db, 1, communication.id, 101, decision_id=decision.id)
            db.commit()

            self.assertEqual(confirmed.status, "confirmed")
            self.assertEqual(confirmed.department_id, 10)
            self.assertEqual(confirmed.final_department_id, 10)
            self.assertEqual(communication.routing_status, "routed")

    def test_human_correction_creates_audit_row_and_rejects_foreign_department(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            decision = analyze_communication(db, 1, communication.id, FakeRuntime(self._proposal(10, 0.60, True)))

            with self.assertRaises(RoutingValidationError):
                correct_routing_decision(
                    db, 1, communication.id, 101, 20, reason="Intento cross-tenant", decision_id=decision.id
                )
            corrected = correct_routing_decision(
                db,
                1,
                communication.id,
                101,
                11,
                reason="La incidencia describe una entrega pendiente.",
                decision_id=decision.id,
            )
            db.commit()

            self.assertEqual(corrected.status, "corrected")
            self.assertEqual(corrected.department_id, 10)
            self.assertEqual(corrected.final_department_id, 11)
            self.assertEqual(db.query(RoutingCorrection).count(), 1)
            self.assertEqual(communication.routing_status, "routed")


if __name__ == "__main__":
    unittest.main()
