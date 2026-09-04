from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import sys

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
    RoutingDecision,
)
from app.jobs.service import enqueue_job  # noqa: E402
from app.routing.auto import enqueue_automatic_routing, process_automatic_routing  # noqa: E402
from app.routing.service import RoutingValidationError  # noqa: E402
from app.settings.integrations import _fetch_imap_emails  # noqa: E402
from app.workers.jobs_worker import _process_job  # noqa: E402


class FakeImapClient:
    def __init__(self, raw_message: bytes) -> None:
        self.raw_message = raw_message

    def login(self, _username, _password):  # noqa: ANN001
        return "OK", [b"logged in"]

    def select(self, *_args, **_kwargs):
        return "OK", [b"1"]

    def status(self, mailbox: str, *_args, **_kwargs):
        return "OK", [f"{mailbox} (UIDVALIDITY 777)".encode()]

    def uid(self, command, *args, **_kwargs):  # noqa: ANN001
        if command == "search":
            return "OK", [b"140"]
        if command == "fetch":
            return "OK", [(b"140 (UID 140 RFC822 {999})", self.raw_message)]
        return "OK", [b""]

    def logout(self):
        return "BYE", [b"logout"]


class AutomaticRoutingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add_all(
                [
                    Company(id=1, name="Tenant A"),
                    Company(id=2, name="Tenant B"),
                    Mailbox(id=1, company_id=1, name="Entrada A", email_address="entrada@a.test"),
                    Department(id=10, company_id=1, name="Comercial", active=True),
                ]
            )
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def _communication(self, db, *, company_id=1, mailbox_id=1, processing_status="processed"):
        communication = Communication(
            company_id=company_id,
            mailbox_id=mailbox_id,
            external_message_id=f"<message-{company_id}-{datetime.now(timezone.utc).timestamp()}>@test",
            provider="imap",
            sender_email="sender@test",
            to_recipients=json.dumps(["entrada@test"]),
            subject="Consulta",
            body_text="Necesito ayuda",
            processing_status=processing_status,
            received_at=datetime.now(timezone.utc),
        )
        db.add(communication)
        db.flush()
        return communication

    def _enable_auto_routing(self, db):
        settings = LLMSettings(
            company_id=1,
            auto_routing_enabled=True,
            api_key_encrypted=encrypt_secret("test-provider-key"),
            agent_enabled=True,
            can_classify_email=True,
        )
        db.add(settings)
        db.commit()
        return settings

    @staticmethod
    def _proposal():
        return json.dumps(
            {
                "proposed_department_id": 10,
                "category": "consulta",
                "confidence": 0.96,
                "requires_review": False,
                "reason": "La intención corresponde al departamento.",
                "alternative_department_id": None,
                "ambiguity_reason": None,
            }
        )

    def test_configuration_absent_does_not_enqueue_or_change_ingestion_state(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            db.commit()

            self.assertIsNone(enqueue_automatic_routing(db, company_id=1, communication_id=communication.id))
            self.assertEqual(communication.routing_status, "unclassified")
            self.assertEqual(db.query(BackgroundJob).count(), 0)

    def test_imap_ingestion_enqueues_without_calling_provider_inline(self):
        raw_message = (
            b"From: sender@example.com\r\n"
            b"To: entrada@example.com\r\n"
            b"Subject: Consulta\r\n"
            b"Message-ID: <auto-routing@example.com>\r\n"
            b"\r\n"
            b"Necesito ayuda\r\n"
        )
        with self.session_factory() as db:
            mailbox = db.get(Mailbox, 1)
            mailbox.connected_email = mailbox.email_address
            mailbox.imap_host = "imap.example.com"
            mailbox.imap_username = mailbox.email_address
            mailbox.imap_password_encrypted = encrypt_secret("imap-password")
            mailbox.read_unread_only = False
            self._enable_auto_routing(db)

            with patch("app.settings.integrations._imap_client", return_value=FakeImapClient(raw_message)) as imap_client:
                with patch("app.settings.integrations.call_openai") as provider:
                    result = _fetch_imap_emails(db, mailbox, 1, unread_only=False, mailbox_id=mailbox.id)

            job = db.scalar(select(BackgroundJob).where(BackgroundJob.job_type == "route_communication"))
            communication = db.scalar(select(Communication))
            self.assertEqual(result["saved"], 1)
            self.assertEqual(result["routing_jobs_enqueued"], 1)
            self.assertEqual(communication.routing_status, "routing_queued")
            self.assertEqual(job.dedupe_key, f"communication:1:{communication.id}")
            imap_client.assert_called_once()
            provider.assert_not_called()

    def test_enqueue_is_tenant_scoped_idempotent_and_contains_only_ids(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._enable_auto_routing(db)

            first = enqueue_automatic_routing(db, company_id=1, communication_id=communication.id)
            second = enqueue_automatic_routing(db, company_id=1, communication_id=communication.id)

            self.assertIsNotNone(first)
            self.assertEqual(first.id, second.id)
            self.assertEqual(communication.routing_status, "routing_queued")
            self.assertEqual(db.query(BackgroundJob).count(), 1)
            payload = json.loads(first.payload_json)
            self.assertEqual(set(payload), {"company_id", "communication_id"})
            self.assertEqual(payload["company_id"], 1)
            self.assertEqual(payload["communication_id"], communication.id)
            self.assertEqual(first.dedupe_key, f"communication:1:{communication.id}")

    def test_worker_executes_real_adapter_and_duplicate_execution_does_not_create_decision(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._enable_auto_routing(db)
            job = enqueue_automatic_routing(db, company_id=1, communication_id=communication.id)

            with patch("app.settings.integrations.call_openai", return_value={"ok": True, "content": self._proposal()}):
                result = _process_job(db, job)
                db.commit()
                duplicate = process_automatic_routing(db, job)
                db.commit()

            decision = db.scalar(select(RoutingDecision).where(RoutingDecision.communication_id == communication.id))
            self.assertTrue(result["ok"])
            self.assertEqual(result["routing_status"], "routed")
            self.assertTrue(duplicate["skipped"])
            self.assertEqual(decision.source, "auto")
            self.assertEqual(db.query(RoutingDecision).count(), 1)
            self.assertEqual(db.query(BackgroundJob).count(), 1)

    def test_timeout_is_retryable_and_leaves_reviewable_error_state(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._enable_auto_routing(db)
            job = enqueue_automatic_routing(db, company_id=1, communication_id=communication.id)

            with patch("app.settings.integrations.call_openai", side_effect=TimeoutError("provider timeout")):
                with self.assertRaises(RoutingValidationError) as raised:
                    _process_job(db, job)
            db.commit()

            self.assertTrue(raised.exception.retryable)
            self.assertEqual(raised.exception.error_type, "timeout")
            self.assertEqual(communication.routing_status, "routing_error")
            self.assertEqual(db.query(RoutingDecision).count(), 0)

    def test_malformed_output_is_permanent_and_audited(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            self._enable_auto_routing(db)
            job = enqueue_automatic_routing(db, company_id=1, communication_id=communication.id)

            with patch("app.settings.integrations.call_openai", return_value={"ok": True, "content": "no-json"}):
                with self.assertRaises(RoutingValidationError) as raised:
                    _process_job(db, job)
            db.commit()

            self.assertFalse(raised.exception.retryable)
            self.assertEqual(raised.exception.error_type, "invalid_json")
            self.assertEqual(communication.routing_status, "routing_error")

    def test_payload_tenant_mismatch_is_rejected_before_loading_communication(self):
        with self.session_factory() as db:
            communication = self._communication(db)
            job = enqueue_job(
                db,
                company_id=1,
                job_type="route_communication",
                payload={"company_id": 2, "communication_id": communication.id},
                dedupe_key="mismatch",
            )

            with self.assertRaises(RoutingValidationError) as raised:
                process_automatic_routing(db, job)

            self.assertEqual(raised.exception.error_type, "tenant_mismatch")


if __name__ == "__main__":
    unittest.main()
