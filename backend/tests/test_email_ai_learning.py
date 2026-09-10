from __future__ import annotations

import tempfile
import unittest
import imaplib
import socket
import ssl
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import os
import sys

os.environ.setdefault("APP_ENV", "development")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.encryption import encrypt_secret  # noqa: E402
from app.agent.services import AgentProcessingService  # noqa: E402
from app.channels.service import get_or_create_channel  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Email, EmailAttachment, EmailSettings, LLMSettings, MessageAttachment, PromptExecution, PromptTemplate, PromptVersion  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import EmailSyncState, MasterCompany  # noqa: E402
from app.agent.model_catalog import LEGACY_OPENAI_MODEL_FALLBACK  # noqa: E402
from app.agent.prompt_runtime import run_prompt_execution, validate_prompt_output  # noqa: E402
import app.settings.integrations as integrations  # noqa: E402
from app.settings.integrations import backfill_imap_emails, preview_initial_imap_sync, read_latest_imap_emails, run_initial_imap_sync  # noqa: E402
from scripts.evaluate_agent import run_evaluation  # noqa: E402


class FakeImapClient:
    def __init__(
        self,
        messages: dict[str, bytes] | None = None,
        search_result: bytes | None = None,
        login_exc: Exception | None = None,
        select_status: str = "OK",
        select_payload: bytes = b"2",
    ) -> None:
        self.messages = messages or {}
        self.search_result = search_result
        self.login_exc = login_exc
        self.select_status = select_status
        self.select_payload = select_payload
        self.search_calls: list[tuple] = []
        self.select_calls: list[tuple] = []
        self.uid_calls: list[tuple] = []
        self.login_calls: list[tuple] = []

    def login(self, *_args, **_kwargs):
        self.login_calls.append(_args)
        if self.login_exc is not None:
            raise self.login_exc
        return "OK", [b"logged in"]

    def select(self, *_args, **_kwargs):
        self.select_calls.append((_args, _kwargs))
        return self.select_status, [self.select_payload]

    def status(self, mailbox: str, *_args, **_kwargs):
        return "OK", [f"{mailbox} (UIDVALIDITY 777)".encode()]

    def search(self, *_args, **_kwargs):
        self.search_calls.append(_args)
        if self.search_result is not None:
            return "OK", [self.search_result]
        return "OK", [b"1 2"]

    def uid(self, command, *_args, **_kwargs):
        self.uid_calls.append((command, *_args))
        if command == "search":
            if self.search_result is not None:
                return "OK", [self.search_result]

            ids = sorted(self.messages.keys(), key=int)

            args = [
                arg.decode() if isinstance(arg, bytes) else str(arg)
                for arg in _args
            ]

            if "UID" in args:
                uid_index = args.index("UID")
                if uid_index + 1 < len(args):
                    uid_range = args[uid_index + 1]
                    start_raw, end_raw = uid_range.split(":", 1)

                    try:
                        start_uid = int(start_raw)
                    except ValueError:
                        start_uid = 1

                    end_uid = None
                    if end_raw != "*":
                        try:
                            end_uid = int(end_raw)
                        except ValueError:
                            end_uid = None

                    ids = [
                        uid
                        for uid in ids
                        if int(uid) >= start_uid
                        and (end_uid is None or int(uid) <= end_uid)
                    ]

            return "OK", [" ".join(ids).encode()]
        if command == "fetch":
            uid = _args[0].decode() if isinstance(_args[0], bytes) else str(_args[0])
            raw = self.messages[uid]
            meta = f"{uid} (UID {uid} RFC822 {{123}})".encode()
            return "OK", [(meta, raw)]
        if command == "store":
            return "OK", [b"stored"]
        return "OK", [b""]

    def fetch(self, msg_id, *_args, **_kwargs):
        uid = msg_id.decode()
        raw = self.messages[uid]
        meta = f"{uid} (UID {uid} RFC822 {{123}})".encode()
        return "OK", [(meta, raw)]

    def logout(self):
        return "BYE", [b"logout"]


class EmailAiLearningTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.master_path = base / "master.sqlite"
        self.tenant_path = base / "tenant.sqlite"
        self.master_engine = create_engine(f"sqlite:///{self.master_path.as_posix()}", connect_args={"check_same_thread": False})
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterBase.metadata.create_all(self.master_engine)
        Base.metadata.create_all(self.tenant_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        self.tempdir.cleanup()

    def _seed_prompt(self, db):
        db.add(LLMSettings(company_id=1, api_key_encrypted=encrypt_secret("dummy-key")))
        template = PromptTemplate(company_id=1, name="Clasificacion", purpose="classification", active_version_id=None)
        db.add(template)
        db.flush()
        version = PromptVersion(company_id=1, template_id=template.id, version=1, content='{"role":"system"}', created_by_user_id=None)
        db.add(version)
        db.flush()
        template.active_version_id = version.id
        db.commit()

    def _seed_imap(self):
        master_db = self.MasterSession()
        master_db.add(MasterCompany(id=1, name="Demo", slug="demo", active=True))
        master_db.add(EmailSyncState(company_id=1, channel_key="email", enabled=True, frequency_seconds=60, status="idle"))
        master_db.commit()
        master_db.close()

        tenant_db = self.TenantSession()
        tenant_db.add(
            EmailSettings(
                company_id=1,
                imap_host="imap.example.com",
                imap_port=993,
                imap_use_ssl=True,
                imap_security="ssl_tls",
                imap_username="demo@example.com",
                imap_password_encrypted=encrypt_secret("demo-password"),
                mailbox="INBOX",
                inbox_folder="INBOX",
                read_unread_only=False,
                auto_process_on_fetch=False,
                mark_as_read_after_import=False,
            )
        )
        tenant_db.commit()
        tenant_db.close()

    def test_prompt_execution_records_prompt_and_validation(self):
        db = self.TenantSession()
        self._seed_prompt(db)

        calls = {"count": 0}

        def fake_provider(settings, messages, model):  # noqa: ANN001
            calls["count"] += 1
            self.assertEqual(model, "gpt-5.6-luna")
            self.assertTrue(messages[1]["content"].startswith("Pedido"))
            return {
                "ok": True,
                "content": '{"tipo_correo":"pedido","confianza":0.92,"motivo":"Solicitud clara"}',
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "estimated_cost": 0.0023},
            }

        result = run_prompt_execution(
            db,
            1,
            "classification",
            db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1)),
            "Pedido urgente de 10 unidades.",
            provider_call=fake_provider,
            input_reference="mail-1",
        )

        self.assertTrue(result["validation_ok"])
        self.assertEqual(result["prompt_purpose"], "classification")
        self.assertEqual(result["validated_content"]["tipo_correo"], "pedido")
        self.assertEqual(calls["count"], 1)
        executions = db.scalars(select(PromptExecution)).all()
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0].output_status, "valid")
        db.close()

    def test_extraction_prompt_uses_legacy_runtime_fallback_when_setting_is_blank(self):
        db = self.TenantSession()
        db.add(
            LLMSettings(
                company_id=1,
                provider="openai",
                api_key_encrypted=encrypt_secret("dummy-key"),
                extraction_model="",
                classification_model="gpt-4.1-mini",
                validation_model="gpt-4.1-mini",
            )
        )
        db.commit()

        calls = {"count": 0}

        def fake_provider(settings, messages, model):  # noqa: ANN001
            calls["count"] += 1
            self.assertEqual(model, LEGACY_OPENAI_MODEL_FALLBACK)
            self.assertTrue(messages[1]["content"].startswith("Pedido"))
            return {
                "ok": True,
                "content": '{"pedido":{"lineas":[{"texto_original":"10 cajas","producto_detectado":"Producto demo","cantidad":10,"unidad":"cajas","confianza_extraccion":0.9}]}}',
                "usage": {"prompt_tokens": 11, "completion_tokens": 5, "estimated_cost": 0.0015},
            }

        result = run_prompt_execution(
            db,
            1,
            "extraction",
            db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1)),
            "Pedido urgente de 10 unidades.",
            provider_call=fake_provider,
            input_reference="mail-2",
        )

        self.assertTrue(result["validation_ok"])
        self.assertEqual(result["prompt_purpose"], "extraction")
        self.assertEqual(calls["count"], 1)
        executions = db.scalars(select(PromptExecution)).all()
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0].model, LEGACY_OPENAI_MODEL_FALLBACK)
        db.close()

    def test_backfill_imap_updates_checkpoint_and_deduplicates(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        master_db = self.MasterSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == 1, EmailSyncState.channel_key == "email"))
        messages = {
            "1": (
                b"From: compras@example.com\r\n"
                b"To: pedidos@example.com\r\n"
                b"Subject: Pedido A\r\n"
                b"Message-ID: <pedido-a@example.com>\r\n"
                b"\r\n"
                b"Pedido 1"
            ),
            "2": (
                b"From: compras@example.com\r\n"
                b"To: pedidos@example.com\r\n"
                b"Subject: Pedido B\r\n"
                b"Message-ID: <pedido-b@example.com>\r\n"
                b"\r\n"
                b"Pedido 2"
            ),
        }

        with patch("app.settings.integrations._imap_client", return_value=FakeImapClient(messages)):
            first = backfill_imap_emails(
                tenant_db,
                settings,
                1,
                from_date="2026-07-01",
                to_date="2026-07-16",
                limit=2,
                sync_state=state,
                sync_session=master_db,
            )
            second = backfill_imap_emails(
                tenant_db,
                settings,
                1,
                from_date="2026-07-01",
                to_date="2026-07-16",
                limit=2,
                sync_state=state,
                sync_session=master_db,
            )

        master_db.refresh(state)
        self.assertTrue(first["ok"])
        self.assertEqual(first["saved"], 2)
        self.assertTrue(second["ok"])
        self.assertEqual(second["saved"], 0)
        self.assertEqual(second["duplicates"], 2)
        self.assertEqual(master_db.get(EmailSyncState, state.id).backfill_status, "idle")
        self.assertEqual(master_db.get(EmailSyncState, state.id).backfill_last_uid, "2")
        self.assertEqual(master_db.get(EmailSyncState, state.id).backfill_created, 2)
        self.assertEqual(tenant_db.scalar(select(func.count()).select_from(Email)) or 0, 2)
        tenant_db.close()
        master_db.close()

    def test_backfill_imap_can_resume_in_single_message_batches(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        master_db = self.MasterSession()
        settings = tenant_db.scalar(
            select(EmailSettings).where(EmailSettings.company_id == 1)
        )
        state = master_db.scalar(
            select(EmailSyncState).where(
                EmailSyncState.company_id == 1,
                EmailSyncState.channel_key == "email",
            )
        )

        messages = {
            "1": (
                b"From: compras@example.com\r\n"
                b"To: pedidos@example.com\r\n"
                b"Subject: Pedido incremental A\r\n"
                b"Message-ID: <pedido-incremental-a@example.com>\r\n"
                b"\r\n"
                b"Pedido 1"
            ),
            "2": (
                b"From: compras@example.com\r\n"
                b"To: pedidos@example.com\r\n"
                b"Subject: Pedido incremental B\r\n"
                b"Message-ID: <pedido-incremental-b@example.com>\r\n"
                b"\r\n"
                b"Pedido 2"
            ),
        }

        with patch(
            "app.settings.integrations._imap_client",
            side_effect=[
                FakeImapClient(messages),
                FakeImapClient(messages),
            ],
        ):
            first = backfill_imap_emails(
                tenant_db,
                settings,
                1,
                from_date="2026-07-01",
                to_date="2026-07-16",
                limit=2,
                batch_size=1,
                stop_after_batch=True,
                sync_state=state,
                sync_session=master_db,
            )

            master_db.refresh(state)

            second = backfill_imap_emails(
                tenant_db,
                settings,
                1,
                from_date="2026-07-01",
                to_date="2026-07-16",
                limit=1,
                batch_size=1,
                resume=True,
                stop_after_batch=True,
                sync_state=state,
                sync_session=master_db,
            )

        master_db.refresh(state)

        self.assertTrue(first["ok"])
        self.assertEqual(first["saved"], 1)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["last_uid"], "1")

        self.assertTrue(second["ok"])
        self.assertEqual(second["saved"], 1)
        self.assertFalse(second["has_more"])
        self.assertEqual(second["last_uid"], "2")

        self.assertEqual(
            integrations._imap_search_criteria(
                start_date=None,
                end_date=None,
                unread_only=False,
                start_uid="2",
            ),
            ["UID", "2:*"],
        )

        self.assertEqual(state.backfill_status, "idle")
        self.assertEqual(state.backfill_last_uid, "2")
        self.assertEqual(
            tenant_db.scalar(select(func.count()).select_from(Email)) or 0,
            2,
        )

        tenant_db.close()
        master_db.close()

    def test_backfill_imap_deduplicates_pdf_attachment_on_second_run(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        master_db = self.MasterSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        state = master_db.scalar(
            select(EmailSyncState).where(
                EmailSyncState.company_id == 1,
                EmailSyncState.channel_key == "email",
            )
        )
        email_message = EmailMessage()
        email_message["From"] = "compras@example.com"
        email_message["To"] = "pedidos@example.com"
        email_message["Subject"] = "Pedido PDF histórico"
        email_message["Message-ID"] = "<pedido-pdf-historico@example.com>"
        email_message.set_content("Pedido histórico con PDF")
        email_message.add_attachment(
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF",
            maintype="application",
            subtype="pdf",
            filename="pedido-historico.pdf",
        )
        client = FakeImapClient({"1": email_message.as_bytes(policy=policy.default)})

        with patch("app.settings.integrations._imap_client", return_value=client):
            first = backfill_imap_emails(
                tenant_db,
                settings,
                1,
                from_date="2026-07-01",
                to_date="2026-07-16",
                limit=1,
                sync_state=state,
                sync_session=master_db,
            )
            second = backfill_imap_emails(
                tenant_db,
                settings,
                1,
                from_date="2026-07-01",
                to_date="2026-07-16",
                limit=1,
                sync_state=state,
                sync_session=master_db,
            )

        self.assertTrue(first["ok"])
        self.assertEqual(first["saved"], 1)
        self.assertTrue(second["ok"])
        self.assertEqual(second["saved"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(tenant_db.scalar(select(func.count()).select_from(Email)) or 0, 1)
        self.assertEqual(tenant_db.scalar(select(func.count()).select_from(EmailAttachment)) or 0, 1)
        self.assertEqual(tenant_db.scalar(select(func.count()).select_from(MessageAttachment)) or 0, 1)

        tenant_db.close()
        master_db.close()

    def test_imap_connection_gmail_success_uses_ssl_and_inbox(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        settings.provider = "gmail"
        settings.imap_host = "imap.gmail.com"
        settings.imap_port = 993
        settings.imap_use_ssl = True
        settings.imap_security = "ssl_tls"
        settings.read_unread_only = False
        client = FakeImapClient(
            {
                "1": b"From: compras@example.com\r\nSubject: Pedido 1\r\nMessage-ID: <pedido-1@example.com>\r\n\r\nPedido 1",
            },
            search_result=b"1",
        )
        with patch("app.settings.integrations.imaplib.IMAP4_SSL", return_value=client) as ssl_mock:
            result = integrations.test_imap_connection(settings)

        self.assertTrue(result["ok"])
        ssl_mock.assert_called_once()
        self.assertEqual(client.login_calls[0][0], "demo@example.com")
        self.assertTrue(client.select_calls[0][1]["readonly"])
        self.assertEqual(client.uid_calls, [])
        tenant_db.close()

    def test_imap_connection_rejects_invalid_encrypted_password(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        settings.imap_password_encrypted = "not-a-fernet-token"

        result = integrations.test_imap_connection(settings)

        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "La contraseña guardada no se ha podido descifrar.")
        tenant_db.close()

    def test_imap_connection_handles_timeout_and_ssl_errors(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        settings.provider = "gmail"
        settings.imap_host = "imap.gmail.com"
        settings.imap_port = 993
        settings.imap_use_ssl = True
        settings.imap_security = "ssl_tls"

        with patch("app.settings.integrations.imaplib.IMAP4_SSL", side_effect=TimeoutError("timed out")):
            timeout_result = integrations.test_imap_connection(settings)
        with patch("app.settings.integrations.imaplib.IMAP4_SSL", side_effect=ssl.SSLError("bad handshake")):
            ssl_result = integrations.test_imap_connection(settings)
        with patch("app.settings.integrations.imaplib.IMAP4_SSL", side_effect=imaplib.IMAP4.error("authentication failed")):
            auth_result = integrations.test_imap_connection(settings)

        self.assertFalse(timeout_result["ok"])
        self.assertEqual(timeout_result["message"], "No se ha podido conectar con imap.gmail.com:993.")
        self.assertFalse(ssl_result["ok"])
        self.assertEqual(ssl_result["message"], "La configuración SSL/TLS no es válida.")
        self.assertFalse(auth_result["ok"])
        self.assertIn("Google ha rechazado la autenticación", auth_result["message"])
        tenant_db.close()

    def test_imap_connection_detects_incomplete_configuration(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        settings.imap_username = ""

        result = integrations.test_imap_connection(settings)

        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "La configuración IMAP está incompleta.")
        tenant_db.close()

    def test_email_without_pdf_uses_body_text_and_keeps_attachment_count_zero(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        tenant_db.add(LLMSettings(company_id=1, api_key_encrypted=encrypt_secret("dummy-key")))
        tenant_db.commit()
        email = Email(
            company_id=1,
            external_id="mail-no-pdf-1",
            sender="compras@example.com",
            subject="Pedido sin PDF",
            body="Necesitamos 5 unidades de P-100 sin PDF.",
        )
        tenant_db.add(email)
        tenant_db.commit()

        channel = get_or_create_channel(tenant_db, 1, "email")
        channel.is_active = True
        tenant_db.commit()

        captured = {"classification_text": None, "extraction_text": None}
        classification = '{"tipo_correo":"pedido","confianza":0.96,"motivo":"Pedido claro"}'
        extraction = '{"cliente":{"nombre_detectado":"Cliente Demo SL","codigo_cliente_detectado":"C001"},"pedido":{"lineas":[{"texto_original":"5 unidades de P-100","referencia_detectada":"P-100","producto_detectado":"Producto Demo","cantidad":5,"unidad":"uds","confianza_extraccion":0.93}]}}'

        def fake_classify(_db, _settings, _company_id, text, _prompt):  # noqa: ANN001
            captured["classification_text"] = text
            return {"ok": True, "content": classification}

        def fake_extract(_db, _settings, _company_id, text, _prompt):  # noqa: ANN001
            captured["extraction_text"] = text
            return {"ok": True, "content": extraction}

        with patch("app.agent.platform.classify_sample", side_effect=fake_classify), patch(
            "app.agent.platform.extract_sample",
            side_effect=fake_extract,
        ):
            result = AgentProcessingService().process_email(tenant_db, email)

        self.assertTrue(result["ok"])
        self.assertIn("Necesitamos 5 unidades de P-100 sin PDF.", captured["classification_text"] or "")
        self.assertIn("Necesitamos 5 unidades de P-100 sin PDF.", captured["extraction_text"] or "")
        refreshed = tenant_db.get(Email, email.id)
        self.assertIsNotNone(refreshed)
        self.assertFalse(refreshed.has_pdf)
        self.assertEqual(tenant_db.scalar(select(func.count()).select_from(EmailAttachment)) or 0, 0)
        tenant_db.close()

    def test_initial_imap_preview_and_sync_seed_last_seen_uid_without_history(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        master_db = self.MasterSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == 1, EmailSyncState.channel_key == "email"))
        state.uidvalidity = "777"
        messages = {
            "1": b"From: compras@example.com\r\nSubject: Pedido A\r\nMessage-ID: <pedido-a@example.com>\r\n\r\nPedido 1",
            "2": b"From: compras@example.com\r\nSubject: Pedido B\r\nMessage-ID: <pedido-b@example.com>\r\n\r\nPedido 2",
            "3": b"From: compras@example.com\r\nSubject: Pedido C\r\nMessage-ID: <pedido-c@example.com>\r\n\r\nPedido 3",
        }
        settings.initial_history_mode = "new"
        settings.initial_history_limit = 50
        client = FakeImapClient(messages, search_result=b"1 2 3")
        with patch("app.settings.integrations._imap_client", return_value=client):
            preview = preview_initial_imap_sync(settings)
            result = run_initial_imap_sync(tenant_db, settings, 1, sync_state=state, sync_session=master_db)

        master_db.refresh(state)
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["estimated"], 0)
        self.assertEqual(preview["checkpoint_uid"], "3")
        self.assertEqual(preview["uidvalidity"], "777")
        self.assertEqual(preview["message"], "Se guardará el punto de partida actual y no se importará histórico.")
        self.assertTrue(client.uid_calls)
        self.assertEqual(client.uid_calls[0][0], "search")
        self.assertTrue(result["ok"])
        self.assertEqual(result["saved"], 0)
        self.assertEqual(master_db.get(EmailSyncState, state.id).last_seen_uid, "3")
        self.assertEqual(tenant_db.scalar(select(func.count()).select_from(Email)) or 0, 0)
        tenant_db.close()
        master_db.close()

    def test_initial_imap_preview_limits_and_caps_history(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        messages = {
            str(index): (
                b"From: compras@example.com\r\n"
                b"Subject: Pedido "
                + str(index).encode()
                + b"\r\n"
                + f"Message-ID: <pedido-{index}@example.com>\r\n\r\n".encode()
                + f"Pedido {index}".encode()
            )
            for index in range(1, 151)
        }
        settings.initial_history_mode = "100"
        settings.initial_history_limit = 150
        with patch("app.settings.integrations._imap_client", return_value=FakeImapClient(messages, search_result=b" ".join(str(index).encode() for index in range(1, 151)))):
            preview = preview_initial_imap_sync(settings)

        self.assertTrue(preview["ok"])
        self.assertEqual(preview["estimated"], 100)
        self.assertIsNotNone(preview["warning"])
        tenant_db.close()

    def test_incremental_sync_starts_after_last_seen_uid(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        master_db = self.MasterSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == 1, EmailSyncState.channel_key == "email"))
        state.uidvalidity = "777"
        state.last_seen_uid = "2"
        master_db.commit()
        client = FakeImapClient(
            {
                "3": b"From: compras@example.com\r\nSubject: Pedido C\r\nMessage-ID: <pedido-c@example.com>\r\n\r\nPedido 3",
            },
            search_result=b"3",
        )

        with patch("app.settings.integrations._imap_client", return_value=client):
            result = read_latest_imap_emails(tenant_db, settings, 1, auto_process=False, unread_only=False, sync_state=state, sync_session=master_db)

        self.assertTrue(result["ok"])
        self.assertFalse(client.search_calls)
        self.assertTrue(client.uid_calls)
        self.assertEqual(client.uid_calls[0][0], "search")
        self.assertIn("3:*", client.uid_calls[0])
        self.assertEqual(result["found"], 1)
        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(result["saved"], 1)
        self.assertEqual(result["last_seen_uid_after"], "3")
        tenant_db.close()
        master_db.close()

    def test_incremental_sync_skips_one_bad_message_and_imports_the_rest(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        master_db = self.MasterSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == 1, EmailSyncState.channel_key == "email"))
        state.uidvalidity = "777"
        state.last_seen_uid = "1"
        master_db.commit()
        messages = {
            "2": b"From: compras@example.com\r\nSubject: Pedido malo\r\nMessage-ID: <pedido-malo@example.com>\r\n\r\nPedido malo",
            "3": b"From: compras@example.com\r\nSubject: Pedido bueno\r\nMessage-ID: <pedido-bueno@example.com>\r\n\r\nPedido bueno",
        }
        client = FakeImapClient(messages, search_result=b"2 3")

        original_extract_body = integrations._extract_body

        def fake_extract_body(msg):  # noqa: ANN001
            subject = msg.get("Subject", "")
            if "malo" in subject:
                raise ValueError("bad body")
            return original_extract_body(msg)

        with patch("app.settings.integrations._imap_client", return_value=client), patch("app.settings.integrations._extract_body", side_effect=fake_extract_body):
            result = read_latest_imap_emails(tenant_db, settings, 1, auto_process=False, unread_only=False, sync_state=state, sync_session=master_db)

        master_db.refresh(state)
        self.assertTrue(result["ok"])
        self.assertEqual(result["found"], 2)
        self.assertEqual(result["saved"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(master_db.get(EmailSyncState, state.id).last_seen_uid, "3")
        self.assertEqual(tenant_db.scalar(select(func.count()).select_from(Email)) or 0, 1)
        tenant_db.close()
        master_db.close()

    def test_incremental_sync_resets_checkpoint_when_uidvalidity_changes(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        master_db = self.MasterSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == 1, EmailSyncState.channel_key == "email"))
        state.uidvalidity = "111"
        state.last_seen_uid = "2"
        master_db.commit()
        client = FakeImapClient(
            {
                "3": b"From: compras@example.com\r\nSubject: Pedido nuevo\r\nMessage-ID: <pedido-nuevo@example.com>\r\n\r\nPedido nuevo",
            },
            search_result=b"3",
        )

        with patch("app.settings.integrations._imap_client", return_value=client):
            result = read_latest_imap_emails(tenant_db, settings, 1, auto_process=False, unread_only=False, sync_state=state, sync_session=master_db)

        master_db.refresh(state)
        self.assertTrue(result["ok"])
        self.assertEqual(result["found"], 0)
        self.assertEqual(result["saved"], 0)
        self.assertEqual(result["last_seen_uid_after"], "3")
        self.assertEqual(master_db.get(EmailSyncState, state.id).uidvalidity, "777")
        self.assertEqual(master_db.get(EmailSyncState, state.id).last_seen_uid, "3")
        self.assertEqual(tenant_db.scalar(select(func.count()).select_from(Email)) or 0, 0)
        self.assertTrue(client.uid_calls)
        self.assertEqual(client.uid_calls[0][0], "search")
        self.assertIn("ALL", client.uid_calls[0])
        tenant_db.close()
        master_db.close()

    def test_prompt_validation_rejects_non_json(self):
        validation = validate_prompt_output("classification", "pedido")
        self.assertFalse(validation.ok)
        self.assertEqual(validation.status, "invalid_json")

    def test_evaluation_fixture_compares_expected_and_actual(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "agent_evaluation.json"
        summary = run_evaluation(fixture)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["exact_matches"], 3)
        self.assertEqual(summary["accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
