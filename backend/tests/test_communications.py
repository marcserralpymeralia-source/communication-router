from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.encryption import encrypt_secret
from app.db.database import Base
from app.db.models import Communication, CommunicationAttachment, Company, Email, InboundMessage, Mailbox
from app.mailboxes.service import get_or_create_mailbox_sync_state
from app.master.database import MasterBase
from app.master.models import MailboxSyncState
from app.communications.service import count_communications, create_or_update_communication_from_email, get_communication, list_communications, recipient_values
from app.communications.routes import communication_detail, communications_list
from app.master.service import TenantRole, TenantUser
from app.settings.integrations import SYNC_LOCKS, _fetch_imap_emails, _message_received_at, _sync_state_matches_scope, _update_sync_checkpoint, effective_unread_only, is_kibak_internal_sender


class FakeImapClient:
    def __init__(self, raw_message: bytes, internal_date: str | None = None) -> None:
        self.raw_message = raw_message
        self.internal_date = internal_date

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
            internal_date = f' INTERNALDATE "{self.internal_date}"' if self.internal_date else ""
            meta = f"140 (UID 140{internal_date} RFC822 {{999}})".encode()
            return "OK", [(meta, self.raw_message)]
        return "OK", [b""]

    def logout(self):
        return "BYE", [b"logout"]


class CommunicationFoundationTests(unittest.TestCase):

    def test_kibak_internal_sender_domain_is_excluded_without_matching_other_domains(self):
        self.assertTrue(is_kibak_internal_sender("relay@ingesco.com"))
        self.assertTrue(is_kibak_internal_sender("RELAY@INGESCO.COM"))
        self.assertFalse(is_kibak_internal_sender("relay@quibac.com"))
        self.assertFalse(is_kibak_internal_sender("relay@ingesco.test"))

    def test_kibak_mailbox_polling_does_not_depend_on_seen_flag(self):
        self.assertFalse(effective_unread_only(configured=True, app_slug="kibak", mailbox_id=1))
        self.assertEqual(effective_unread_only(configured=True, app_slug="anchi", mailbox_id=1), True)
        self.assertIsNone(effective_unread_only(configured=None, app_slug="kibak", mailbox_id=None))

    def test_imap_date_is_normalized_for_kibak_communications(self):
        from email.message import EmailMessage

        message = EmailMessage()
        message["Date"] = "Mon, 14 Sep 2026 09:30:00 +0200"

        self.assertEqual(
            _message_received_at(message),
            datetime(2026, 9, 14, 7, 30, tzinfo=timezone.utc),
        )

    def test_imap_internaldate_fills_missing_message_date(self):
        from email.message import EmailMessage

        message = EmailMessage()
        message["Subject"] = "Sin cabecera Date"
        fetch_meta = '140 (UID 140 INTERNALDATE "14-Sep-2026 09:30:00 +0200" RFC822)'

        self.assertEqual(
            _message_received_at(message, fetch_meta),
            datetime(2026, 9, 14, 7, 30, tzinfo=timezone.utc),
        )

    def test_kibak_duplicate_repair_fills_received_at_without_creating_communication(self):
        raw_message = (
            b"From: sender@example.com\r\n"
            b"To: inbox@example.com\r\n"
            b"Subject: Historical message\r\n"
            b"Message-ID: <historical-repair@example.com>\r\n"
            b"\r\n"
            b"Body"
        )
        with self.tenant_session() as db:
            db.add(Company(id=1, name="Tenant A"))
            mailbox = Mailbox(
                id=1,
                company_id=1,
                name="Inbox",
                email_address="inbox@example.com",
                provider="microsoft365",
                imap_host="outlook.office365.com",
                imap_username="inbox@example.com",
                imap_password_encrypted=encrypt_secret("password"),
                read_unread_only=False,
            )
            db.add(mailbox)
            db.flush()
            existing = Communication(
                company_id=1,
                mailbox_id=mailbox.id,
                provider="microsoft365",
                external_message_id="<historical-repair@example.com>",
                subject="Historical message",
                body_text="Body",
                received_at=None,
                processing_status="received",
                routing_status="unclassified",
            )
            db.add(existing)
            db.commit()
            mailbox_id = mailbox.id

            with patch(
                "app.settings.integrations._imap_client",
                return_value=FakeImapClient(raw_message, "14-Sep-2026 09:30:00 +0200"),
            ), patch(
                "app.settings.integrations.get_settings",
                return_value=SimpleNamespace(app_slug="kibak", is_pilot_runtime=False),
            ):
                original_dialect_name = self.tenant_engine.dialect.name
                self.tenant_engine.dialect.name = "postgresql"
                try:
                    result = _fetch_imap_emails(
                        db,
                        mailbox,
                        1,
                        unread_only=False,
                        mailbox_id=mailbox_id,
                    )
                finally:
                    self.tenant_engine.dialect.name = original_dialect_name

            db.commit()
            communications = db.scalars(select(Communication)).all()

        self.assertEqual(result["saved"], 0)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(len(communications), 1)
        self.assertEqual(
            communications[0].received_at.replace(tzinfo=timezone.utc),
            datetime(2026, 9, 14, 7, 30, tzinfo=timezone.utc),
        )

    def test_kibak_internal_sender_is_discarded_and_cursor_advances(self):
        raw_message = (
            b"From: relay@ingesco.com\r\n"
            b"To: inbox@example.com\r\n"
            b"Subject: Internal forward\r\n"
            b"Message-ID: <internal-forward@example.com>\r\n"
            b"Date: Wed, 23 Sep 2026 10:00:00 +0000\r\n"
            b"\r\n"
            b"Forwarded message"
        )
        with self.tenant_session() as db, self.master_session() as master_db:
            db.add(Company(id=1, name="Tenant A"))
            mailbox = Mailbox(
                id=1,
                company_id=1,
                name="Inbox",
                email_address="inbox@example.com",
                provider="microsoft365",
                imap_host="outlook.office365.com",
                imap_username="inbox@example.com",
                imap_password_encrypted=encrypt_secret("password"),
                read_unread_only=True,
            )
            db.add(mailbox)
            db.flush()
            state = MailboxSyncState(company_id=1, mailbox_id=mailbox.id, last_seen_uid="139")
            master_db.add(state)
            master_db.commit()

            with patch(
                "app.settings.integrations._imap_client",
                return_value=FakeImapClient(raw_message, "23-Sep-2026 10:00:00 +0000"),
            ), patch(
                "app.settings.integrations.get_settings",
                return_value=SimpleNamespace(app_slug="kibak", is_pilot_runtime=False),
            ):
                original_dialect_name = self.tenant_engine.dialect.name
                self.tenant_engine.dialect.name = "postgresql"
                try:
                    result = _fetch_imap_emails(
                        db,
                        mailbox,
                        1,
                        unread_only=False,
                        sync_state=state,
                        sync_session=master_db,
                        mailbox_id=mailbox.id,
                    )
                finally:
                    self.tenant_engine.dialect.name = original_dialect_name

            db.commit()
            self.assertEqual(result["saved"], 0)
            self.assertEqual(result["discarded"], 1)
            self.assertEqual(db.scalars(select(Communication)).all(), [])
            self.assertEqual(state.last_seen_uid, "140")

    def test_mailbox_sync_state_uses_mailbox_id_without_legacy_mailbox_field(self):
        with self.master_session() as db:
            state = MailboxSyncState(company_id=1, mailbox_id=1, last_seen_uid="41")
            db.add(state)
            db.commit()

            scope = {
                "provider": "microsoft365",
                "host": "outlook.office365.com",
                "username": "mailbox@example.com",
                "connected_email": "mailbox@example.com",
                "mailbox": "INBOX",
            }
            self.assertTrue(_sync_state_matches_scope(state, scope, None))
            _update_sync_checkpoint(
                state,
                db,
                mailbox="INBOX",
                uidvalidity="777",
                source_provider="microsoft365",
                source_host="outlook.office365.com",
                source_username="mailbox@example.com",
                source_connected_email="mailbox@example.com",
                last_uid="42",
                saved=1,
                duplicates=0,
                attachments_saved=0,
                found=1,
                status="idle",
            )

            self.assertEqual(state.last_seen_uid, "42")
            self.assertEqual(state.status, "idle")
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

    def test_communications_list_prioritizes_received_messages_over_undated_rows(self):
        with self.tenant_session() as db:
            db.add(Company(id=1, name="Tenant A"))
            db.add(Mailbox(id=1, company_id=1, name="A", email_address="a@example.com"))
            db.flush()
            undated = create_or_update_communication_from_email(
                db,
                company_id=1,
                mailbox_id=1,
                provider="microsoft365",
                external_message_id="undated",
                subject="Historical import",
            )
            dated = create_or_update_communication_from_email(
                db,
                company_id=1,
                mailbox_id=1,
                provider="microsoft365",
                external_message_id="dated",
                subject="New message",
                received_at=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
            )
            db.commit()

            rows = list_communications(db, 1, limit=10)

        self.assertEqual([row.id for row in rows], [dated.id, undated.id])

    def test_kibak_communications_exclude_internal_ingesco_senders(self):
        with self.tenant_session() as db:
            db.add(Company(id=1, name="Tenant A"))
            db.add(Mailbox(id=1, company_id=1, name="A", email_address="a@example.com"))
            db.flush()
            external = create_or_update_communication_from_email(
                db,
                company_id=1,
                mailbox_id=1,
                provider="microsoft365",
                external_message_id="external",
                sender_email="client@example.com",
                subject="External",
            )
            internal = create_or_update_communication_from_email(
                db,
                company_id=1,
                mailbox_id=1,
                provider="microsoft365",
                external_message_id="internal",
                sender_email="central@ingesco.com",
                subject="Internal copy",
            )
            db.commit()

            rows = list_communications(db, 1, limit=10, exclude_internal_senders=True)
            total = count_communications(db, 1, exclude_internal_senders=True)
            hidden = get_communication(db, 1, internal.id, exclude_internal_senders=True)

        self.assertEqual([row.id for row in rows], [external.id])
        self.assertEqual(total, 1)
        self.assertIsNone(hidden)


if __name__ == "__main__":
    unittest.main()
