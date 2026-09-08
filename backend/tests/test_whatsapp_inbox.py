from __future__ import annotations

import os
import unittest
import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.core import lifespan as lifespan_module  # noqa: E402
from app.core.app_factory import create_app  # noqa: E402
from app.db.models import ChannelSetting, InboundMessage, InputChannel, utcnow  # noqa: E402
from app.messages.service import upsert_inbound_message  # noqa: E402
from app.whatsapp.service import send_whatsapp_media  # noqa: E402
from scripts.performance_data import build_performance_fixture, temporary_performance_environment  # noqa: E402


class WhatsAppInboxTests(unittest.TestCase):
    def _client_for(self, fixture):
        context = temporary_performance_environment(fixture)
        context.__enter__()
        app = create_app()
        patches = (
            patch.object(lifespan_module, "start_email_sync_worker", lambda: None),
            patch.object(lifespan_module, "start_job_worker", lambda: None),
        )
        for item in patches:
            item.__enter__()
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()

        def cleanup():
            client.__exit__(None, None, None)
            for item in reversed(patches):
                item.__exit__(None, None, None)
            context.__exit__(None, None, None)

        return client, cleanup

    def _seed_whatsapp(self, fixture, *, active: bool = True, configured: bool = True) -> int:
        engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        with session() as db:
            channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))
            if channel is None:
                channel = InputChannel(
                    company_id=1,
                    key="whatsapp",
                    name="WhatsApp",
                    channel_type="message",
                    is_active=active,
                    supports_text=True,
                    supports_attachments=True,
                    supports_audio=True,
                    supports_documents=True,
                    supports_images=True,
                )
                db.add(channel)
                db.flush()
            else:
                channel.is_active = active
            if configured:
                values = {
                    "enabled": "true",
                    "provider": "meta",
                    "phone_number_id": "phone-test",
                    "business_account_id": "business-test",
                    "access_token": "token-test",
                    "verify_token": "verify-test",
                    "connection_status": "connected",
                    "webhook_enabled": "true",
                }
                for key, value in values.items():
                    setting = db.scalar(
                        select(ChannelSetting).where(
                            ChannelSetting.company_id == 1,
                            ChannelSetting.channel_id == channel.id,
                            ChannelSetting.key == key,
                        )
                    )
                    if setting is None:
                        setting = ChannelSetting(company_id=1, channel_id=channel.id, key=key)
                        db.add(setting)
                    setting.value = value
            db.flush()
            message, conversation = upsert_inbound_message(
                db,
                company_id=1,
                channel_key="whatsapp",
                provider="meta",
                external_id="wamid.inbox-test",
                sender="+34600000000",
                recipients=["+34910000000"],
                subject="Consulta de WhatsApp",
                text_content="Necesitamos confirmar la entrega.",
                external_thread_id="+34600000000",
                received_at=utcnow() - timedelta(minutes=2),
                content_type="text",
            )
            message.status = "received"
            db.commit()
            conversation_id = conversation.id
        engine.dispose()
        return conversation_id

    def _login(self, client, fixture):
        return client.post(
            "/login",
            data={"email": fixture.admin_email, "password": fixture.admin_password, "next": "/whatsapp/inbox"},
            follow_redirects=False,
        )

    def test_inbox_lists_only_active_whatsapp_conversations_and_renders_split_chat(self):
        fixture = build_performance_fixture("small")
        conversation_id = self._seed_whatsapp(fixture)
        client, cleanup = self._client_for(fixture)
        try:
            self._login(client, fixture)
            response = client.get(f"/whatsapp/inbox?conversation_id={conversation_id}")
        finally:
            cleanup()
            fixture.cleanup()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Buzón de WhatsApp", response.text)
        self.assertIn("Contactos", response.text)
        self.assertIn("Necesitamos confirmar la entrega.", response.text)
        self.assertIn('action="/whatsapp/inbox"', response.text)
        self.assertIn("Dashboard", response.text)
        self.assertIn('name="files"', response.text)
        self.assertEqual(response.text.count('href="/history"'), 1)
        self.assertEqual(response.text.count('class="nav-label">WhatsApp</span>'), 0)

    def test_inactive_channel_is_not_available(self):
        fixture = build_performance_fixture("small")
        self._seed_whatsapp(fixture, active=False, configured=False)
        client, cleanup = self._client_for(fixture)
        try:
            self._login(client, fixture)
            response = client.get("/whatsapp/inbox")
        finally:
            cleanup()
            fixture.cleanup()
        self.assertEqual(response.status_code, 404)

    def test_reply_accepts_supported_attachment_and_delegates_to_whatsapp_service(self):
        fixture = build_performance_fixture("small")
        conversation_id = self._seed_whatsapp(fixture)
        client, cleanup = self._client_for(fixture)
        try:
            self._login(client, fixture)
            with patch(
                "app.whatsapp.inbox_routes.send_manual_response",
                new=AsyncMock(return_value=InboundMessage(id=99)),
            ) as send_response:
                response = client.post(
                    f"/whatsapp/inbox/{conversation_id}/reply",
                    data={"body": "Adjunto la confirmación."},
                    files={"files": ("confirmacion.pdf", b"%PDF-1.4 demo", "application/pdf")},
                    follow_redirects=False,
                )
        finally:
            cleanup()
            fixture.cleanup()

        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=sent", response.headers["location"])
        self.assertTrue(send_response.await_count == 1)
        payload = send_response.await_args.kwargs["attachments"]
        self.assertEqual(payload[0]["filename"], "confirmacion.pdf")
        self.assertEqual(payload[0]["content_type"], "application/pdf")

    def test_media_send_uploads_file_then_sends_document_message(self):
        fixture = build_performance_fixture("small")
        conversation_id = self._seed_whatsapp(fixture)
        engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        with session() as db:
            def handler(request):
                if request.url.path.endswith("/media"):
                    self.assertIn("multipart/form-data", request.headers.get("content-type", ""))
                    return httpx.Response(200, json={"id": "media-test-1"})
                self.assertTrue(request.url.path.endswith("/messages"))
                payload = json.loads(request.content)
                self.assertEqual(payload["type"], "document")
                self.assertEqual(payload["document"]["id"], "media-test-1")
                return httpx.Response(200, json={"messages": [{"id": "wamid.document-1"}]})

            async def run_send():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    return await send_whatsapp_media(
                        db,
                        company_id=1,
                        conversation_id=conversation_id,
                        content=b"contenido de prueba",
                        filename="pedido.docx",
                        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        client=client,
                    )

            result = asyncio.run(run_send())
        engine.dispose()
        fixture.cleanup()
        self.assertEqual(result["provider_message_id"], "wamid.document-1")


if __name__ == "__main__":
    unittest.main()
