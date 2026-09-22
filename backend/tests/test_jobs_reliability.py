from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import UploadFile
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "development")

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.services import AgentProcessingService  # noqa: E402
from app.agent.platform import UnifiedOrderPipelineService  # noqa: E402
from app.channels.service import get_or_create_channel  # noqa: E402
from app.agent.extraction.schema import ExtractedCustomer, ExtractedOrderLine, OrderExtraction, OrderExtractionInput, OrderExtractionResult  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import BackgroundJob, Customer, Email, EmailSettings, ImportJob, InputChannel, InboundMessage, JobAttempt, LLMSettings, Mailbox, Order, OrderLine, Product  # noqa: E402
from app.imports.service import create_preview  # noqa: E402
from app.jobs.service import claim_next_job, enqueue_job, execute_job_inline, fail_job, finish_job, recover_stale_jobs, retry_job, get_job  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.core.encryption import encrypt_secret  # noqa: E402
from app.tenancy.migrations import upgrade_tenant_schema  # noqa: E402
from app.workers.jobs_worker import _process_import_job, _process_job, run_worker_cycle  # noqa: E402
from app.core.security import hash_password  # noqa: E402


class JobsReliabilityTests(unittest.TestCase):
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

    def _seed_master(self):
        db = self.MasterSession()
        company = MasterCompany(id=1, name="Demo", slug="demo", active=True)
        user = MasterUser(id=1, email="admin@anchi.local", full_name="Admin Demo", password_hash=hash_password("admin123"), is_active=True)
        membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True)
        tenant_db = MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok")
        db.add_all([company, user, membership, tenant_db])
        db.commit()
        db.close()

    def _seed_llm(self):
        db = self.TenantSession()
        db.add(
            LLMSettings(
                company_id=1,
                api_key_encrypted=encrypt_secret("test-token"),
                agent_enabled=True,
                can_extract_order=True,
                can_classify_email=True,
                can_calculate_score=True,
            )
        )
        db.commit()
        db.close()

    def _seed_fast_path_message(self, *, body: str, subject: str = "Pedido directo", sender: str = "compras@example.com"):
        self._seed_master()
        self._seed_llm()
        upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")

        db = self.TenantSession()
        db.add(Product(company_id=1, reference="P-100", name="Producto demo"))
        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        message = InboundMessage(
            company_id=1,
            channel_id=channel.id,
            provider="imap",
            source_external_id="mail-fast-path",
            sender=sender,
            subject=subject,
            original_content=body,
            content_type="email",
        )
        db.add(message)
        db.commit()
        return db, message

    def test_email_sync_job_skips_when_email_channel_is_disabled(self):
        db = self.TenantSession()
        try:
            db.add(
                InputChannel(
                    company_id=1,
                    key="email",
                    name="Email",
                    channel_type="message",
                    is_active=False,
                    is_default=True,
                    supports_text=True,
                    supports_attachments=True,
                    supports_documents=True,
                    supports_audio=False,
                    supports_images=False,
                )
            )
            db.add(
                EmailSettings(
                    company_id=1,
                    auto_sync_enabled=True,
                )
            )
            db.commit()

            job = enqueue_job(
                db,
                company_id=1,
                job_type="email_sync",
                payload={"auto_process": False},
                created_by_user_id=None,
            )

            with patch(
                "app.workers.jobs_worker.read_latest_imap_emails"
            ) as read_imap:
                result = _process_job(db, job)

            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("skipped"))
            self.assertIn("desactivado", result.get("message", "").lower())
            read_imap.assert_not_called()
        finally:
            db.close()

    def test_backfill_job_enqueues_single_continuation_with_remaining_limit(self):
        self._seed_master()

        db = self.TenantSession()
        try:
            db.add(
                InputChannel(
                    company_id=1,
                    key="email",
                    name="Email",
                    channel_type="message",
                    is_active=True,
                    is_default=True,
                    supports_text=True,
                    supports_attachments=True,
                    supports_documents=True,
                    supports_audio=False,
                    supports_images=False,
                )
            )
            db.add(
                EmailSettings(
                    company_id=1,
                    auto_sync_enabled=True,
                )
            )
            db.commit()

            job = enqueue_job(
                db,
                company_id=1,
                job_type="backfill_imap",
                payload={
                    "from_date": "2026-08-20",
                    "to_date": None,
                    "limit": 7,
                    "batch_size": 1,
                },
                created_by_user_id=None,
            )

            with patch(
                "app.workers.jobs_worker.MasterSessionLocal",
                new=self.MasterSession,
            ), patch(
                "app.workers.jobs_worker.backfill_imap_emails",
                return_value={
                    "ok": True,
                    "saved": 1,
                    "duplicates": 0,
                    "has_more": True,
                    "last_uid": "10",
                    "batch_count": 1,
                    "found": 7,
                    "message": "1 correo procesado",
                },
            ) as backfill:
                result = _process_job(db, job)

            self.assertTrue(result["ok"])
            self.assertEqual(result["remaining"], 6)
            self.assertEqual(result["remaining_messages"], 6)
            self.assertEqual(result["remaining_limit"], 6)
            self.assertEqual(result["total_found"], 7)
            self.assertEqual(result["batch_count"], 1)
            self.assertIn("continuation_job_id", result)

            backfill.assert_called_once()
            kwargs = backfill.call_args.kwargs
            self.assertEqual(kwargs["batch_size"], 1)
            self.assertTrue(kwargs["stop_after_batch"])

            jobs = db.scalars(
                select(BackgroundJob)
                .where(
                    BackgroundJob.company_id == 1,
                    BackgroundJob.job_type == "backfill_imap",
                )
                .order_by(BackgroundJob.id)
            ).all()

            self.assertEqual(len(jobs), 2)

            continuation = jobs[1]
            continuation_payload = __import__(
                "app.jobs.service",
                fromlist=["job_payload"],
            ).job_payload(continuation)

            self.assertEqual(continuation_payload["limit"], 6)
            self.assertTrue(continuation_payload["resume"])
            self.assertEqual(continuation_payload["total_found"], 7)
            self.assertEqual(continuation_payload["processed_count"], 1)
            self.assertEqual(continuation_payload["batch_size"], 1)
            self.assertEqual(
                continuation_payload["from_date"],
                "2026-08-20",
            )
            self.assertNotIn("from_uid", continuation_payload)
        finally:
            db.close()

    def test_pilot_backfill_fills_missing_window_for_legacy_job(self):
        self._seed_master()

        db = self.TenantSession()
        try:
            db.add(
                InputChannel(
                    company_id=1,
                    key="email",
                    name="Email",
                    channel_type="message",
                    is_active=True,
                    is_default=True,
                    supports_text=True,
                    supports_attachments=True,
                    supports_documents=True,
                    supports_audio=False,
                    supports_images=False,
                )
            )
            db.add(EmailSettings(company_id=1, auto_sync_enabled=True))
            db.commit()
            job = enqueue_job(
                db,
                company_id=1,
                job_type="backfill_imap",
                payload={"unbounded": True},
                created_by_user_id=None,
            )

            with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
                "app.workers.jobs_worker.get_settings",
                return_value=SimpleNamespace(is_pilot_runtime=True),
            ), patch(
                "app.workers.jobs_worker._pilot_backfill_window",
                return_value=("2026-09-15", "2026-09-22"),
            ), patch(
                "app.workers.jobs_worker.backfill_imap_emails",
                return_value={"ok": True, "has_more": False, "batch_count": 0, "found": 0},
            ) as backfill:
                result = _process_job(db, job)

            self.assertTrue(result["ok"])
            args, kwargs = backfill.call_args
            self.assertEqual(args[3], "2026-09-15")
            self.assertEqual(args[4], "2026-09-22")
            self.assertEqual(kwargs["batch_size"], 1)
        finally:
            db.close()

    def test_backfill_continuation_enqueues_next_continuation_with_accumulated_progress(self):
        self._seed_master()

        db = self.TenantSession()
        try:
            db.add(
                InputChannel(
                    company_id=1,
                    key="email",
                    name="Email",
                    channel_type="message",
                    is_active=True,
                    is_default=True,
                    supports_text=True,
                    supports_attachments=True,
                    supports_documents=True,
                    supports_audio=False,
                    supports_images=False,
                )
            )
            db.add(
                EmailSettings(
                    company_id=1,
                    auto_sync_enabled=True,
                )
            )
            db.commit()

            job = enqueue_job(
                db,
                company_id=1,
                job_type="backfill_imap",
                payload={
                    "from_date": "2026-08-20",
                    "to_date": None,
                    "limit": 7,
                    "batch_size": 1,
                    "total_found": 12,
                    "processed_count": 5,
                    "resume": True,
                },
                created_by_user_id=None,
            )

            with patch(
                "app.workers.jobs_worker.MasterSessionLocal",
                new=self.MasterSession,
            ), patch(
                "app.workers.jobs_worker.backfill_imap_emails",
                return_value={
                    "ok": True,
                    "found": 7,
                    "batch_count": 1,
                    "has_more": True,
                    "message": "1 correo procesado",
                },
            ) as backfill:
                result = _process_job(db, job)

            self.assertTrue(result["ok"])
            self.assertEqual(result["remaining_messages"], 6)
            self.assertEqual(result["remaining_limit"], 6)
            self.assertEqual(result["total_found"], 12)
            self.assertEqual(result["batch_count"], 1)
            self.assertEqual(result["remaining"], 6)
            self.assertIn("continuation_job_id", result)

            backfill.assert_called_once()
            kwargs = backfill.call_args.kwargs
            self.assertEqual(kwargs["batch_size"], 1)
            self.assertTrue(kwargs["stop_after_batch"])
            self.assertTrue(kwargs["resume"])

            jobs = db.scalars(
                select(BackgroundJob)
                .where(
                    BackgroundJob.company_id == 1,
                    BackgroundJob.job_type == "backfill_imap",
                )
                .order_by(BackgroundJob.id)
            ).all()

            self.assertEqual(len(jobs), 2)
            continuation_payload = __import__(
                "app.jobs.service",
                fromlist=["job_payload"],
            ).job_payload(jobs[1])

            self.assertEqual(continuation_payload["limit"], 6)
            self.assertEqual(continuation_payload["total_found"], 12)
            self.assertEqual(continuation_payload["processed_count"], 6)
            self.assertEqual(continuation_payload["batch_size"], 1)
            self.assertTrue(continuation_payload["resume"])
        finally:
            db.close()

    def test_unbounded_backfill_can_use_disabled_mailbox_without_enabling_it(self):
        self._seed_master()

        db = self.TenantSession()
        try:
            db.add(
                InputChannel(
                    company_id=1,
                    key="email",
                    name="Email",
                    channel_type="message",
                    is_active=True,
                    is_default=True,
                    supports_text=True,
                    supports_attachments=True,
                    supports_documents=True,
                    supports_audio=False,
                    supports_images=False,
                )
            )
            mailbox = Mailbox(
                company_id=1,
                name="Pilot",
                email_address="pilot@example.com",
                enabled=False,
                auto_sync_enabled=False,
                mark_as_read_after_import=False,
            )
            db.add(mailbox)
            db.commit()
            db.refresh(mailbox)

            job = enqueue_job(
                db,
                company_id=1,
                job_type="backfill_imap",
                payload={
                    "mailbox_id": mailbox.id,
                    "from_date": "2026-09-14",
                    "to_date": "2026-09-21",
                    "limit": None,
                    "unbounded": True,
                },
                created_by_user_id=None,
            )

            with patch(
                "app.workers.jobs_worker.MasterSessionLocal",
                new=self.MasterSession,
            ), patch(
                "app.workers.jobs_worker.backfill_imap_emails",
                return_value={
                    "ok": True,
                    "saved": 2,
                    "duplicates": 0,
                    "has_more": False,
                    "last_uid": "2",
                    "batch_count": 2,
                    "found": 2,
                },
            ) as backfill:
                result = _process_job(db, job)

            self.assertTrue(result["ok"])
            kwargs = backfill.call_args.kwargs
            self.assertTrue(kwargs["unbounded"])
            self.assertEqual(kwargs["limit"], None)
            self.assertTrue(kwargs["stop_after_batch"])
            self.assertFalse(db.get(Mailbox, mailbox.id).enabled)
        finally:
            db.close()

    def test_enqueue_job_is_idempotent_and_rejects_secrets(self):
        db = self.TenantSession()

        first = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 10}, created_by_user_id=1)
        second = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 10}, created_by_user_id=1)
        self.assertEqual(first.id, second.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob)) or 0, 1)

        db.get(BackgroundJob, first.id).status = "success"
        db.commit()
        third = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 10}, created_by_user_id=1)
        self.assertEqual(third.id, first.id)

        with self.assertRaises(ValueError):
            enqueue_job(db, company_id=1, job_type="process_email", payload={"password": "secret"}, created_by_user_id=1)
        db.close()

    def test_enqueue_job_ignores_missing_created_by_user_fk(self):
        db = self.TenantSession()
        job = enqueue_job(db, company_id=1, job_type="email_sync", payload={"auto_process": False}, created_by_user_id=999)
        self.assertIsNone(job.created_by_user_id)
        self.assertEqual(job.status, "queued")
        db.close()

    def test_claim_finish_and_attempt_history(self):
        db = self.TenantSession()
        job = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 11}, created_by_user_id=1)

        claimed = claim_next_job(db, owner="worker-a", job_types={"process_email"})
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.attempt_count, 1)

        attempts = db.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id)).all()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "running")
        self.assertEqual(attempts[0].worker_id, "worker-a")

        finish_job(db, claimed, {"ok": True, "message": "done"})
        db.refresh(claimed)
        attempts = db.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id)).all()
        self.assertEqual(attempts[0].status, "succeeded")
        self.assertIsNotNone(attempts[0].finished_at)
        self.assertEqual(claimed.status, "success")
        db.close()

    def test_retry_manual_keeps_attempt_history(self):
        db = self.TenantSession()
        job = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 12}, created_by_user_id=1)
        claimed = claim_next_job(db, owner="worker-a", job_types={"process_email"})
        self.assertIsNotNone(claimed)
        fail_job(db, claimed, "timeout", retry=False, error_type="TimeoutError")

        retried = retry_job(db, 1, job.id)
        self.assertIsNotNone(retried)
        self.assertEqual(retried.status, "queued")
        self.assertEqual(retried.retry_count, 1)
        attempts = db.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id)).all()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "failed_permanent")
        db.close()

    def test_retry_manual_can_be_applied_multiple_times_without_duplicate_attempts(self):
        db = self.TenantSession()
        job = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 13}, created_by_user_id=1)

        claimed_first = claim_next_job(db, owner="worker-a", job_types={"process_email"})
        self.assertIsNotNone(claimed_first)
        self.assertEqual(claimed_first.attempt_count, 1)
        fail_job(db, claimed_first, "timeout-1", retry=False, error_type="TimeoutError")
        db.close()

        db = self.TenantSession()
        first_retry = retry_job(db, 1, job.id)
        self.assertIsNotNone(first_retry)
        self.assertEqual(first_retry.retry_count, 1)
        db.close()

        db = self.TenantSession()
        claimed_second = claim_next_job(db, owner="worker-b", job_types={"process_email"})
        self.assertIsNotNone(claimed_second)
        self.assertEqual(claimed_second.attempt_count, 2)
        fail_job(db, claimed_second, "timeout-2", retry=False, error_type="TimeoutError")
        db.close()

        db = self.TenantSession()
        second_retry = retry_job(db, 1, job.id)
        self.assertIsNotNone(second_retry)
        self.assertEqual(second_retry.retry_count, 2)
        db.close()

        db = self.TenantSession()
        claimed_third = claim_next_job(db, owner="worker-c", job_types={"process_email"})
        self.assertIsNotNone(claimed_third)
        self.assertEqual(claimed_third.attempt_count, 3)
        finish_job(db, claimed_third, {"ok": True, "message": "done"})

        job = db.get(BackgroundJob, job.id)
        attempts = db.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id).order_by(JobAttempt.attempt_number)).all()
        self.assertEqual(job.status, "success")
        self.assertEqual(job.retry_count, 2)
        self.assertEqual(len(attempts), 3)
        self.assertEqual([attempt.status for attempt in attempts], ["failed_permanent", "failed_permanent", "succeeded"])
        self.assertEqual([attempt.worker_id for attempt in attempts], ["worker-a", "worker-b", "worker-c"])
        db.close()

    def test_stale_jobs_recover_or_fail_at_limit(self):
        db = self.TenantSession()
        stale_running = BackgroundJob(
            company_id=1,
            job_type="process_email",
            status="running",
            payload_json='{"email_id": 1}',
            lock_owner="old-worker",
            attempt_count=1,
            retry_count=0,
            max_retries=2,
        )
        db.add(stale_running)
        db.flush()
        stale_running.started_at = stale_running.created_at.replace(year=stale_running.created_at.year - 1)
        stale_running.lock_until = stale_running.started_at
        stale_running.last_heartbeat_at = stale_running.started_at
        db.add(JobAttempt(company_id=1, job_id=stale_running.id, attempt_number=1, worker_id="old-worker", status="running", started_at=stale_running.started_at))

        exhausted = BackgroundJob(
            company_id=1,
            job_type="process_email",
            status="running",
            payload_json='{"email_id": 2}',
            lock_owner="old-worker",
            attempt_count=1,
            retry_count=0,
            max_retries=0,
        )
        db.add(exhausted)
        db.flush()
        exhausted.started_at = exhausted.created_at.replace(year=exhausted.created_at.year - 1)
        exhausted.lock_until = exhausted.started_at
        exhausted.last_heartbeat_at = exhausted.started_at
        db.add(JobAttempt(company_id=1, job_id=exhausted.id, attempt_number=1, worker_id="old-worker", status="running", started_at=exhausted.started_at))
        db.commit()

        recovered = recover_stale_jobs(db, owner="worker-b", job_types={"process_email"})
        db.refresh(stale_running)
        db.refresh(exhausted)
        self.assertEqual(len(recovered), 2)
        self.assertEqual(stale_running.status, "retrying")
        self.assertEqual(stale_running.retry_count, 1)
        self.assertIsNotNone(stale_running.next_retry_at)
        self.assertEqual(exhausted.status, "failed")
        self.assertIsNotNone(exhausted.finished_at)
        abandoned_attempt = db.scalar(select(JobAttempt).where(JobAttempt.job_id == stale_running.id))
        self.assertEqual(abandoned_attempt.status, "abandoned")
        db.close()

    def test_execute_inline_does_not_restart_active_running_job(self):
        db = self.TenantSession()
        job = BackgroundJob(
            company_id=1,
            job_type="process_email",
            status="running",
            payload_json='{"email_id": 1}',
            lock_owner="active-worker",
            attempt_count=1,
            retry_count=0,
            max_retries=2,
        )
        db.add(job)
        db.flush()

        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        job.started_at = now
        job.lock_until = now + timedelta(minutes=10)
        job.last_heartbeat_at = now
        db.add(
            JobAttempt(
                company_id=1,
                job_id=job.id,
                attempt_number=1,
                worker_id="active-worker",
                status="running",
                started_at=now,
            )
        )
        db.commit()

        with patch("app.workers.jobs_worker._process_job") as process_job:
            result = execute_job_inline(db, job)

        db.refresh(job)
        attempts = db.scalars(
            select(JobAttempt).where(JobAttempt.job_id == job.id)
        ).all()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "job_already_running")
        self.assertEqual(job.status, "running")
        self.assertEqual(job.attempt_count, 1)
        self.assertEqual(len(attempts), 1)
        process_job.assert_not_called()
        db.close()

    def test_execute_inline_recovers_stale_running_job_before_retry(self):
        db = self.TenantSession()
        job = BackgroundJob(
            company_id=1,
            job_type="process_email",
            status="running",
            payload_json='{"email_id": 1}',
            lock_owner="old-worker",
            attempt_count=1,
            retry_count=0,
            max_retries=2,
        )
        db.add(job)
        db.flush()

        from datetime import datetime, timedelta, timezone

        stale_at = datetime.now(timezone.utc) - timedelta(hours=1)
        job.started_at = stale_at
        job.lock_until = stale_at
        job.last_heartbeat_at = stale_at
        db.add(
            JobAttempt(
                company_id=1,
                job_id=job.id,
                attempt_number=1,
                worker_id="old-worker",
                status="running",
                started_at=stale_at,
            )
        )
        db.commit()

        with patch(
            "app.workers.jobs_worker._process_job",
            return_value={"ok": True, "message": "Procesado"},
        ) as process_job:
            result = execute_job_inline(db, job)

        db.refresh(job)
        attempts = db.scalars(
            select(JobAttempt)
            .where(JobAttempt.job_id == job.id)
            .order_by(JobAttempt.attempt_number)
        ).all()

        self.assertTrue(result["ok"])
        self.assertEqual(job.status, "success")
        self.assertEqual(job.attempt_count, 2)
        self.assertEqual(job.retry_count, 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].status, "abandoned")
        self.assertEqual(attempts[0].error_type, "stale_worker")
        self.assertEqual(attempts[1].status, "succeeded")
        process_job.assert_called_once()
        db.close()

    def test_process_email_retry_is_idempotent(self):
        self._seed_llm()
        db = self.TenantSession()
        email = Email(company_id=1, external_id="mail-1", sender="cliente@example.com", subject="Pedido", body="10 cajas")
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        calls = {"count": 0}

        def fake_process_inbound_message(_self, session, inbound_message, user=None, force_order=False, email=None):  # noqa: ANN001
            calls["count"] += 1
            if inbound_message.order_id:
                return {"ok": True, "status": "order_detected", "message": f"Pedido {inbound_message.order_id} ya habia sido creado.", "order_id": inbound_message.order_id, "score": 91}
            order = Order(company_id=inbound_message.company_id, email_id=email.id if email else None, customer_detected_name="Cliente demo", status="pedido_pendiente_revision", score=91)
            session.add(order)
            session.flush()
            session.add(OrderLine(company_id=inbound_message.company_id, order_id=order.id, original_text="10 cajas", quantity=10, unit="cajas", extraction_confidence=0.95, validation_status="validated"))
            inbound_message.order_id = order.id
            inbound_message.customer_id = None
            inbound_message.status = "order_detected"
            inbound_message.processing_step = "completed"
            inbound_message.score = 91
            inbound_message.last_processed_at = inbound_message.received_at
            session.commit()
            return {"ok": True, "status": "order_detected", "message": f"Pedido {order.id} creado.", "order_id": order.id, "score": 91}

        with patch("app.agent.platform.UnifiedOrderPipelineService.process_inbound_message", new=fake_process_inbound_message):
            result_first = AgentProcessingService().process_email(db, email)
            self.assertTrue(result_first["ok"])
            result_second = AgentProcessingService().process_email(db, email)
            self.assertTrue(result_second["ok"])
            self.assertEqual(result_first["order_id"], result_second["order_id"])
            self.assertEqual(calls["count"], 1)

        db.close()

    def test_import_confirm_retry_is_idempotent(self):
        db = self.TenantSession()
        csv_bytes = b"code,fiscal_name,email\nC001,Cliente Uno,uno@example.com\n"
        upload = UploadFile(filename="customers.csv", file=io.BytesIO(csv_bytes))
        preview = asyncio.run(create_preview(upload, "customers"))
        payload = {
            "token": preview["token"],
            "filename": preview["filename"],
            "entity_type": "customers",
            "encoding": "utf-8",
            "mapping": {"code": "code", "fiscal_name": "fiscal_name", "email": "email"},
            "mode": "update_existing",
            "save_template": False,
            "template_name": "",
        }
        job = enqueue_job(db, company_id=1, job_type="import_confirm", payload=payload, created_by_user_id=1)
        result_first = _process_import_job(db, job, payload)
        self.assertTrue(result_first["ok"])
        customers_after_first = db.scalar(select(func.count()).select_from(Customer)) or 0

        result_second = _process_import_job(db, job, payload)
        self.assertTrue(result_second["ok"])
        self.assertEqual(customers_after_first, db.scalar(select(func.count()).select_from(Customer)) or 0)
        db.close()


    def test_process_email_job_propagates_force_flag(self):
        self._seed_master()
        self._seed_llm()
        upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")

        db = self.TenantSession()
        email = Email(
            company_id=1,
            external_id="mail-force-worker",
            sender="cliente@example.com",
            subject="Pedido",
            body="3 cajas",
        )
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        job = enqueue_job(
            db,
            company_id=1,
            job_type="process_email",
            payload={"email_id": email.id, "force": True},
            created_by_user_id=1,
        )
        db.close()

        captured = {"force_order": None}

        def fake_process_email(_self, session, current_email, user=None, force_order=False):
            captured["force_order"] = force_order
            return {
                "ok": True,
                "status": "order_detected",
                "message": "Procesado",
                "order_id": 1,
                "score": 90,
            }

        with patch(
            "app.workers.jobs_worker.MasterSessionLocal",
            new=self.MasterSession,
        ), patch(
            "app.workers.jobs_worker.AgentProcessingService.process_email_fast",
            new=fake_process_email,
        ):
            summary = run_worker_cycle()

        self.assertEqual(summary["tenants"], 1)
        self.assertTrue(captured["force_order"])

        db = self.TenantSession()
        self.assertEqual(db.get(BackgroundJob, job.id).status, "success")
        db.close()

    def test_process_order_job_forces_reprocessing(self):
        self._seed_master()
        self._seed_llm()
        upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")

        db = self.TenantSession()

        email = Email(
            company_id=1,
            external_id="mail-order-reprocess-worker",
            sender="cliente@example.com",
            subject="Pedido",
            body="3 cajas",
        )
        db.add(email)
        db.flush()

        order = Order(
            company_id=1,
            email_id=email.id,
            customer_detected_name="Cliente demo",
            status="pedido_pendiente_revision",
            score=90,
        )
        db.add(order)
        db.commit()

        order_id = order.id
        job = enqueue_job(
            db,
            company_id=1,
            job_type="process_order",
            payload={"order_id": order_id},
            created_by_user_id=1,
        )
        db.close()

        captured = {"force_order": None}

        def fake_process_email(_self, session, current_email, user=None, force_order=False):
            captured["force_order"] = force_order
            return {
                "ok": True,
                "status": "order_detected",
                "message": "Reprocesado",
                "order_id": order_id,
                "score": 90,
            }

        with patch(
            "app.workers.jobs_worker.MasterSessionLocal",
            new=self.MasterSession,
        ), patch(
            "app.workers.jobs_worker.AgentProcessingService.process_email_fast",
            new=fake_process_email,
        ):
            summary = run_worker_cycle()

        self.assertEqual(summary["tenants"], 1)
        self.assertTrue(captured["force_order"])

        db = self.TenantSession()
        self.assertEqual(db.get(BackgroundJob, job.id).status, "success")
        db.close()

    def test_worker_cycle_processes_job_once(self):
        self._seed_master()
        self._seed_llm()
        upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")
        db = self.TenantSession()
        email = Email(company_id=1, external_id="mail-2", sender="cliente@example.com", subject="Pedido worker", body="3 cajas")
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        job = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": email.id}, created_by_user_id=1)
        db.close()

        def fake_process_email_fast(_self, session, current_email, user=None, force_order=False):  # noqa: ANN001
            order = Order(company_id=current_email.company_id, email_id=current_email.id if current_email else None, customer_detected_name="Cliente worker", status="pedido_pendiente_revision", score=88)
            session.add(order)
            session.flush()
            session.add(OrderLine(company_id=current_email.company_id, order_id=order.id, original_text="3 cajas", quantity=3, unit="cajas", extraction_confidence=0.9, validation_status="validated"))
            session.commit()
            return {"ok": True, "status": "order_detected", "message": f"Pedido {order.id} creado.", "order_id": order.id, "score": 88}

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch("app.workers.jobs_worker.AgentProcessingService.process_email_fast", new=fake_process_email_fast):
            summary = run_worker_cycle()

        self.assertEqual(summary["tenants"], 1)
        self.assertGreaterEqual(summary["processed"], 1)

        db = self.TenantSession()
        processed_job = db.get(BackgroundJob, job.id)
        self.assertEqual(processed_job.status, "success")
        self.assertEqual(db.scalar(select(func.count()).select_from(Order)) or 0, 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id == job.id)) or 0, 1)
        db.close()

    def _run_fast_path_case(
        self,
        *,
        extract_order_mock: Mock,
        expected_status: str,
        expected_ok: bool,
        expected_order_count: int,
        expected_message_contains: str,
    ):
        db, message = self._seed_fast_path_message(
            body="Necesitamos 10 cajas de producto demo.",
        )
        pipeline = UnifiedOrderPipelineService()
        no_call = Mock(side_effect=AssertionError("No se esperaba una llamada legacy/classification en el fast path de correo."))

        with (
            patch("app.agent.platform.extract_order", new=extract_order_mock),
            patch("app.agent.platform.classify_sample", new=no_call),
            patch("app.agent.platform.extract_sample", new=no_call),
            patch("app.agent.platform.retrieve_product_knowledge", new=no_call),
            patch("app.agent.platform.find_product_candidates", new=no_call),
            patch.object(pipeline.decision, "customer_decision", new=no_call),
            patch.object(pipeline.decision, "product_decision", new=no_call),
        ):
            result = pipeline.process_inbound_message(db, message, email_fast_path=True)

        order_count = db.scalar(select(func.count()).select_from(Order)) or 0
        order = db.scalar(select(Order).where(Order.company_id == 1))
        db.refresh(message)

        self.assertEqual(extract_order_mock.call_count, 1)
        self.assertEqual(extract_order_mock.call_args.kwargs["model"], "gpt-5.6-luna")
        self.assertEqual(no_call.call_count, 0)
        self.assertEqual(order_count, expected_order_count)
        self.assertEqual(result["status"], expected_status)
        self.assertEqual(result["ok"], expected_ok)
        self.assertIn(expected_message_contains, result["message"])
        return db, message, order

    def test_email_fast_path_uses_single_structured_extraction_without_fallback(self):
        structured = OrderExtractionResult(
            raw_input=OrderExtractionInput(
                text="Asunto: Pedido directo\nRemitente: compras@example.com\n\nNecesitamos 10 cajas de producto demo.",
                source_type="email",
                source_id="1",
            ),
            extracted_data=OrderExtraction(
                is_order=True,
                customer=ExtractedCustomer(raw_name="Cliente demo", raw_name_source="expressed"),
                lines=[
                    ExtractedOrderLine(
                        raw_text="10 cajas de producto demo",
                        raw_description="Producto demo",
                        raw_description_source="expressed",
                        reference="P-100",
                        reference_source="expressed",
                        quantity=10,
                        quantity_source="expressed",
                        unit="cajas",
                        unit_source="expressed",
                        notes=[],
                        uncertainties=[],
                        requires_review=False,
                    )
                ],
                notes=[],
                uncertainties=[],
                requires_review=False,
            ),
            model="gpt-4.1-mini",
        )
        extract_mock = Mock(return_value=structured)

        db, message, order = self._run_fast_path_case(
            extract_order_mock=extract_mock,
            expected_status="order_detected",
            expected_ok=True,
            expected_order_count=1,
            expected_message_contains="creado",
        )

        self.assertTrue(order)
        self.assertEqual(order.customer_detected_name, "Cliente demo")
        self.assertEqual(order.lines[0].validation_status, "validated")
        self.assertFalse(message.classification_json and "legacy" in message.classification_json.lower())
        db.close()

    def test_email_fast_path_explicit_no_order_does_not_create_order(self):
        structured = OrderExtractionResult(
            raw_input=OrderExtractionInput(
                text="Asunto: Consulta\nRemitente: compras@example.com\n\nSolo queria preguntar por un pedido anterior.",
                source_type="email",
                source_id="1",
            ),
            extracted_data=OrderExtraction(
                is_order=False,
                customer=ExtractedCustomer(raw_name=None, raw_name_source="unknown"),
                lines=[],
                notes=[],
                uncertainties=[],
                requires_review=False,
            ),
            model="gpt-4.1-mini",
        )
        extract_mock = Mock(return_value=structured)

        db, message, order = self._run_fast_path_case(
            extract_order_mock=extract_mock,
            expected_status="no_order",
            expected_ok=True,
            expected_order_count=0,
            expected_message_contains="no es un pedido",
        )

        self.assertIsNone(order)
        self.assertEqual(message.status, "no_order")
        self.assertEqual(message.processing_step, "classified_non_order")
        db.close()

    def test_email_fast_path_order_without_lines_stays_doubtful_without_legacy_fallback(self):
        extract_mock = Mock(side_effect=ValueError("Un pedido debe incluir al menos una linea extraida."))

        db, message, order = self._run_fast_path_case(
            extract_order_mock=extract_mock,
            expected_status="doubtful",
            expected_ok=False,
            expected_order_count=0,
            expected_message_contains="lineas validas",
        )

        self.assertIsNone(order)
        self.assertEqual(message.status, "doubtful")
        self.assertEqual(message.processing_step, "structured_doubtful")
        self.assertIn("lineas validas", message.processing_error or "")
        db.close()

    def test_email_fast_path_structured_failure_fails_fast_without_legacy_fallback(self):
        extract_mock = Mock(side_effect=RuntimeError("Timeout llamando al proveedor IA."))

        db, message, order = self._run_fast_path_case(
            extract_order_mock=extract_mock,
            expected_status="error",
            expected_ok=False,
            expected_order_count=0,
            expected_message_contains="Extraccion estructurada fallida",
        )

        self.assertIsNone(order)
        self.assertEqual(message.status, "error")
        self.assertEqual(message.processing_step, "structured_extraction_error")
        self.assertIn("Extraccion estructurada fallida", message.processing_error or "")
        db.close()


    def test_job_access_isolated_by_company(self):
        db = self.TenantSession()

        job = enqueue_job(
            db,
            company_id=1,
            job_type="process_inbound_message",
            payload={"test": True},
            dedupe_key="tenant-isolation-job",
        )
        db.commit()

        same_company = get_job(
            db,
            company_id=1,
            job_id=job.id,
        )

        other_company = get_job(
            db,
            company_id=2,
            job_id=job.id,
        )

        self.assertIsNotNone(same_company)
        self.assertIsNone(other_company)

        db.close()


if __name__ == "__main__":
    unittest.main()
