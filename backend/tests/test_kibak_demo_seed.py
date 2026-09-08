from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    Communication,
    Company,
    Department,
    DepartmentKnowledge,
    LLMSettings,
    Mailbox,
    RoutingAction,
    RoutingCorrection,
    RoutingDecision,
)
from app.master.database import MasterBase
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser
from scripts.seed_kibak_demo import (
    DEMO_RESET_CONFIRMATION,
    demo_runtime_guard,
    reset_demo,
    seed_demo,
)


class KibakDemoSeedTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        tenant_engine = create_engine(f"sqlite:///{Path(self.tempdir.name, 'tenant.db')}")
        master_engine = create_engine(f"sqlite:///{Path(self.tempdir.name, 'master.db')}")
        Base.metadata.create_all(tenant_engine)
        MasterBase.metadata.create_all(master_engine)
        self.tenant_db = sessionmaker(bind=tenant_engine)()
        self.master_db = sessionmaker(bind=master_engine)()
        self.engines = (tenant_engine, master_engine)

    def tearDown(self):
        self.tenant_db.close()
        self.master_db.close()
        for engine in self.engines:
            engine.dispose()
        self.tempdir.cleanup()

    def _seed(self):
        return seed_demo(
            self.master_db,
            self.tenant_db,
            admin_password="local-demo-password-123",
            database_url="sqlite:///demo-tenant.db",
            environment="development",
            app_slug="kibak",
        )

    def test_seed_creates_demo_content_and_is_idempotent(self):
        summary = self._seed()
        self.assertEqual(summary["company"], "Empresa Demo")
        self.assertEqual(summary["departments"], 6)
        self.assertEqual(summary["mailboxes"], 3)
        self.assertEqual(summary["communications"], 22)
        self.assertEqual(summary["routing_corrections"], 1)
        self.assertEqual(summary["routing_actions"], 4)
        self.assertEqual(self.tenant_db.scalar(select(func.count()).select_from(Department)), 6)
        self.assertGreaterEqual(self.tenant_db.scalar(select(func.count()).select_from(DepartmentKnowledge)), 18)
        self.assertEqual(self.tenant_db.scalar(select(func.count()).select_from(Communication)), 22)
        self.assertEqual(self.tenant_db.scalar(select(func.count()).select_from(RoutingDecision)), 22)
        self.assertEqual(self.tenant_db.scalar(select(func.count()).select_from(RoutingCorrection)), 1)
        self.assertEqual(self.tenant_db.scalar(select(func.count()).select_from(RoutingAction)), 4)
        self.assertEqual(self.master_db.scalar(select(func.count()).select_from(MasterUser)), 4)
        self.assertEqual(self.master_db.scalar(select(func.count()).select_from(CompanyMembership)), 4)

        mailbox = self.tenant_db.scalar(select(Mailbox).where(Mailbox.email_address == "info@empresa-demo.local"))
        self.assertEqual(mailbox.provider, "demo")
        self.assertFalse(mailbox.auto_sync_enabled)
        self.assertFalse(mailbox.smtp_enabled)
        self.assertIsNone(mailbox.imap_password_encrypted)
        self.assertIsNone(mailbox.smtp_password_encrypted)
        llm = self.tenant_db.scalar(select(LLMSettings))
        self.assertFalse(llm.agent_enabled)
        self.assertEqual(llm.provider, "demo")
        self.assertIsNone(llm.api_key_encrypted)

        self._seed()
        self.assertEqual(self.tenant_db.scalar(select(func.count()).select_from(Communication)), 22)
        self.assertEqual(self.tenant_db.scalar(select(func.count()).select_from(RoutingCorrection)), 1)
        self.assertEqual(self.tenant_db.scalar(select(func.count()).select_from(RoutingAction)), 4)
        self.assertEqual(self.master_db.scalar(select(func.count()).select_from(MasterCompany)), 1)

    def test_reset_requires_confirmation_and_only_removes_demo(self):
        self._seed()
        with self.assertRaisesRegex(RuntimeError, "KIBAK_DEMO_RESET"):
            reset_demo(self.master_db, self.tenant_db, confirmation="")

        reset_demo(self.master_db, self.tenant_db, confirmation=DEMO_RESET_CONFIRMATION)
        self.assertEqual(self.tenant_db.scalar(select(func.count()).select_from(Communication)), 0)
        self.assertEqual(self.tenant_db.scalar(select(func.count()).select_from(Department)), 0)
        self.assertEqual(self.master_db.scalar(select(func.count()).select_from(MasterCompany)), 0)
        self.assertEqual(self.master_db.scalar(select(func.count()).select_from(MasterTenantDatabase)), 0)

    def test_guard_and_tenant_scope(self):
        with self.assertRaisesRegex(RuntimeError, "APP_ENV"):
            demo_runtime_guard("production", "kibak")
        with self.assertRaisesRegex(RuntimeError, "APP_SLUG"):
            demo_runtime_guard("development", "anchi")

        self._seed()
        other_company = Company(id=2, name="Otra empresa", legal_name="Otra empresa", active=True)
        self.tenant_db.add(other_company)
        self.tenant_db.commit()
        self.assertEqual(
            self.tenant_db.scalar(select(func.count()).select_from(Communication).where(Communication.company_id == other_company.id)),
            0,
        )


if __name__ == "__main__":
    unittest.main()
