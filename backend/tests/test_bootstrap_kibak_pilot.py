from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.security import verify_password
from app.db.database import Base
from app.db.models import Company, Department, DepartmentKnowledge, LLMSettings, Mailbox, PromptTemplate, RaciAssignment, User
from app.master.database import MasterBase
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser, MailboxSyncState
from scripts.bootstrap_kibak_pilot import (
    PILOT_ADMIN_EMAIL,
    PILOT_DATABASE_NAME,
    PILOT_MAILBOX_EMAIL,
    PILOT_NAME,
    PILOT_SLUG,
    _sequence_needs_alignment,
    ensure_pilot_records,
)


class KibakPilotBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.master_engine = create_engine(f"sqlite:///{root / 'master.db'}")
        self.tenant_engine = create_engine(f"sqlite:///{root / 'tenant.db'}")
        MasterBase.metadata.create_all(self.master_engine)
        Base.metadata.create_all(self.tenant_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine)
        self.TenantSession = sessionmaker(bind=self.tenant_engine)
        with self.MasterSession() as db:
            db.add(MasterCompany(id=1, name="Empresa Demo", slug="empresa-demo", active=True))
            db.commit()
        with self.TenantSession() as db:
            # The demo tenant has its own database; this target represents the
            # pilot database and starts with its baseline Company row.
            db.add(Company(id=2, name="", active=True))
            db.commit()

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        self.tempdir.cleanup()

    def _bootstrap(self, password: str | None = "pilot-test-password"):
        with self.MasterSession() as master_db, self.TenantSession() as tenant_db:
            summary = ensure_pilot_records(
                master_db,
                tenant_db,
                database_url=f"postgresql://local/{PILOT_DATABASE_NAME}",
                admin_password=password,
            )
            tenant_db.commit()
            master_db.commit()
            return summary

    def test_creates_pilot_without_modifying_demo_and_is_idempotent(self):
        summary = self._bootstrap()
        self.assertEqual(summary.departments, 6)
        self.assertEqual(summary.knowledge_items, 30)
        self.assertEqual(summary.raci_assignments, 6)
        self.assertEqual(summary.llm_provider, "openai")

        with self.MasterSession() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(MasterCompany)), 2)
            demo = db.scalar(select(MasterCompany).where(MasterCompany.slug == "empresa-demo"))
            self.assertEqual((demo.name, demo.slug), ("Empresa Demo", "empresa-demo"))
        with self.TenantSession() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Department)), 6)
            self.assertEqual(db.scalar(select(func.count()).select_from(DepartmentKnowledge)), 30)
            self.assertEqual(db.scalar(select(func.count()).select_from(RaciAssignment)), 6)
            self.assertEqual(db.scalar(select(func.count()).select_from(Mailbox)), 1)

        self._bootstrap(password=None)
        with self.MasterSession() as db, self.TenantSession() as tenant_db:
            pilot = db.scalar(select(MasterCompany).where(MasterCompany.slug == PILOT_SLUG))
            self.assertEqual(db.scalar(select(func.count()).select_from(MasterCompany)), 2)
            self.assertEqual(tenant_db.scalar(select(func.count()).select_from(Department)), 6)
            self.assertEqual(tenant_db.scalar(select(func.count()).select_from(DepartmentKnowledge)), 30)
            self.assertEqual(tenant_db.scalar(select(func.count()).select_from(RaciAssignment)), 6)
            self.assertEqual(tenant_db.scalar(select(func.count()).select_from(Mailbox)), 1)
            self.assertEqual(tenant_db.scalar(select(func.count()).select_from(PromptTemplate)), 1)
            self.assertEqual(pilot.name, PILOT_NAME)

    def test_admin_hash_is_created_without_appearing_in_summary_or_output(self):
        secret = "pilot-password-never-printed"
        output = io.StringIO()
        with redirect_stdout(output):
            self._bootstrap(password=secret)
        rendered = output.getvalue()
        self.assertNotIn(secret, rendered)
        with self.MasterSession() as db, self.TenantSession() as tenant_db:
            master_user = db.scalar(select(MasterUser).where(MasterUser.email == PILOT_ADMIN_EMAIL))
            tenant_user = tenant_db.scalar(select(User).where(User.email == PILOT_ADMIN_EMAIL))
            self.assertTrue(verify_password(secret, master_user.password_hash))
            self.assertTrue(verify_password(secret, tenant_user.password_hash))
            self.assertNotIn(master_user.password_hash, rendered)
            self.assertNotIn(tenant_user.password_hash, rendered)

    def test_policy_mailbox_llm_prompt_and_sync_are_safe(self):
        self._bootstrap()
        with self.MasterSession() as master_db, self.TenantSession() as tenant_db:
            pilot = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == PILOT_SLUG))
            membership = master_db.scalar(select(CompanyMembership).where(CompanyMembership.company_id == pilot.id))
            mailbox = tenant_db.scalar(select(Mailbox).where(Mailbox.company_id == pilot.id))
            llm = tenant_db.scalar(select(LLMSettings).where(LLMSettings.company_id == pilot.id))
            state = master_db.scalar(select(MailboxSyncState).where(MailboxSyncState.company_id == pilot.id))
            self.assertEqual(membership.role_key, "Administrador")
            self.assertFalse(llm.agent_enabled)
            self.assertTrue(llm.auto_routing_enabled)
            self.assertTrue(llm.simulation_mode)
            self.assertFalse(llm.auto_forwarding_enabled)
            self.assertEqual(llm.provider, "openai")
            self.assertIsNone(llm.api_key_encrypted)
            self.assertFalse(mailbox.enabled)
            self.assertFalse(mailbox.auto_sync_enabled)
            self.assertFalse(mailbox.smtp_enabled)
            self.assertEqual(mailbox.email_address, PILOT_MAILBOX_EMAIL)
            self.assertIsNone(mailbox.imap_password_encrypted)
            self.assertIsNone(mailbox.smtp_password_encrypted)
            self.assertFalse(state.enabled)
            self.assertEqual(state.status, "disabled")
            self.assertEqual(state.backfill_status, "idle")

    def test_company_sequence_contract_only_aligns_when_next_id_can_collide(self):
        self.assertTrue(_sequence_needs_alignment(1, 1, False))
        self.assertFalse(_sequence_needs_alignment(1, 1, True))
        self.assertFalse(_sequence_needs_alignment(1, 2, False))
        self.assertFalse(_sequence_needs_alignment(None, 1, False))

    def test_incompatible_tenant_rolls_back_without_leaving_pilot_master_rows(self):
        with self.TenantSession() as tenant_db:
            tenant_db.get(Company, 2).name = "Otro tenant"
            tenant_db.commit()
        with self.MasterSession() as master_db, self.TenantSession() as tenant_db:
            with self.assertRaisesRegex(RuntimeError, "Company no coincide"):
                ensure_pilot_records(
                    master_db,
                    tenant_db,
                    database_url=f"postgresql://local/{PILOT_DATABASE_NAME}",
                    admin_password="pilot-test-password",
                )
            master_db.rollback()
            tenant_db.rollback()
        with self.MasterSession() as db:
            self.assertIsNone(db.scalar(select(MasterCompany).where(MasterCompany.slug == PILOT_SLUG)))
        with self.TenantSession() as db:
            self.assertEqual(db.get(Company, 2).name, "Otro tenant")
            self.assertEqual(db.scalar(select(func.count()).select_from(Department)), 0)


if __name__ == "__main__":
    unittest.main()
