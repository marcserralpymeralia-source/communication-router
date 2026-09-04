from __future__ import annotations

import smtplib
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.encryption import encrypt_secret
from app.db.database import Base
from app.db.models import (
    Communication,
    CommunicationAttachment,
    Company,
    Department,
    Mailbox,
    RoutingAction,
    RoutingDecision,
)
from app.routing.forwarding import ForwardingValidationError, forward_communication


class FakeSmtp:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {"provider_message_id": "provider-123"}
        self.login_args: tuple[str, str] | None = None
        self.message = None
        self.send_count = 0
        self.quit_count = 0

    def login(self, username: str, password: str):
        self.login_args = (username, password)
        return "OK", [b"authenticated"]

    def send_message(self, message, *, from_addr: str, to_addrs: list[str]):  # noqa: ANN001
        self.send_count += 1
        self.message = message
        self.from_addr = from_addr
        self.to_addrs = to_addrs
        return self.result

    def quit(self):
        self.quit_count += 1
        return "BYE", [b"closed"]


class ForwardingFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _seed(self, *, destination: str | None = "operations@example.com") -> None:
        with self.session_factory() as db:
            db.add_all([
                Company(id=1, name="Tenant A"),
                Company(id=2, name="Tenant B"),
                Mailbox(
                    id=1,
                    company_id=1,
                    name="Inbound A",
                    email_address="inbound@example.com",
                    smtp_provider="smtp",
                    smtp_enabled=True,
                    smtp_host="smtp.example.com",
                    smtp_port=587,
                    smtp_security="starttls",
                    smtp_username="relay@example.com",
                    smtp_password_encrypted=encrypt_secret("smtp-secret"),
                    from_email="relay@example.com",
                ),
                Department(id=10, company_id=1, name="Operations", destination_email=destination),
                Communication(
                    id=100,
                    company_id=1,
                    mailbox_id=1,
                    external_message_id="<incoming-100@example.com>",
                    provider="imap",
                    sender_email="customer@example.com",
                    to_recipients='["inbound@example.com"]',
                    cc_recipients='["copy@example.com"]',
                    subject="Consulta original",
                    body_text="Contenido original",
                    body_html="<p>Contenido original</p>",
                    received_at=datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc),
                ),
                RoutingDecision(
                    id=200,
                    company_id=1,
                    communication_id=100,
                    department_id=10,
                    final_department_id=10,
                    category="operations",
                    confidence=0.95,
                    requires_review=False,
                    reason="Confirmed by user",
                    status="confirmed",
                    source="manual",
                ),
            ])
            db.commit()

    def _call(self, db, smtp: FakeSmtp, **kwargs):  # noqa: ANN001
        return forward_communication(
            db,
            company_id=1,
            communication_id=100,
            routing_decision_id=200,
            department_id=10,
            smtp_client_factory=lambda _mailbox: smtp,
            **kwargs,
        )

    def test_success_uses_mailbox_smtp_reply_to_and_attachment(self):
        self._seed()
        with self.session_factory() as db:
            communication = db.get(Communication, 100)
            communication.attachments.append(
                CommunicationAttachment(
                    company_id=1,
                    filename="factura.pdf",
                    mime_type="application/pdf",
                    storage_ref="blob://factura-100",
                )
            )
            db.commit()
            smtp = FakeSmtp()

            with patch("app.routing.forwarding.read_attachment", return_value=b"pdf-content"):
                action = self._call(db, smtp)

            self.assertEqual(action.status, "sent")
            self.assertEqual(action.destination_email, "operations@example.com")
            self.assertEqual(action.provider_message_id, "provider-123")
            self.assertEqual(smtp.login_args, ("relay@example.com", "smtp-secret"))
            self.assertEqual(smtp.from_addr, "relay@example.com")
            self.assertEqual(smtp.to_addrs, ["operations@example.com"])
            self.assertEqual(smtp.message["Reply-To"], "customer@example.com")
            self.assertIn("Contenido original", smtp.message.get_body().get_content())
            self.assertEqual(
                [part.get_filename() for part in smtp.message.iter_attachments()],
                ["factura.pdf"],
            )

    def test_idempotency_does_not_send_twice(self):
        self._seed()
        with self.session_factory() as db:
            smtp = FakeSmtp()
            first = self._call(db, smtp)
            db.commit()
            second = self._call(db, smtp)

            self.assertEqual(first.id, second.id)
            self.assertEqual(second.attempt_count, 1)
            self.assertEqual(smtp.send_count, 1)
            self.assertEqual(db.query(RoutingAction).count(), 1)

    def test_transient_provider_error_can_be_retried_explicitly(self):
        self._seed()

        class FlakySmtp(FakeSmtp):
            def send_message(self, message, *, from_addr: str, to_addrs: list[str]):  # noqa: ANN001
                self.send_count += 1
                if self.send_count == 1:
                    raise smtplib.SMTPServerDisconnected("smtp-secret should not be persisted")
                self.message = message
                self.from_addr = from_addr
                self.to_addrs = to_addrs
                return self.result

        with self.session_factory() as db:
            smtp = FlakySmtp()
            failed = self._call(db, smtp)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.error_code, "server_disconnected")
            self.assertNotIn("smtp-secret", failed.error_message or "")
            db.commit()

            sent = self._call(db, smtp, retry_failed=True)
            self.assertEqual(sent.status, "sent")
            self.assertEqual(sent.attempt_count, 2)
            self.assertEqual(smtp.send_count, 2)
            self.assertEqual(db.query(RoutingAction).count(), 1)

    def test_permanent_failure_is_not_retried_implicitly(self):
        self._seed(destination=None)
        with self.session_factory() as db:
            smtp = FakeSmtp()
            failed = self._call(db, smtp)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.error_code, "invalid_destination")
            db.commit()

            same_failed = self._call(db, smtp, retry_failed=True)
            self.assertEqual(same_failed.id, failed.id)
            self.assertEqual(same_failed.attempt_count, 1)
            self.assertEqual(smtp.send_count, 0)

    def test_missing_smtp_configuration_is_a_controlled_failure(self):
        self._seed()
        with self.session_factory() as db:
            db.get(Mailbox, 1).smtp_enabled = False
            db.commit()
            smtp = FakeSmtp()

            action = self._call(db, smtp)

            self.assertEqual(action.status, "failed")
            self.assertEqual(action.error_code, "invalid_configuration")
            self.assertEqual(smtp.send_count, 0)

    def test_tenant_mismatch_is_rejected_before_delivery(self):
        self._seed()
        with self.session_factory() as db:
            smtp = FakeSmtp()
            with self.assertRaises(ForwardingValidationError):
                forward_communication(
                    db,
                    company_id=2,
                    communication_id=100,
                    routing_decision_id=200,
                    department_id=10,
                    smtp_client_factory=lambda _mailbox: smtp,
                )
            self.assertEqual(smtp.send_count, 0)
            self.assertEqual(db.query(RoutingAction).count(), 0)

    def test_trigger_user_must_belong_to_tenant(self):
        self._seed()
        with self.session_factory() as db:
            smtp = FakeSmtp()
            with self.assertRaises(ForwardingValidationError):
                self._call(db, smtp, triggered_by_user_id=999)
            self.assertEqual(db.query(RoutingAction).count(), 0)

    def test_caller_controls_commit(self):
        self._seed()
        with self.session_factory() as db:
            action = self._call(db, FakeSmtp())
            self.assertEqual(action.status, "sent")
            db.rollback()

        with self.session_factory() as db:
            self.assertEqual(db.query(RoutingAction).count(), 0)


if __name__ == "__main__":
    unittest.main()
