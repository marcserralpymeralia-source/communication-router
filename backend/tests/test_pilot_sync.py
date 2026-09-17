from __future__ import annotations

import unittest
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.encryption import encrypt_secret
from app.db.database import Base
from app.db.models import BackgroundJob, Communication, CommunicationAttachment, Company, Mailbox, RoutingAction, RoutingDecision
from app.mailboxes.pilot_sync import run_pilot_sync
from app.mailboxes.routes import pilot_sync_mailbox
from app.master.service import TenantRole, TenantUser


def raw_message(number: int, *, attachment: bool = False, date: str = "Mon, 14 Sep 2026 10:00:00 +0000") -> bytes:
    message = EmailMessage()
    message["From"] = f"sender{number}@example.com"
    message["To"] = "pilot@example.com"
    message["Subject"] = f"Pilot {number}"
    message["Message-ID"] = f"<pilot-{number}@example.com>"
    message["Date"] = date
    message.set_content(f"Contenido de prueba {number}")
    if attachment:
        message.add_attachment(b"no se guarda", maintype="text", subtype="plain", filename="factura.txt")
    return message.as_bytes()


class FakePilotIMAP:
    def __init__(self, messages: dict[str, bytes], search_result: bytes = b"1 2 3 4 5") -> None:
        self.messages = messages
        self.search_result = search_result
        self.calls: list[tuple] = []

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"5"]

    def status(self, folder, _query):
        self.calls.append(("status", folder))
        return "OK", [b"INBOX (UIDVALIDITY 77)"]

    def uid(self, command, message_id, *args):
        self.calls.append((command, message_id, *args))
        if command == "search":
            return "OK", [self.search_result]
        if command == "fetch":
            uid = message_id.decode()
            return "OK", [(f"{uid} (UID {uid} RFC822)".encode(), self.messages[uid])]
        if command == "store":
            raise AssertionError("El piloto no debe ejecutar STORE")
        raise AssertionError(f"Comando IMAP no permitido: {command}")

    def logout(self):
        self.calls.append(("logout",))


class PilotSyncTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.engine.dispose()

    def _mailbox(self, db):
        db.add(Company(id=1, name="Pilot"))
        mailbox = Mailbox(
            id=1,
            company_id=1,
            name="Quibac",
            email_address="pilot@example.com",
            provider="microsoft365",
            connection_method="oauth2",
            imap_host="outlook.office365.com",
            imap_username="pilot@example.com",
            imap_security="ssl_tls",
            refresh_token_encrypted=encrypt_secret("refresh-token"),
            enabled=False,
            auto_sync_enabled=False,
            mark_as_read_after_import=False,
        )
        db.add(mailbox)
        db.commit()
        return mailbox

    def _run(self, db, client):
        with patch("app.mailboxes.pilot_sync._imap_client", return_value=client), patch(
            "app.mailboxes.pilot_sync._imap_authenticate"
        ):
            return run_pilot_sync(db, db.get(Mailbox, 1), 1)

    def test_bounded_readonly_import_skips_attachments_without_side_effects(self):
        with self.Session() as db:
            self._mailbox(db)
            client = FakePilotIMAP({
                "1": raw_message(1),
                "2": raw_message(2),
                "3": raw_message(3),
                "4": raw_message(4),
                "5": raw_message(5, attachment=True),
            })
            result = self._run(db, client)

            self.assertTrue(result["ok"])
            self.assertEqual(result["imported"], 3)
            self.assertEqual(result["skipped_attachment"], 1)
            self.assertEqual(result["candidates_reviewed"], 4)
            self.assertEqual(db.query(Communication).count(), 3)
            self.assertEqual(db.query(CommunicationAttachment).count(), 0)
            self.assertEqual(db.query(BackgroundJob).count(), 0)
            self.assertEqual(db.query(RoutingDecision).count(), 0)
            self.assertEqual(db.query(RoutingAction).count(), 0)
            mailbox = db.get(Mailbox, 1)
            self.assertFalse(mailbox.enabled)
            self.assertFalse(mailbox.auto_sync_enabled)
            self.assertFalse(mailbox.mark_as_read_after_import)
            self.assertEqual([call[0] for call in client.calls], ["select", "status", "search", "fetch", "fetch", "fetch", "fetch", "logout"])
            self.assertEqual(client.calls[0], ("select", "INBOX", True))
            self.assertEqual(client.calls[2], ("search", None, "SINCE", "14-Sep-2026", "UNSEEN"))
            self.assertTrue(all(item.processing_status == "received" for item in db.scalars(select(Communication))))

    def test_date_filter_excludes_messages_before_minimum_date(self):
        with self.Session() as db:
            self._mailbox(db)
            client = FakePilotIMAP(
                {"1": raw_message(1, date="Sun, 13 Sep 2026 23:59:59 +0000")},
                search_result=b"1",
            )
            result = self._run(db, client)
            self.assertTrue(result["ok"])
            self.assertEqual(result["imported"], 0)
            self.assertEqual(db.query(Communication).count(), 0)
            self.assertEqual(client.calls[2], ("search", None, "SINCE", "14-Sep-2026", "UNSEEN"))

    def test_candidate_scan_is_capped_at_ten_recent_uids(self):
        with self.Session() as db:
            self._mailbox(db)
            client = FakePilotIMAP(
                {str(number): raw_message(number) for number in range(1, 13)},
                search_result=b"1 2 3 4 5 6 7 8 9 10 11 12",
            )
            result = self._run(db, client)
            fetches = [call[1].decode() for call in client.calls if call[0] == "fetch"]
            self.assertEqual(result["imported"], 3)
            self.assertEqual(fetches, ["12", "11", "10"])

    def test_endpoint_is_separate_from_normal_sync_and_returns_summary(self):
        with self.Session() as db:
            mailbox = self._mailbox(db)
            user = TenantUser(
                id=1,
                email="admin@example.com",
                name="Admin",
                is_active=True,
                company_id=1,
                company_name="Pilot",
                company_slug="pilot",
                role=TenantRole("Administrador"),
                membership_id=1,
            )
            result = {
                "ok": True,
                "imported": 2,
                "duplicates": 1,
                "skipped_attachment": 1,
                "candidates_reviewed": 4,
            }
            with patch("app.mailboxes.routes.run_pilot_sync", return_value=result) as pilot:
                response = pilot_sync_mailbox(
                    mailbox.id,
                    SimpleNamespace(headers={"accept": "application/json"}),
                    db,
                    user,
                )
            self.assertEqual(response.status_code, 200)
            self.assertIs(pilot.call_args.args[0], db)
            self.assertIs(pilot.call_args.args[1], mailbox)
            self.assertEqual(db.query(Communication).count(), 0)

    def test_repeating_pilot_sync_deduplicates_without_cursor_state(self):
        with self.Session() as db:
            self._mailbox(db)
            client = FakePilotIMAP({str(number): raw_message(number) for number in range(1, 6)})
            first = self._run(db, client)
            second = self._run(db, client)
            self.assertEqual(first["imported"], 3)
            self.assertEqual(second["imported"], 2)
            self.assertEqual(second["duplicates"], 3)
            self.assertEqual(db.query(Communication).count(), 5)

    def test_pilot_rejects_enabled_or_non_microsoft_mailboxes(self):
        with self.Session() as db:
            mailbox = self._mailbox(db)
            mailbox.enabled = True
            self.assertFalse(run_pilot_sync(db, mailbox, 1)["ok"])
            mailbox.enabled = False
            mailbox.provider = "imap"
            self.assertFalse(run_pilot_sync(db, mailbox, 1)["ok"])


if __name__ == "__main__":
    unittest.main()
