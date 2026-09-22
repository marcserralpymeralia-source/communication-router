from __future__ import annotations

import os
import unittest
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

from app.db.models import Email, EmailSettings  # noqa: E402
from app.master.models import EmailSyncState  # noqa: E402
from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


def _tenant_session(tenant_path: Path):
    engine = create_engine(f"sqlite:///{tenant_path.as_posix()}", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class MailInboxRoutesTests(unittest.TestCase):
    def test_mail_routes_are_registered(self):
        from app.core.app_factory import create_app

        def _collect_routes(app_or_router, prefix=""):
            collected = set()
            for route in getattr(app_or_router, "routes", []):
                if hasattr(route, "path") and hasattr(route, "methods"):
                    collected.add((prefix + route.path, tuple(sorted(route.methods or []))))
                elif hasattr(route, "original_router"):
                    p = getattr(route.include_context, "prefix", "") or ""
                    collected.update(_collect_routes(route.original_router, prefix=prefix + p))
            return collected

        routes = _collect_routes(create_app())
        expected = {
            ("/mail", ("GET",)),
            ("/mail/{email_id}", ("GET",)),
            ("/mail/bulk-action", ("POST",)),
            ("/mail/{email_id}/favorite", ("POST",)),
            ("/mail/{email_id}/process", ("POST",)),
            ("/cron/email-sync", ("GET", "POST")),
            ("/cron/jobs", ("GET", "POST")),
        }
        for item in expected:
            self.assertIn(item, routes)

    def test_jobs_cron_requires_auth_and_runs_single_job(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                unauthorized = client.get("/cron/jobs")

                with patch(
                    "app.cron.routes.run_worker_cycle",
                    return_value={
                        "tenants": 1,
                        "recovered": 0,
                        "attempted": 1,
                        "processed": 1,
                        "blocked": 0,
                    },
                ) as worker:
                    authorized = client.get(
                        "/cron/jobs",
                        headers={"x-vercel-cron": "1"},
                    )

            self.assertEqual(unauthorized.status_code, 403)
            self.assertEqual(authorized.status_code, 200)

            payload = authorized.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["attempted"], 1)
            self.assertEqual(payload["processed"], 1)

            worker.assert_called_once_with(max_jobs=1)
        finally:
            fixture.cleanup()

    def test_jobs_cron_accepts_vercel_bearer_secret(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client, patch(
                "app.cron.routes.get_settings",
                return_value=SimpleNamespace(cron_secret="cron-test-secret"),
            ), patch(
                "app.cron.routes.run_worker_cycle",
                return_value={
                    "tenants": 1,
                    "recovered": 0,
                    "attempted": 1,
                    "processed": 1,
                    "blocked": 0,
                },
            ) as worker:
                response = client.get(
                    "/cron/jobs",
                    headers={"authorization": "Bearer cron-test-secret"},
                )

            self.assertEqual(response.status_code, 200)
            worker.assert_called_once_with(max_jobs=1)
        finally:
            fixture.cleanup()

    def test_mail_inbox_page_and_detail_are_available(self):
        fixture = build_performance_fixture("small")
        SessionLocal = _tenant_session(fixture.tenant_path)
        try:
            with SessionLocal() as db:
                email_id = db.scalar(select(Email.id).where(Email.company_id == fixture.company_id).order_by(Email.id))
            self.assertIsNotNone(email_id)

            with performance_test_client(fixture) as client:
                inbox = client.get("/mail", follow_redirects=False)
                detail = client.get(f"/mail/{email_id}")
                dashboard_fragment = client.get("/?partial=workbench", headers={"X-Requested-With": "fetch"})

            self.assertEqual(inbox.status_code, 303)
            self.assertEqual(inbox.headers["location"], "/")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("Detalle de correo", detail.text)
            self.assertNotIn("Internal Server Error", detail.text)
            self.assertEqual(dashboard_fragment.status_code, 200)
            self.assertIn('class="workbench-shell', dashboard_fragment.text)
            self.assertNotIn("<!doctype html>", dashboard_fragment.text.lower())
        finally:
            fixture.cleanup()

    def test_mail_process_runs_job_inline_and_redirects_back(self):
        fixture = build_performance_fixture("small")
        SessionLocal = _tenant_session(fixture.tenant_path)
        try:
            with SessionLocal() as db:
                email_id = db.scalar(select(Email.id).where(Email.company_id == fixture.company_id).order_by(Email.id))
            self.assertIsNotNone(email_id)

            queued_job = SimpleNamespace(id=987)
            with performance_test_client(fixture) as client:
                with patch("app.mail.routes.queue_email_processing", return_value=queued_job) as queue_mock, patch(
                    "app.mail.routes.execute_job_inline",
                    return_value={"ok": True, "message": "Procesado inmediatamente"},
                ) as inline_mock:
                    response = client.post(
                        f"/mail/{email_id}/process",
                        headers={"referer": "/mail"},
                        follow_redirects=False,
                    )

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/mail")
            queue_mock.assert_called_once_with(
                unittest.mock.ANY,
                company_id=fixture.company_id,
                user_id=unittest.mock.ANY,
                email_id=email_id,
            )
            inline_mock.assert_called_once_with(unittest.mock.ANY, queued_job)
        finally:
            fixture.cleanup()

    def test_mail_read_favorite_and_bulk_actions_update_selected_emails(self):
        fixture = build_performance_fixture("small")
        SessionLocal = _tenant_session(fixture.tenant_path)
        try:
            with SessionLocal() as db:
                email_ids = db.scalars(
                    select(Email.id)
                    .where(Email.company_id == fixture.company_id)
                    .order_by(Email.id)
                    .limit(2)
                ).all()
            self.assertEqual(len(email_ids), 2)

            with performance_test_client(fixture) as client:
                favorite = client.post(
                    f"/mail/{email_ids[0]}/favorite",
                    headers={"referer": "/history"},
                    follow_redirects=False,
                )
                favorite_async = client.post(
                    f"/mail/{email_ids[0]}/favorite",
                    headers={"X-Requested-With": "fetch", "Accept": "application/json"},
                )
                pane = client.get(f"/history/pane/email/{email_ids[0]}")
                bulk_read = client.post(
                    "/mail/bulk-action",
                    data={"action": "mark_read", "email_ids": [str(email_ids[0]), str(email_ids[1])]},
                    headers={"referer": "/history"},
                    follow_redirects=False,
                )
                bulk_archive = client.post(
                    "/mail/bulk-action",
                    data={"action": "archive", "email_ids": [str(email_ids[0]), str(email_ids[1])]},
                    headers={"referer": "/history"},
                    follow_redirects=False,
                )
                fragment = client.get(
                    "/history?partial=list",
                    headers={"X-Requested-With": "fetch", "Accept": "text/html"},
                )

            self.assertEqual(favorite.status_code, 303)
            self.assertEqual(favorite_async.status_code, 200)
            self.assertFalse(favorite_async.json()["is_favorite"])
            self.assertEqual(pane.status_code, 200)
            self.assertEqual(bulk_read.status_code, 303)
            self.assertEqual(bulk_archive.status_code, 303)
            self.assertEqual(fragment.status_code, 200)
            self.assertIn('class="webmail-list-pane"', fragment.text)
            self.assertNotIn("<!doctype html>", fragment.text.lower())

            with SessionLocal() as db:
                first, second = [db.get(Email, email_id) for email_id in email_ids]
                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                assert first is not None and second is not None
                self.assertFalse(first.is_favorite)
                self.assertTrue(first.is_read)
                self.assertTrue(second.is_read)
                self.assertTrue(first.archived)
                self.assertTrue(second.archived)
        finally:
            fixture.cleanup()

    def test_mail_favorite_allows_historical_email_outside_active_mail_scope(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(fixture.master_database_url, connect_args={"check_same_thread": False})
        tenant_engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            with MasterSession() as master_db:
                state = master_db.scalar(
                    select(EmailSyncState).where(
                        EmailSyncState.company_id == fixture.company_id,
                        EmailSyncState.channel_key == "email",
                    )
                )
                self.assertIsNotNone(state)
                assert state is not None
                state.mailbox = "INBOX"
                state.uidvalidity = "999"
                master_db.commit()

            with TenantSession() as tenant_db:
                email = tenant_db.scalar(
                    select(Email)
                    .where(Email.company_id == fixture.company_id)
                    .order_by(Email.id)
                )
                self.assertIsNotNone(email)
                assert email is not None
                email_id = email.id
                self.assertNotEqual(email.imap_mailbox, "INBOX")
                self.assertNotEqual(email.imap_uidvalidity, "999")

            with performance_test_client(fixture) as client:
                response = client.post(
                    f"/mail/{email_id}/favorite",
                    headers={"X-Requested-With": "fetch", "Accept": "application/json"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"ok": True, "email_id": email_id, "is_favorite": True})

            with TenantSession() as tenant_db:
                saved = tenant_db.get(Email, email_id)
                self.assertIsNotNone(saved)
                assert saved is not None
                self.assertTrue(saved.is_favorite)
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_mail_inbox_defaults_to_ten_and_filters_active_account(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(fixture.master_database_url, connect_args={"check_same_thread": False})
        tenant_engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            with MasterSession() as master_db:
                state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == fixture.company_id, EmailSyncState.channel_key == "email"))
                self.assertIsNotNone(state)
                assert state is not None
                state.mailbox = "INBOX"
                state.uidvalidity = "777"
                master_db.commit()

            with TenantSession() as tenant_db:
                for index in range(12):
                    tenant_db.add(
                        Email(
                            company_id=fixture.company_id,
                            external_id=f"active-{index}",
                            message_id=f"<active-{index}@example.com>",
                            imap_mailbox="INBOX",
                            imap_uidvalidity="777",
                            imap_uid=str(200 + index),
                            sender="nuevo@example.com",
                            subject=f"Activo {index}",
                            body="Pedido activo",
                            received_at=datetime(2026, 7, 31, 12, index, tzinfo=timezone.utc),
                        )
                    )
                for index in range(2):
                    tenant_db.add(
                        Email(
                            company_id=fixture.company_id,
                            external_id=f"legacy-{index}",
                            message_id=f"<legacy-{index}@example.com>",
                            imap_mailbox="INBOX",
                            imap_uidvalidity="111",
                            imap_uid=str(50 + index),
                            sender="legacy@example.com",
                            subject=f"Antiguo {index}",
                            body="Pedido antiguo",
                            received_at=datetime(2026, 7, 30, 12, index, tzinfo=timezone.utc),
                        )
                    )
                tenant_db.commit()

            with performance_test_client(fixture) as client:
                inbox = client.get("/mail", follow_redirects=True)

            self.assertEqual(inbox.status_code, 200)
            self.assertIn("Bandeja", inbox.text)
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_mail_inbox_redirects_to_dashboard(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/mail", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/")
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
