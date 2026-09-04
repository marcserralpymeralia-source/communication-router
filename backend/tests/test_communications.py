from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.encryption import encrypt_secret
from app.db.database import Base
from app.db.models import Communication, CommunicationAttachment, Company, Email, InboundMessage, Mailbox
from app.mailboxes.service import get_or_create_mailbox_sync_state
from app.master.database import MasterBase
from app.communications.service import create_or_update_communication_from_email, get_communication, recipient_values
from app.communications.routes import communication_detail, communications_list
from app.master.service import TenantRole, TenantUser
from app.settings.integrations import SYNC_LOCKS, _fetch_imap_emails


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
            meta = b"140 (UID 140 RFC822 {999})"
            return "OK", [(meta, self.raw_message)]
        return "OK", [b""]

    def logout(self):
        return "BYE", [b"logout"]


class CommunicationFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tenant_engine = create_engine("sqlite:///:memory:")
        self.master_engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.tenant_engine)
        MasterBase.metadata.create_all(self.master_engine)
        self.tenant_session = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        self.master_session = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)

    def tearDown(self):
        SYNC_LOCKS.clear()
        self.tenant_engine.dispose()
        self.master_engine.dispose()

    def test_same_external_id_is_independent_per_mailbox_and_retry_is_idempotent(self):
        with self.tenant_session() as db:
            db.add_all([Company(id=1, name="Tenant A"), Company(id=2, name="Tenant B")])
            db.add_all(
                [
                    Mailbox(id=1, company_id=1, name="A", email_address="a@example.com"),
                    Mailbox(id=2, company_id=1, name="B", email_address="b@example.com"),
                ]
            )
            db.commit()
            first = create_or_update_communication_from_email(
                db,
                company_id=1,
                mailbox_id=1,
                provider="imap",
                external_message_id="<same@example.com>",
                sender_email="sender@example.com",
                to_recipients=["a@example.com"],
                subject="A",
                body_text="Mensaje A",
                received_at=datetime.now(timezone.utc),
            )
            retry = create_or_update_communication_from_email(
                db,
                company_id=1,
                mailbox_id=1,
                provider="imap",
                external_message_id="<same@example.com>",
                processing_status="error",
                subject="A reintento",
                body_text="Mensaje A",
            )
            second_mailbox = create_or_update_communication_from_email(
                db,
                company_id=1,
                mailbox_id=2,
                provider="imap",
                external_message_id="<same@example.com>",
                subject="B",
                body_text="Mensaje B",
            )
            db.commit()

            self.assertEqual(first.id, retry.id)
            self.assertNotEqual(first.id, second_mailbox.id)
            self.assertEqual(db.query(Communication).count(), 2)
            self.assertEqual(retry.processing_status, "error")

            tenant_b = get_communication(db, 2, first.id)
            self.assertIsNone(tenant_b)
            self.assertEqual(recipient_values(first.to_recipients), ["a@example.com"])

    def test_imap_mailbox_dual_writes_communication_and_attachments(self):
        raw_message = (
            b"From: Marta Perez <marta@example.com>\r\n"
            b"To: pedidos@example.com\r\n"
            b"Cc: copia@example.com\r\n"
            b"Subject: Pedido con documento\r\n"
            b"Message-ID: <communication-001@example.com>\r\n"
            b"Date: Mon, 27 Jul 2026 10:00:00 +0000\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=BOUNDARY\r\n"
            b"\r\n"
            b"--BOUNDARY\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Texto del pedido\r\n"
            b"--BOUNDARY\r\n"
            b"Content-Type: text/plain; name=pedido.txt\r\n"
            b"Content-Disposition: attachment; filename=pedido.txt\r\n"
            b"\r\n"
            b"Texto extraible del adjunto\r\n"
            b"--BOUNDARY--\r\n"
        )
        with self.tenant_session() as db:
            db.add(Company(id=1, name="Tenant A"))
            db.add_all(
                [
                    Mailbox(
                        id=1,
                        company_id=1,
                        name="Pedidos A",
                        email_address="pedidos-a@example.com",
                        connected_email="pedidos-a@example.com",
                        imap_host="imap.a.test",
                        imap_username="pedidos-a@example.com",
                        imap_password_encrypted=encrypt_secret("password"),
                        read_unread_only=False,
                    ),
                    Mailbox(
                        id=2,
                        company_id=1,
                        name="Pedidos B",
                        email_address="pedidos-b@example.com",
                        connected_email="pedidos-b@example.com",
                        imap_host="imap.b.test",
                        imap_username="pedidos-b@example.com",
                        imap_password_encrypted=encrypt_secret("password"),
                        read_unread_only=False,
                    ),
                ]
            )
            db.commit()
            mailbox_a = db.get(Mailbox, 1)
            mailbox_b = db.get(Mailbox, 2)
            with patch("app.settings.integrations._imap_client", return_value=FakeImapClient(raw_message)):
                with patch("app.settings.integrations.save_attachment", return_value="/tmp/communication-attachment.txt"):
                    result_a = _fetch_imap_emails(db, mailbox_a, 1, unread_only=False, mailbox_id=mailbox_a.id)
                    result_b = _fetch_imap_emails(db, mailbox_b, 1, unread_only=False, mailbox_id=mailbox_b.id)
                    retry = _fetch_imap_emails(db, mailbox_a, 1, unread_only=False, mailbox_id=mailbox_a.id)

            db.commit()
            communications = db.scalars(select(Communication).order_by(Communication.mailbox_id)).all()
            emails = db.scalars(select(Email).order_by(Email.mailbox_id)).all()
            inbound = db.scalars(select(InboundMessage).order_by(InboundMessage.mailbox_id)).all()
            attachments = db.scalars(select(CommunicationAttachment)).all()

            self.assertEqual(result_a["saved"], 1)
            self.assertEqual(result_b["saved"], 1)
            self.assertEqual(retry["duplicates"], 1)
            self.assertEqual(len(communications), 2)
            self.assertEqual([item.mailbox_id for item in communications], [1, 2])
            self.assertEqual(len(emails), 2)
            self.assertEqual(len(inbound), 2)
            self.assertEqual(len(attachments), 2)
            self.assertTrue(all(item.communication_id for item in emails))
            self.assertTrue(all(item.extracted_text == "Texto extraible del adjunto" for item in attachments))
            self.assertEqual(communications[0].sender_email, "marta@example.com")
            self.assertEqual(communications[0].sender_name, "Marta Perez")
            self.assertEqual(recipient_values(communications[0].cc_recipients), ["copia@example.com"])

    def test_sync_states_are_independent_for_communications_mailboxes(self):
        with self.tenant_session() as db, self.master_session() as master_db:
            db.add(Company(id=1, name="Tenant A"))
            db.add_all(
                [
                    Mailbox(id=1, company_id=1, name="A", email_address="a@example.com", auto_sync_enabled=True),
                    Mailbox(id=2, company_id=1, name="B", email_address="b@example.com", auto_sync_enabled=True),
                ]
            )
            db.commit()
            state_a = get_or_create_mailbox_sync_state(master_db, db.get(Mailbox, 1))
            state_b = get_or_create_mailbox_sync_state(master_db, db.get(Mailbox, 2))
            state_a.last_seen_uid = "20"
            master_db.commit()
            self.assertEqual(master_db.get(type(state_b), state_b.id).last_seen_uid, None)

    def test_read_routes_are_scoped_to_the_active_tenant(self):
        with self.tenant_session() as db:
            db.add_all([Company(id=1, name="Tenant A"), Company(id=2, name="Tenant B")])
            db.add_all(
                [
                    Mailbox(id=1, company_id=1, name="A", email_address="a@example.com"),
                    Mailbox(id=2, company_id=2, name="B", email_address="b@example.com"),
                ]
            )
            first = create_or_update_communication_from_email(
                db,
                company_id=1,
                mailbox_id=1,
                provider="imap",
                external_message_id="a-1",
                subject="Tenant A",
            )
            second = create_or_update_communication_from_email(
                db,
                company_id=2,
                mailbox_id=2,
                provider="imap",
                external_message_id="b-1",
                subject="Tenant B",
            )
            db.commit()
            user = TenantUser(
                id=1,
                email="admin-a@example.com",
                name="Admin A",
                is_active=True,
                company_id=1,
                company_name="Tenant A",
                company_slug="tenant-a",
                role=TenantRole("Administrador"),
                membership_id=1,
            )

            listing = communications_list(limit=50, offset=0, db=db, user=user)
            detail = communication_detail(second.id, db=db, user=user)

            self.assertEqual([item["id"] for item in json.loads(listing.body)["items"]], [first.id])
            self.assertEqual(detail.status_code, 404)


if __name__ == "__main__":
    unittest.main()
