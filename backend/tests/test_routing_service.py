from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Communication, CommunicationAttachment, Company, Department, Mailbox
from app.routing.service import (
    RoutingValidationError,
    classify_communication,
)


class FakeRoutingRuntime:
    def __init__(self, response):  # noqa: ANN001
        self.response = response
        self.system_prompt = None
        self.user_prompt = None
        self.output_schema = None

    def complete(self, *, system_prompt, user_prompt, output_schema):  # noqa: ANN001
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.output_schema = output_schema
        return self.response


class RoutingServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add_all([
                Company(id=1, name="Tenant A"),
                Company(id=2, name="Tenant B"),
                Mailbox(id=1, company_id=1, name="Entrada", email_address="entrada@a.test"),
            ])
            db.add_all([
                Department(id=10, company_id=1, name="Comercial", active=True),
                Department(id=11, company_id=1, name="Logistica", active=True),
                Department(id=12, company_id=1, name="Administracion", active=True),
                Department(id=13, company_id=1, name="Inactivo", active=False),
                Department(id=20, company_id=2, name="Privado", active=True),
            ])
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def _communication(self, db, *, subject, body, attachment_text=None):  # noqa: ANN001
        communication = Communication(
            company_id=1,
            mailbox_id=1,
            external_message_id=f"<{subject}@a.test>",
            provider="imap",
            sender_email="cliente@cliente.test",
            sender_name="Cliente",
            to_recipients=json.dumps(["entrada@a.test"]),
            cc_recipients=json.dumps(["copia@a.test"]),
            subject=subject,
            body_text=body,
            received_at=datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc),
        )
        db.add(communication)
        db.flush()
        if attachment_text is not None:
            communication.attachments.append(
                CommunicationAttachment(
                    company_id=1,
                    communication_id=communication.id,
                    filename="detalle.txt",
                    extracted_text=attachment_text,
                )
            )
            db.flush()
        return communication

    @staticmethod
    def _context(*departments):  # noqa: ANN001
        return {"departments": list(departments)}

    @staticmethod
    def _department(department_id, name, **extra):  # noqa: ANN001
        return {"department_id": department_id, "name": name, **extra}

    def test_evident_quote_goes_to_commercial_with_high_confidence(self):
        with self.session_factory() as db:
            communication = self._communication(db, subject="Presupuesto", body="Necesito presupuesto para 20 cajas")
            runtime = FakeRoutingRuntime({
                "proposed_department_id": 10,
                "category": "presupuesto",
                "confidence": 0.96,
                "requires_review": False,
                "reason": "La solicitud pide un precio para una cantidad concreta.",
                "alternative_department_id": None,
                "ambiguity_reason": None,
            })

            result = classify_communication(
                db,
                1,
                communication,
                runtime,
                routing_context=self._context(self._department(10, "Comercial"), self._department(11, "Logistica")),
            )

            self.assertEqual(result.proposed_department_id, 10)
            self.assertGreaterEqual(result.confidence, 0.9)
            self.assertFalse(result.requires_review)

    def test_default_context_builder_limits_prompt_to_active_departments(self):
        with self.session_factory() as db:
            communication = self._communication(db, subject="Presupuesto", body="Necesito precio")
            runtime = FakeRoutingRuntime({
                "proposed_department_id": 10,
                "category": "presupuesto",
                "confidence": 0.9,
                "requires_review": False,
                "reason": "La solicitud es comercial.",
                "alternative_department_id": None,
                "ambiguity_reason": None,
            })

            result = classify_communication(db, 1, communication, runtime)
            organization = json.loads(runtime.user_prompt)["organization"]

            self.assertEqual(result.proposed_department_id, 10)
            self.assertEqual({item["department_id"] for item in organization["departments"]}, {10, 11, 12})

    def test_semantic_delivery_incident_can_go_to_logistics(self):
        with self.session_factory() as db:
            communication = self._communication(db, subject="Entrega pendiente", body="Seguimos sin recibir lo que tenia que llegar ayer.")
            runtime = FakeRoutingRuntime({
                "proposed_department_id": 11,
                "category": "incidencia_entrega",
                "confidence": 0.88,
                "requires_review": False,
                "reason": "Describe una entrega pendiente, aunque no usa el nombre del departamento.",
                "alternative_department_id": None,
                "ambiguity_reason": None,
            })
            context = self._context(self._department(
                11,
                "Logistica",
                knowledge={"responsibilities": [{"title": "Incidencias de entrega", "content": "Mercancia que no llega a tiempo"}]},
            ))

            result = classify_communication(db, 1, communication, runtime, routing_context=context)

            self.assertEqual(result.proposed_department_id, 11)
            prompt = json.loads(runtime.user_prompt)
            self.assertEqual(prompt["communication"]["body_text"], "Seguimos sin recibir lo que tenia que llegar ayer.")
            self.assertIn("Incidencias de entrega", json.dumps(prompt["organization"], ensure_ascii=False))
            self.assertIn("No clasifiques por palabras clave aisladas", runtime.system_prompt)

    def test_exclusion_context_is_sent_and_does_not_force_administration(self):
        with self.session_factory() as db:
            communication = self._communication(db, subject="Factura de mercancia", body="Necesito precio y disponibilidad de estas cajas; adjunto la factura de referencia.")
            runtime = FakeRoutingRuntime({
                "proposed_department_id": 10,
                "category": "consulta_comercial",
                "confidence": 0.82,
                "requires_review": False,
                "reason": "La intencion principal es comercial y la factura es solo contexto.",
                "alternative_department_id": None,
                "ambiguity_reason": None,
            })
            context = self._context(
                self._department(10, "Comercial"),
                self._department(12, "Administracion", knowledge={"exclusions": [{"content": "No gestionar consultas de precio"}]}),
            )

            result = classify_communication(db, 1, communication, runtime, routing_context=context)

            self.assertEqual(result.proposed_department_id, 10)
            self.assertIn("No gestionar consultas de precio", runtime.user_prompt)

    def test_ambiguous_invoice_and_missing_goods_keeps_alternative_and_review(self):
        with self.session_factory() as db:
            communication = self._communication(db, subject="Factura pendiente", body="Necesito revisar una factura porque corresponde a mercancia que no hemos recibido.")
            runtime = FakeRoutingRuntime({
                "proposed_department_id": 12,
                "category": "factura_e_incidencia_entrega",
                "confidence": 0.51,
                "requires_review": True,
                "reason": "La factura apunta a Administracion, pero el hecho operativo apunta a Logistica.",
                "alternative_department_id": 11,
                "ambiguity_reason": "No queda claro si se solicita revisar la factura o reclamar la entrega.",
            })

            result = classify_communication(
                db,
                1,
                communication,
                runtime,
                routing_context=self._context(self._department(11, "Logistica"), self._department(12, "Administracion")),
            )

            self.assertEqual(result.alternative_department_id, 11)
            self.assertTrue(result.requires_review)
            self.assertLess(result.confidence, 0.7)

    def test_no_valid_department_does_not_force_a_choice(self):
        with self.session_factory() as db:
            communication = self._communication(db, subject="Saludo", body="Buenos dias, gracias por todo.")
            runtime = FakeRoutingRuntime({
                "proposed_department_id": None,
                "category": "sin_clasificar",
                "confidence": 0.08,
                "requires_review": True,
                "reason": "No hay una solicitud operativa identificable.",
                "alternative_department_id": None,
                "ambiguity_reason": "No hay evidencia suficiente.",
            })

            result = classify_communication(db, 1, communication, runtime, routing_context={"departments": []})

            self.assertIsNone(result.proposed_department_id)
            self.assertTrue(result.requires_review)

    def test_inactive_department_can_never_be_proposed(self):
        with self.session_factory() as db:
            communication = self._communication(db, subject="Antiguo", body="Solicitud")
            runtime = FakeRoutingRuntime({
                "proposed_department_id": 13,
                "category": "general",
                "confidence": 0.9,
                "requires_review": False,
                "reason": "Respuesta insegura.",
                "alternative_department_id": None,
                "ambiguity_reason": None,
            })

            with self.assertRaisesRegex(RoutingValidationError, "departamento activo"):
                classify_communication(
                    db,
                    1,
                    communication,
                    runtime,
                    routing_context=self._context(self._department(13, "Inactivo", active=False)),
                )

    def test_foreign_tenant_department_id_is_rejected(self):
        with self.session_factory() as db:
            communication = self._communication(db, subject="Tenant", body="Solicitud")
            runtime = FakeRoutingRuntime({
                "proposed_department_id": 20,
                "category": "general",
                "confidence": 0.9,
                "requires_review": False,
                "reason": "Respuesta insegura.",
                "alternative_department_id": None,
                "ambiguity_reason": None,
            })

            with self.assertRaisesRegex(RoutingValidationError, "departamento activo"):
                classify_communication(
                    db,
                    1,
                    communication,
                    runtime,
                    routing_context=self._context(self._department(10, "Comercial")),
                )

    def test_schema_rejects_extra_fields_and_invalid_confidence(self):
        with self.session_factory() as db:
            communication = self._communication(db, subject="Invalido", body="Solicitud")
            runtime = FakeRoutingRuntime({
                "proposed_department_id": 10,
                "category": "general",
                "confidence": 1.2,
                "requires_review": False,
                "reason": "Respuesta insegura.",
                "alternative_department_id": None,
                "ambiguity_reason": None,
                "unexpected": "no permitido",
            })

            with self.assertRaises(RoutingValidationError):
                classify_communication(
                    db,
                    1,
                    communication,
                    runtime,
                    routing_context=self._context(self._department(10, "Comercial")),
                )

    def test_communication_payload_includes_mailbox_recipients_and_attachment_text(self):
        with self.session_factory() as db:
            communication = self._communication(
                db,
                subject="Documento",
                body="Revisar documento",
                attachment_text="Detalle extraible del adjunto",
            )
            runtime = FakeRoutingRuntime({
                "proposed_department_id": None,
                "category": "sin_clasificar",
                "confidence": 0.1,
                "requires_review": False,
                "reason": "No hay evidencia suficiente.",
                "alternative_department_id": None,
                "ambiguity_reason": None,
            })

            classify_communication(db, 1, communication, runtime, routing_context={"departments": []})
            payload = json.loads(runtime.user_prompt)["communication"]

            self.assertEqual(payload["mailbox"], "entrada@a.test")
            self.assertEqual(payload["recipients"]["to"], ["entrada@a.test"])
            self.assertEqual(payload["recipients"]["cc"], ["copia@a.test"])
            self.assertEqual(payload["attachments"][0]["filename"], "detalle.txt")
            self.assertEqual(payload["attachments"][0]["extracted_text"], "Detalle extraible del adjunto")


if __name__ == "__main__":
    unittest.main()
