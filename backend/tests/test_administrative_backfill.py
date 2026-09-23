from __future__ import annotations

import asyncio
from argparse import Namespace
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Company, Mailbox
from app.master.service import TenantRole, TenantUser
from app.mailboxes.routes import administrative_backfill_once
from scripts.run_mailbox_backfill_once import validate_backfill_in_context, validate_mailbox_safety


class JsonRequest:
    def __init__(self, payload: dict[str, str]):
        self.headers = {"content-type": "application/json", "accept": "application/json"}
        self.payload = payload

    async def json(self):
        return self.payload


class AdministrativeBackfillTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.settings = SimpleNamespace(environment="production", app_slug="kibak", enable_production_backfill_admin=True)

    def tearDown(self):
        self.engine.dispose()

    def _fixture(self, role: str = "Administrador", company_slug: str = "kibak-pilot"):
        db = self.Session()
        db.add(Company(id=1, name="KIBAK Pilot"))
        db.add(
            Mailbox(
                id=2,
                company_id=1,
                name="Microsoft piloto",
                email_address="pilot@example.com",
                provider="microsoft365",
                connection_method="oauth2",
                refresh_token_encrypted="test-ciphertext",
                enabled=False,
                auto_sync_enabled=False,
                mark_as_read_after_import=False,
            )
        )
        db.commit()
        user = TenantUser(
            id=1,
            email="admin@example.com",
            name="Admin",
            is_active=True,
            company_id=1,
            company_name="KIBAK Pilot",
            company_slug=company_slug,
            role=TenantRole(role),
            membership_id=1,
            database_url="postgresql://runtime/kibak_tenant_quibac",
        )
        return db, user

    def _result(self):
        return {
            "ok": True,
            "range": {"since": "2026-09-14", "to": "2026-09-21"},
            "backfill": {"found": 52, "saved": 50, "duplicates": 2, "attachments": 3, "errors": 0, "routing_jobs_enqueued": 0},
            "counts_before": {"communications": 0, "attachments": 0, "prompt_executions": 0, "routing_decisions": 0, "routing_actions": 0, "background_jobs": 0},
            "counts_after": {"communications": 50, "attachments": 3, "prompt_executions": 0, "routing_decisions": 0, "routing_actions": 0, "background_jobs": 0},
            "count_deltas": {"communications": 50, "attachments": 3, "prompt_executions": 0, "routing_decisions": 0, "routing_actions": 0, "background_jobs": 0},
            "received_range": {"first_received_at": None, "last_received_at": None},
            "storage": {"db_attachment_present": True, "persistent_storage_ref": True, "object_read_ok": True, "tenant_isolation_ok": True},
            "policy": {"simulation_mode": True, "auto_forwarding_enabled": False},
            "execution": {"background_worker_started": False, "openai_called": False},
        }

    def test_non_admin_is_forbidden(self):
        db, user = self._fixture(role="Supervisor")
        with patch("app.mailboxes.routes.get_settings", return_value=self.settings), patch("app.mailboxes.routes.run_backfill_in_context") as run:
            response = asyncio.run(administrative_backfill_once(2, JsonRequest({"confirmation": "BACKFILL_PRODUCTION_CONFIRM"}), db, object(), user))
        self.assertEqual(response.status_code, 403)
        run.assert_not_called()
        db.close()

    def test_confirmation_is_required_without_calling_service(self):
        db, user = self._fixture()
        with patch("app.mailboxes.routes.get_settings", return_value=self.settings), patch("app.mailboxes.routes.run_backfill_in_context") as run:
            response = asyncio.run(administrative_backfill_once(2, JsonRequest({}), db, object(), user))
        self.assertEqual(response.status_code, 400)
        run.assert_not_called()
        db.close()

    def test_simulation_auto_process_flag_does_not_block_admin_backfill(self):
        db, _ = self._fixture()
        mailbox = db.get(Mailbox, 2)
        mailbox.auto_process_on_fetch = True
        db.commit()
        validate_mailbox_safety(mailbox)
        db.close()

    def test_database_binding_mismatch_still_fails_closed(self):
        db, _ = self._fixture()
        with self.assertRaises(RuntimeError):
            validate_backfill_in_context(
                Namespace(company_slug="kibak-pilot", since="2026-09-14", to=None),
                settings=self.settings,
                master_db=db,
                tenant_db=db,
                company=db.get(Company, 1),
                mailbox=db.get(Mailbox, 2),
                database_name="other_database",
            )
        db.close()

    def test_admin_confirmation_uses_authenticated_context_and_safe_metrics(self):
        db, user = self._fixture()
        with patch("app.mailboxes.routes.get_settings", return_value=self.settings), patch("app.mailboxes.routes.log_action"), patch(
            "app.mailboxes.routes.validate_backfill_in_context", return_value=(date(2026, 9, 14), date(2026, 9, 23))
        ) as validate, patch(
            "app.mailboxes.routes.enqueue_job", return_value=SimpleNamespace(id=101, status="queued")
        ) as enqueue:
            response = asyncio.run(
                administrative_backfill_once(
                    2,
                    JsonRequest({"confirmation": "BACKFILL_PRODUCTION_CONFIRM"}),
                    db,
                    object(),
                    user,
                )
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.body.find(b'"auto_process":false') >= 0)
        self.assertTrue(response.body.find(b'"routing_jobs_enqueued":0') >= 0)
        self.assertTrue(response.body.find(b'"job_id":101') >= 0)
        validate.assert_called_once()
        payload = enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["from_date"], "2026-09-14")
        self.assertEqual(payload["to_date"], "2026-09-23")
        self.assertEqual(payload["batch_size"], 25)
        self.assertTrue(payload["admin_backfill"])
        self.assertFalse(payload["auto_process"])
        db.close()

    def test_feature_flag_is_closed_by_default(self):
        db, user = self._fixture()
        disabled = SimpleNamespace(environment="production", app_slug="kibak", enable_production_backfill_admin=False)
        with patch("app.mailboxes.routes.get_settings", return_value=disabled), patch("app.mailboxes.routes.run_backfill_in_context") as run:
            response = asyncio.run(administrative_backfill_once(2, JsonRequest({"confirmation": "BACKFILL_PRODUCTION_CONFIRM"}), db, object(), user))
        self.assertEqual(response.status_code, 404)
        run.assert_not_called()
        db.close()

    def test_other_tenant_cannot_use_pilot_action(self):
        db, user = self._fixture(company_slug="empresa-demo")
        with patch("app.mailboxes.routes.get_settings", return_value=self.settings), patch("app.mailboxes.routes.run_backfill_in_context") as run:
            response = asyncio.run(administrative_backfill_once(2, JsonRequest({"confirmation": "BACKFILL_PRODUCTION_CONFIRM"}), db, object(), user))
        self.assertEqual(response.status_code, 403)
        run.assert_not_called()
        db.close()


if __name__ == "__main__":
    unittest.main()
