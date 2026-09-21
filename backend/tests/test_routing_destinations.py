from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    Communication,
    Company,
    Department,
    Mailbox,
    RoutingAction,
    RoutingDecisionDestination,
)
from app.routing.service import RoutingValidationError, analyze_communication, serialize_routing_decision


class FakeRuntime:
    def __init__(self, response):  # noqa: ANN001
        self.response = response

    def complete(self, *, system_prompt, user_prompt, output_schema):  # noqa: ANN001
        del system_prompt, user_prompt, output_schema
        return self.response


class RoutingDestinationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add_all(
                [
                    Company(id=1, name="Tenant A"),
                    Company(id=2, name="Tenant B"),
                    Mailbox(id=1, company_id=1, name="Entrada", email_address="entrada@a.test"),
                ]
            )
            db.add_all(
                [
                    Department(id=10, company_id=1, name="Operaciones", active=True),
                    Department(id=11, company_id=1, name="Facturacion", active=True),
                    Department(id=12, company_id=1, name="Prevencion", active=True),
                    Department(id=13, company_id=1, name="Inactiva", active=False),
                    Department(id=20, company_id=2, name="Privada", active=True),
                ]
            )
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def _communication(self, db, subject: str) -> Communication:
        communication = Communication(
            company_id=1,
            mailbox_id=1,
            external_message_id=f"<{subject}@a.test>",
            provider="synthetic",
            sender_email="cliente@cliente.test",
            subject=subject,
            body_text="Contenido del caso",
            received_at=datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc),
        )
        db.add(communication)
        db.flush()
        return communication

    @staticmethod
    def _response(**changes):
        response = {
            "proposed_department_id": 10,
            "category": "incidencia",
            "confidence": 0.93,
            "requires_review": False,
            "reason": "El caso tiene un destino operativo claro.",
            "alternative_department_id": None,
            "ambiguity_reason": None,
            "runner_up_department_id": 11,
            "runner_up_confidence": 0.25,
            "additional_destinations": [],
        }
        response.update(changes)
        return response

    def test_simple_routing_keeps_primary_and_runner_up_separate_without_action(self):
        with self.session_factory() as db:
            communication = self._communication(db, "simple")
            decision = analyze_communication(
                db,
                1,
                communication.id,
                FakeRuntime(self._response()),
                routing_context={"departments": [{"department_id": 10}, {"department_id": 11}]},
            )
            destinations = list(
                db.scalars(
                    select(RoutingDecisionDestination).where(
                        RoutingDecisionDestination.routing_decision_id == decision.id
                    )
                )
            )
            self.assertEqual([(item.department_id, item.role) for item in destinations], [(10, "operational")])
            self.assertEqual(decision.runner_up_department_id, 11)
            self.assertEqual(db.scalar(select(func.count(RoutingAction.id))), 0)
            self.assertEqual(serialize_routing_decision(decision)["destinations"][0]["department_id"], 10)

    def test_cases_107_and_108_preserve_two_operational_destinations(self):
        with self.session_factory() as db:
            for case_number in (107, 108):
                communication = self._communication(db, f"caso {case_number}")
                decision = analyze_communication(
                    db,
                    1,
                    communication.id,
                    FakeRuntime(
                        self._response(
                            runner_up_department_id=None,
                            additional_destinations=[
                                {"department_id": 11, "role": "operational", "position": 2}
                            ],
                        )
                    ),
                    routing_context={"departments": [{"department_id": 10}, {"department_id": 11}]},
                )
                destinations = list(
                    db.scalars(
                        select(RoutingDecisionDestination)
                        .where(RoutingDecisionDestination.routing_decision_id == decision.id)
                        .order_by(RoutingDecisionDestination.position)
                    )
                )
                self.assertEqual(
                    [(item.department_id, item.role) for item in destinations],
                    [(10, "operational"), (11, "operational")],
                )
                self.assertIsNone(decision.runner_up_department_id)

    def test_case_125_persists_responsible_and_informed_without_runner_up(self):
        with self.session_factory() as db:
            communication = self._communication(db, "caso 125")
            decision = analyze_communication(
                db,
                1,
                communication.id,
                FakeRuntime(
                    self._response(
                        runner_up_department_id=None,
                        primary_role="responsible",
                        additional_destinations=[
                            {"department_id": 12, "role": "informed", "position": 2}
                        ],
                    )
                ),
                routing_context={"departments": [{"department_id": 10}, {"department_id": 12}]},
            )
            destinations = list(
                db.scalars(
                    select(RoutingDecisionDestination)
                    .where(RoutingDecisionDestination.routing_decision_id == decision.id)
                    .order_by(RoutingDecisionDestination.position)
                )
            )
            self.assertEqual(
                [(item.department_id, item.role) for item in destinations],
                [(10, "responsible"), (12, "informed")],
            )
            self.assertIsNone(decision.runner_up_department_id)

    def test_duplicate_additional_destinations_are_deduplicated(self):
        with self.session_factory() as db:
            communication = self._communication(db, "dedupe")
            decision = analyze_communication(
                db,
                1,
                communication.id,
                FakeRuntime(
                    self._response(
                        runner_up_department_id=None,
                        additional_destinations=[
                            {"department_id": 11, "role": "operational", "position": 2},
                            {"department_id": 11, "role": "operational", "position": 3},
                        ],
                    )
                ),
                routing_context={"departments": [{"department_id": 10}, {"department_id": 11}]},
            )
            self.assertEqual(
                db.scalar(
                    select(func.count(RoutingDecisionDestination.id)).where(
                        RoutingDecisionDestination.routing_decision_id == decision.id
                    )
                ),
                2,
            )

    def test_cross_tenant_and_inactive_destinations_are_rejected(self):
        for department_id, message in ((20, "tenant"), (13, "activo")):
            with self.subTest(department_id=department_id), self.session_factory() as db:
                communication = self._communication(db, message)
                with self.assertRaisesRegex(RoutingValidationError, "Additional destination"):
                    analyze_communication(
                        db,
                        1,
                        communication.id,
                        FakeRuntime(
                            self._response(
                                runner_up_department_id=None,
                                additional_destinations=[
                                    {"department_id": department_id, "role": "operational", "position": 2}
                                ],
                            )
                        ),
                        routing_context={"departments": [{"department_id": 10}, {"department_id": department_id}]},
                    )

    def test_workbench_template_separates_additional_destinations_from_alternative(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "templates"
            / "communications"
            / "detail.html"
        ).read_text(encoding="utf-8")
        self.assertIn("También intervienen", template)
        self.assertIn("row.additional_destinations", template)
        self.assertIn("analysis-alternative", template)


if __name__ == "__main__":
    unittest.main()
