from __future__ import annotations

import json
import smtplib
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.encryption import encrypt_secret
from app.db.database import Base
from app.db.models import (
    BackgroundJob,
    Communication,
    Company,
    Department,
    LLMSettings,
    Mailbox,
    Role,
    RoutingAction,
    RoutingDecision,
    User,
)
from app.routing.auto import (
    AutomaticRoutingError,
    enqueue_automatic_routing,
    enqueue_forwarding_for_decision,
    process_automatic_routing,
)
from app.routing.forwarding import AUTO_FORWARD_JOB_TYPE, process_forwarding_job
from app.routing.service import analyze_communication, confirm_routing_decision, correct_routing_decision
from app.workers.jobs_worker import _process_job


class FakeSmtp:
    def __init__(self) -> None:
        self.message = None
        self.send_count = 0
        self.login_args = None

    def login(self, username: str, password: str):
        self.login_args = (username, password)
        return "OK", [b"authenticated"]

    def send_message(self, message, *, from_addr: str, to_addrs: list[str]):  # noqa: ANN001
        self.message = message
        self.from_addr = from_addr
        self.to_addrs = to_addrs
        self.send_count += 1
        return {"provider_message_id": "smtp-provider-1"}

    def quit(self):
        return "BYE", [b"closed"]


class AutoForwardingIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add_all([
                Company(id=1, name="Tenant A"),
                Company(id=2, name="Tenant B"),
                Role(id=1, company_id=1, name="Administrador"),
                User(id=101, company_id=1, role_id=1, email="admin@a.test", name="Admin A", password_hash="x"),
                Mailbox(
                    id=1,
                    company_id=1,
                    name="Entrada A",
                    email_address="entrada@a.test",
                    smtp_enabled=True,
                    smtp_host="smtp.a.test",
                    smtp_username="relay@a.test",
                    smtp_password_encrypted=encrypt_secret("smtp-secret"),
                    from_email="relay@a.test",
                ),
                Department(id=10, company_id=1, name="Comercial", destination_email="commercial@a.test"),
                Department(id=11, company_id=1, name="Logistica", destination_email="logistics@a.test"),
            ])
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def _communication(self, db, *, body: str = "Necesito ayuda") -> Communication:  # noqa: ANN001
        communication = Communication(
            company_id=1,
            mailbox_id=1,
            external_message_id=f"<message-{datetime.now(timezone.utc).timestamp()}@a.test>",
            provider="imap",
            sender_email="customer@external.test",
            to_recipients=json.dumps(["entrada@a.test"]),
            subject="Consulta",
            body_text=body,
            processing_status="processed",
            received_at=datetime.now(timezone.utc),
        )
        db.add(communication)
        db.flush()
        return communication

    def _settings(self, db, *, auto_routing: bool, auto_forwarding: bool) -> LLMSettings:  # noqa: ANN001
        settings = LLMSettings(
            company_id=1,
            auto_routing_enabled=auto_routing,
            auto_forwarding_enabled=auto_forwarding,
            api_key_encrypted=encrypt_secret("provider-key"),
            agent_enabled=True,
            can_classify_email=True,
        )
        db.add(settings)
        db.commit()
        return settings

    @staticmethod
    def _proposal(department_id: int | None = 10, *, confidence: float = 0.96, requires_review: bool = False, ambiguity: str | None = None) -> dict:
        return {
            "proposed_department_id": department_id,
            "category": "consulta",
            "confidence": confidence,
            "requires_review": requires_review,
            "reason": "La intención corresponde al departamento.",
            "alternative_department_id": None,
            "ambiguity_reason": ambiguity,
        }

    def _run_auto_routing(self, db, communication: Communication, proposal: dict):  # noqa: ANN001
        job = enqueue_automatic_routing(db, company_id=1, communication_id=communication.id)
        self.assertIsNotNone(job)
        with patch("app.settings.integrations.call_openai", return_value={"ok": True, "content": json.dumps(proposal)}):
            result = _process_job(db, job)
        db.commit()
        return result, db.get(RoutingDecision, result["decision_id"])

    def test_auto_routing_off_does_not_enqueue(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=False, auto_forwarding=True)

            self.assertIsNone(enqueue_automatic_routing(db, company_id=1, communication_id=communication.id))
            self.assertEqual(db.query(BackgroundJob).count(), 0)

    def test_auto_routing_without_auto_forwarding_only_persists_decision(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=True, auto_forwarding=False)

            result, decision = self._run_auto_routing(db, communication, self._proposal())

            self.assertTrue(result["ok"])
            self.assertEqual(decision.status, "routed")
            self.assertEqual(decision.final_department_id, 10)
            self.assertEqual(db.query(RoutingAction).count(), 0)
            self.assertEqual(db.query(BackgroundJob).filter(BackgroundJob.job_type == AUTO_FORWARD_JOB_TYPE).count(), 0)

    def test_both_enabled_routes_then_forwards_from_a_minimal_payload(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=True, auto_forwarding=True)

            result, decision = self._run_auto_routing(db, communication, self._proposal())
            forward_job = db.scalar(select(BackgroundJob).where(BackgroundJob.job_type == AUTO_FORWARD_JOB_TYPE))
            self.assertIsNotNone(forward_job)
            self.assertEqual(set(json.loads(forward_job.payload_json)), {"company_id", "routing_action_id"})
            self.assertEqual(decision.final_department_id, 10)

            smtp = FakeSmtp()
            with patch("app.settings.integrations._smtp_client", return_value=smtp):
                delivery = process_forwarding_job(db, forward_job)
            db.commit()
            action = db.scalar(select(RoutingAction).where(RoutingAction.company_id == 1))

            self.assertTrue(delivery["ok"])
            self.assertEqual(action.status, "sent")
            self.assertEqual(action.source, "automatic")
            self.assertEqual(smtp.to_addrs, ["commercial@a.test"])

    def test_review_required_or_ambiguous_decision_never_forwards(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=True, auto_forwarding=True)
            _result, decision = self._run_auto_routing(db, communication, self._proposal(requires_review=True))

            self.assertEqual(decision.status, "pending_review")
            self.assertEqual(db.query(RoutingAction).count(), 0)

    def test_ambiguous_decision_never_forwards(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=True, auto_forwarding=True)
            _result, decision = self._run_auto_routing(
                db,
                communication,
                self._proposal(ambiguity="No queda claro el departamento final."),
            )

            self.assertEqual(decision.status, "pending_review")
            self.assertEqual(db.query(RoutingAction).count(), 0)

    def test_missing_destination_forces_review_without_creating_action(self):
        with self.session_factory() as db:
            db.get(Department, 10).destination_email = None
            db.commit()
            communication = self._communication(db)
            self._settings(db, auto_routing=True, auto_forwarding=True)

            _result, decision = self._run_auto_routing(db, communication, self._proposal())

            self.assertEqual(decision.status, "pending_review")
            self.assertTrue(decision.requires_review)
            self.assertEqual(communication.routing_status, "pending_review")
            self.assertEqual(db.query(RoutingAction).count(), 0)

    def test_routing_provider_failure_keeps_ingestion_and_creates_no_forward(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=True, auto_forwarding=True)
            job = enqueue_automatic_routing(db, company_id=1, communication_id=communication.id)

            with patch("app.settings.integrations.call_openai", side_effect=TimeoutError("provider timeout")):
                with self.assertRaises(Exception) as raised:
                    _process_job(db, job)
            db.commit()

            self.assertTrue(getattr(raised.exception, "retryable", False))
            self.assertEqual(communication.processing_status, "processed")
            self.assertEqual(communication.routing_status, "routing_error")
            self.assertEqual(db.query(RoutingAction).count(), 0)

    def test_transient_smtp_failure_retries_same_action_and_not_a_new_one(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=True, auto_forwarding=True)
            _result, _decision = self._run_auto_routing(db, communication, self._proposal())
            forward_job = db.scalar(select(BackgroundJob).where(BackgroundJob.job_type == AUTO_FORWARD_JOB_TYPE))

            class FlakySmtp(FakeSmtp):
                def send_message(self, message, *, from_addr: str, to_addrs: list[str]):  # noqa: ANN001
                    self.send_count += 1
                    if self.send_count == 1:
                        raise smtplib.SMTPResponseException(421, "temporary provider failure")
                    self.message = message
                    self.from_addr = from_addr
                    self.to_addrs = to_addrs
                    return {"provider_message_id": "smtp-provider-retry"}

            smtp = FlakySmtp()
            with patch("app.settings.integrations._smtp_client", return_value=smtp):
                first = process_forwarding_job(db, forward_job)
                db.commit()
                second = process_forwarding_job(db, forward_job)
            db.commit()

            action = db.scalar(select(RoutingAction).where(RoutingAction.company_id == 1))
            self.assertFalse(first["ok"])
            self.assertTrue(first["retryable"])
            self.assertTrue(second["ok"])
            self.assertEqual(action.status, "sent")
            self.assertEqual(action.attempt_count, 2)
            self.assertEqual(db.query(RoutingAction).count(), 1)

    def test_duplicate_forward_worker_execution_sends_once(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=True, auto_forwarding=True)
            _result, _decision = self._run_auto_routing(db, communication, self._proposal())
            forward_job = db.scalar(select(BackgroundJob).where(BackgroundJob.job_type == AUTO_FORWARD_JOB_TYPE))
            smtp = FakeSmtp()
            with patch("app.settings.integrations._smtp_client", return_value=smtp):
                first = process_forwarding_job(db, forward_job)
                db.commit()
                duplicate = process_forwarding_job(db, forward_job)

            self.assertTrue(first["ok"])
            self.assertTrue(duplicate["skipped"])
            self.assertEqual(smtp.send_count, 1)
            self.assertEqual(db.query(RoutingAction).count(), 1)

    def test_human_confirmation_can_enqueue_when_auto_forwarding_is_enabled(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=False, auto_forwarding=True)
            decision = analyze_communication(
                db,
                1,
                communication.id,
                lambda **_kwargs: self._proposal(confidence=0.55, requires_review=True),
            )
            confirmed = confirm_routing_decision(db, 1, communication.id, 101, decision_id=decision.id)
            forward_job = enqueue_forwarding_for_decision(
                db,
                company_id=1,
                decision=confirmed,
                triggered_by_user_id=101,
                source="human",
                human_confirmed=True,
            )
            self.assertIsNotNone(forward_job)
            action = db.scalar(select(RoutingAction).where(RoutingAction.company_id == 1))
            self.assertEqual(action.source, "human")

    def test_human_correction_forwards_only_final_department(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=False, auto_forwarding=True)
            decision = analyze_communication(
                db,
                1,
                communication.id,
                lambda **_kwargs: self._proposal(confidence=0.55, requires_review=True),
            )
            corrected = correct_routing_decision(
                db,
                1,
                communication.id,
                101,
                11,
                reason="La incidencia es logística.",
                decision_id=decision.id,
            )
            forward_job = enqueue_forwarding_for_decision(
                db,
                company_id=1,
                decision=corrected,
                triggered_by_user_id=101,
                source="human",
                human_confirmed=True,
            )
            action = db.scalar(select(RoutingAction).where(RoutingAction.company_id == 1))
            self.assertIsNotNone(forward_job)
            self.assertEqual(action.department_id, 11)
            self.assertEqual(action.destination_email, "logistics@a.test")

    def test_reanalysis_after_sent_does_not_enqueue_automatic_second_forward(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=True, auto_forwarding=True)
            _result, first_decision = self._run_auto_routing(db, communication, self._proposal())
            first_job = db.scalar(select(BackgroundJob).where(BackgroundJob.job_type == AUTO_FORWARD_JOB_TYPE))
            with patch("app.settings.integrations._smtp_client", return_value=FakeSmtp()):
                process_forwarding_job(db, first_job)
            db.commit()

            second_decision = analyze_communication(
                db,
                1,
                communication.id,
                lambda **_kwargs: self._proposal(department_id=11),
                source="auto",
            )
            second_job = enqueue_forwarding_for_decision(
                db,
                company_id=1,
                decision=second_decision,
                source="automatic",
            )

            self.assertIsNone(second_job)
            self.assertEqual(db.query(RoutingAction).count(), 1)
            self.assertEqual(first_decision.status, "superseded")

    def test_tenant_mismatch_is_rejected_before_forwarding(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._settings(db, auto_routing=True, auto_forwarding=True)
            _result, decision = self._run_auto_routing(db, communication, self._proposal())

            with self.assertRaises(AutomaticRoutingError):
                enqueue_forwarding_for_decision(db, company_id=2, decision=decision, source="automatic")
            self.assertEqual(db.query(RoutingAction).count(), 1)


if __name__ == "__main__":
    unittest.main()
