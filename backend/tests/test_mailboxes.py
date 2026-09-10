from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.encryption import decrypt_secret, encrypt_secret
from app.db.database import Base
from app.db.models import AuditLog, Company, EmailSettings, Mailbox
from app.mailboxes.service import get_mailbox, get_or_create_mailbox_sync_state
from app.mailboxes.routes import create_mailbox, test_mailbox, test_mailbox_smtp, update_mailbox
from app.master.database import MasterBase
from app.master.models import MailboxSyncState, MasterCompany
from app.master.service import TenantRole, TenantUser
from app.migrations.registry import _apply_tenant_mailboxes
from app.settings.integrations import SYNC_LOCKS, _normalized_email_external_id
from app.workers.jobs_worker import _email_job_context


class FakeForm(dict):
    def multi_items(self):
        return list(self.items())


class FakeRequest:
    def __init__(self, form_data):
        self.headers = {"accept": "text/html", "content-type": "application/x-www-form-urlencoded"}
        self._form_data = FakeForm(form_data)

    async def form(self):
        return self._form_data


class MailboxFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tenant_engine = create_engine("sqlite:///:memory:")
        self.master_engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.tenant_engine)
        MasterBase.metadata.create_all(self.master_engine)
        self.tenant_session = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        self.master_session = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.tenant_engine.dispose()
        self.master_engine.dispose()
        SYNC_LOCKS.clear()

    def test_tenant_can_have_two_mailboxes_with_independent_sync_states(self):
        with self.tenant_session() as db, self.master_session() as master_db:
            db.add(Company(id=1, name="Tenant A"))
            db.commit()
            first = Mailbox(company_id=1, name="Info", email_address="info@empresa.com")
            second = Mailbox(company_id=1, name="Hola", email_address="hola@empresa.com")
            db.add_all([first, second])
            db.commit()
            db.refresh(first)
            db.refresh(second)

            state_a = get_or_create_mailbox_sync_state(master_db, first)
            state_b = get_or_create_mailbox_sync_state(master_db, second)
            state_a.last_seen_uid = "10"
            state_a.last_error_message = "solo A"
            master_db.commit()

            self.assertNotEqual(first.id, second.id)
            self.assertNotEqual(state_a.id, state_b.id)
            self.assertEqual(master_db.get(MailboxSyncState, state_b.id).last_seen_uid, None)
            self.assertEqual(master_db.get(MailboxSyncState, state_b.id).last_error_message, None)

    def test_two_tenants_keep_mailbox_configuration_independent(self):
        with self.tenant_session() as db:
            db.add_all([Company(id=1, name="Tenant A"), Company(id=2, name="Tenant B")])
            db.add_all(
                [
                    Mailbox(company_id=1, name="A", email_address="shared@example.com", imap_host="imap.a.test"),
                    Mailbox(company_id=2, name="B", email_address="shared@example.com", imap_host="imap.b.test"),
                ]
            )
            db.commit()
            self.assertEqual(db.scalar(select(Mailbox).where(Mailbox.company_id == 1)).imap_host, "imap.a.test")
            self.assertEqual(db.scalar(select(Mailbox).where(Mailbox.company_id == 2)).imap_host, "imap.b.test")

    def test_mailbox_lookup_rejects_another_tenant(self):
        with self.tenant_session() as db:
            db.add_all([Company(id=1, name="Tenant A"), Company(id=2, name="Tenant B")])
            db.add(Mailbox(company_id=2, name="B", email_address="b@example.com"))
            db.commit()
            mailbox = db.scalar(select(Mailbox).where(Mailbox.company_id == 2))
            self.assertIsNone(get_mailbox(db, 1, mailbox.id))
            self.assertIsNotNone(get_mailbox(db, 2, mailbox.id))

    def test_legacy_email_settings_are_copied_without_decrypting_secret(self):
        with self.tenant_session() as db:
            db.add(Company(id=1, name="Tenant A"))
            db.add(
                EmailSettings(
                    company_id=1,
                    connected_email="Legacy@Empresa.com",
                    imap_username="Legacy@Empresa.com",
                    imap_host="imap.example.com",
                    imap_password_encrypted=encrypt_secret("secret"),
                    auto_sync_enabled=True,
                )
            )
            db.commit()

        _apply_tenant_mailboxes(self.tenant_engine, dry_run=False)

        with self.tenant_session() as db:
            mailbox = db.scalar(select(Mailbox).where(Mailbox.company_id == 1))
            self.assertIsNotNone(mailbox)
            self.assertEqual(mailbox.email_address, "legacy@empresa.com")
            self.assertEqual(mailbox.imap_host, "imap.example.com")
            self.assertNotEqual(mailbox.imap_password_encrypted, "secret")
            self.assertEqual(decrypt_secret(mailbox.imap_password_encrypted), "secret")

    def test_new_mailbox_dedupe_and_locks_include_mailbox_id(self):
        self.assertNotEqual(
            _normalized_email_external_id("INBOX", "77", "10", 1),
            _normalized_email_external_id("INBOX", "77", "10", 2),
        )
        self.assertNotEqual(SYNC_LOCKS.setdefault((1, 1), object()), SYNC_LOCKS.setdefault((1, 2), object()))

    def test_master_state_is_keyed_by_company_and_mailbox(self):
        with self.master_session() as db:
            db.add_all([MasterCompany(id=1, name="A", slug="a"), MasterCompany(id=2, name="B", slug="b")])
            db.add_all(
                [
                    MailboxSyncState(company_id=1, mailbox_id=1),
                    MailboxSyncState(company_id=1, mailbox_id=2),
                    MailboxSyncState(company_id=2, mailbox_id=1),
                ]
            )
            db.commit()
            self.assertEqual(db.query(MailboxSyncState).count(), 3)

    def test_connection_test_targets_the_requested_mailbox(self):
        with self.tenant_session() as db:
            db.add(Company(id=1, name="Tenant A"))
            db.add_all(
                [
                    Mailbox(company_id=1, name="Info", email_address="info@example.com"),
                    Mailbox(company_id=1, name="Hola", email_address="hola@example.com"),
                ]
            )
            db.commit()
            mailbox = db.scalar(select(Mailbox).where(Mailbox.email_address == "hola@example.com"))
            user = TenantUser(
                id=1,
                email="admin@example.com",
                name="Admin",
                is_active=True,
                company_id=1,
                company_name="Tenant A",
                company_slug="tenant-a",
                role=TenantRole("Administrador"),
                membership_id=1,
            )
            request = SimpleNamespace(headers={"accept": "application/json"}, state=SimpleNamespace(request_id="test"))
            with patch("app.mailboxes.routes.test_imap_connection", return_value={"ok": True, "message": "ok"}) as connection_test:
                response = test_mailbox(mailbox.id, request, db, user)
            self.assertEqual(response.status_code, 200)
            self.assertIs(connection_test.call_args.args[0], mailbox)

            with patch("app.mailboxes.routes.test_smtp_connection", return_value={"ok": True, "message": "ok"}) as smtp_test:
                response = test_mailbox_smtp(mailbox.id, request, db, user)
            self.assertEqual(response.status_code, 200)
            self.assertIs(smtp_test.call_args.args[0], mailbox)

    def test_create_mailbox_is_disabled_and_returns_visible_save_feedback(self):
        with self.tenant_session() as db, self.master_session() as master_db:
            db.add(Company(id=1, name="Tenant A"))
            db.commit()
            user = TenantUser(
                id=1,
                email="admin@example.com",
                name="Admin",
                is_active=True,
                company_id=1,
                company_name="Tenant A",
                company_slug="tenant-a",
                role=TenantRole("Administrador"),
                membership_id=1,
            )
            request = FakeRequest(
                {
                    "name": "Nuevo buzón",
                    "email_address": "nuevo@example.com",
                    "imap_username": "nuevo@example.com",
                    "imap_password_encrypted": "fake-password",
                    "imap_host": "imap.example.com",
                    "imap_port": "993",
                    "imap_security": "ssl_tls",
                    "inbox_folder": "INBOX",
                }
            )
            with patch("app.mailboxes.routes.test_imap_connection") as connection_test:
                response = asyncio.run(create_mailbox(request, db, master_db, user))

            self.assertEqual(response.status_code, 303)
            query = parse_qs(urlparse(response.headers["location"]).query)
            self.assertEqual(query["mailbox_message"], ["Configuración IMAP guardada correctamente."])
            self.assertNotIn("fake-password", response.headers["location"])
            mailbox = db.scalar(select(Mailbox).where(Mailbox.company_id == 1))
            self.assertIsNotNone(mailbox)
            self.assertFalse(mailbox.enabled)
            self.assertFalse(mailbox.auto_sync_enabled)
            self.assertFalse(mailbox.smtp_enabled)
            self.assertEqual(decrypt_secret(mailbox.imap_password_encrypted), "fake-password")
            self.assertEqual(master_db.query(MailboxSyncState).count(), 1)
            self.assertFalse(master_db.scalar(select(MailboxSyncState)).enabled)
            connection_test.assert_not_called()

    def test_update_mailbox_does_not_duplicate_or_activate_and_reports_feedback(self):
        with self.tenant_session() as db, self.master_session() as master_db:
            db.add(Company(id=1, name="Tenant A"))
            mailbox = Mailbox(company_id=1, name="Existente", email_address="existente@example.com", enabled=False, auto_sync_enabled=False)
            db.add(mailbox)
            db.commit()
            db.refresh(mailbox)
            user = TenantUser(
                id=1,
                email="admin@example.com",
                name="Admin",
                is_active=True,
                company_id=1,
                company_name="Tenant A",
                company_slug="tenant-a",
                role=TenantRole("Administrador"),
                membership_id=1,
            )
            request = FakeRequest(
                {
                    "name": "Existente",
                    "email_address": "existente@example.com",
                    "imap_username": "existente@example.com",
                    "imap_password_encrypted": "fake-password",
                    "imap_host": "imap.example.com",
                    "imap_port": "993",
                    "imap_security": "ssl_tls",
                    "inbox_folder": "INBOX",
                }
            )
            with patch("app.mailboxes.routes.test_imap_connection") as connection_test:
                response = asyncio.run(update_mailbox(mailbox.id, request, db, master_db, user))

            self.assertEqual(response.status_code, 303)
            query = parse_qs(urlparse(response.headers["location"]).query)
            self.assertEqual(query["mailbox_message"], ["Configuración IMAP guardada correctamente."])
            self.assertEqual(db.query(Mailbox).filter(Mailbox.company_id == 1).count(), 1)
            saved = db.get(Mailbox, mailbox.id)
            self.assertFalse(saved.enabled)
            self.assertFalse(saved.auto_sync_enabled)
            self.assertEqual(decrypt_secret(saved.imap_password_encrypted), "fake-password")
            self.assertFalse(master_db.scalar(select(MailboxSyncState)).enabled)
            self.assertEqual(db.scalar(select(AuditLog).where(AuditLog.action == "settings.mailbox.update")).entity_id, mailbox.id)
            connection_test.assert_not_called()

    def test_invalid_mailbox_save_returns_safe_visible_error(self):
        with self.tenant_session() as db, self.master_session() as master_db:
            db.add(Company(id=1, name="Tenant A"))
            db.commit()
            user = TenantUser(
                id=1,
                email="admin@example.com",
                name="Admin",
                is_active=True,
                company_id=1,
                company_name="Tenant A",
                company_slug="tenant-a",
                role=TenantRole("Administrador"),
                membership_id=1,
            )
            response = asyncio.run(create_mailbox(FakeRequest({"email_address": "no-es-email"}), db, master_db, user))

            self.assertEqual(response.status_code, 303)
            query = parse_qs(urlparse(response.headers["location"]).query)
            self.assertTrue(query["mailbox_error"][0].startswith("No se ha podido guardar la configuración IMAP."))
            self.assertNotIn("no-es-email", query["mailbox_error"][0])
            self.assertEqual(db.query(Mailbox).filter(Mailbox.company_id == 1).count(), 0)

    def test_email_job_context_resolves_only_the_requested_mailbox(self):
        with self.tenant_session() as db, self.master_session() as master_db:
            db.add_all([Company(id=1, name="Tenant A"), Company(id=2, name="Tenant B")])
            mailbox = Mailbox(company_id=1, name="Pedidos", email_address="pedidos@example.com")
            db.add(mailbox)
            db.commit()
            db.refresh(mailbox)

            resolved, state, mailbox_id = _email_job_context(
                db,
                master_db,
                1,
                {"mailbox_id": mailbox.id},
            )
            self.assertIs(resolved, mailbox)
            self.assertEqual(state.mailbox_id, mailbox.id)
            self.assertEqual(mailbox_id, mailbox.id)
            with self.assertRaisesRegex(RuntimeError, "No se encontró"):
                _email_job_context(db, master_db, 2, {"mailbox_id": mailbox.id})


if __name__ == "__main__":
    unittest.main()
