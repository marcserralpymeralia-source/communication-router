from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.db.database import Base
from app.db.models import Company
from app.migrations.kibak_baseline import KIBAK_MASTER_TABLES, KIBAK_TENANT_TABLES
from app.master.database import MasterBase
from app.master.migrations import master_migration_report, upgrade_master_schema
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser
from app.tenancy.migrations import tenant_migration_report, upgrade_tenant_schema


class PostgreSQLSmokeTests(unittest.TestCase):
    @unittest.skipUnless(os.getenv("POSTGRES_TEST_MASTER_DATABASE_URL") and os.getenv("POSTGRES_TEST_DATABASE_URL"), "PostgreSQL smoke is disabled")
    def test_master_and_tenant_migrations_are_current(self):
        master_url = os.environ["POSTGRES_TEST_MASTER_DATABASE_URL"]
        tenant_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
        master_engine = create_engine(master_url)
        tenant_engine = create_engine(tenant_url)
        try:
            # Keep the smoke target aligned with the clean KIBAK PostgreSQL
            # baseline. Creating all historical metadata here would leave
            # legacy tables behind and make the strict schema report fail.
            MasterBase.metadata.create_all(
                master_engine,
                tables=[MasterBase.metadata.tables[name] for name in KIBAK_MASTER_TABLES],
            )
            Base.metadata.create_all(
                tenant_engine,
                tables=[Base.metadata.tables[name] for name in KIBAK_TENANT_TABLES],
            )
            master_session = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)()
            tenant_session = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)()
            try:
                tenant_session.add(
                    Company(
                        id=1,
                        name="PG Demo",
                        legal_name="PG Demo SL",
                        active=True,
                        country="España",
                        language="es",
                        timezone="Europe/Madrid",
                    )
                )
                tenant_session.commit()
                master_session.add_all(
                    [
                        MasterCompany(id=1, name="PG Demo", slug="pg-demo", active=True, legal_name="PG Demo SL"),
                        MasterUser(id=1, email="admin@anchi.local", full_name="Admin PG", password_hash=hash_password("admin123"), is_active=True),
                        CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True),
                        MasterTenantDatabase(company_id=1, database_key="pg-demo", database_url=tenant_url, is_active=True, health_status="ok", provisioned_at=datetime.now(timezone.utc)),
                    ]
                )
                master_session.commit()
                upgrade_master_schema(master_engine, application_version="1.2.3")
                upgrade_tenant_schema(tenant_engine, company_id=1, application_version="1.2.3")
                self.assertTrue(master_migration_report(master_session)["is_current"])
                self.assertTrue(tenant_migration_report(tenant_session, 1)["is_current"])
                self.assertEqual(master_session.scalar(select(MasterCompany.id)) or 0, 1)
            finally:
                tenant_session.close()
                master_session.close()
        finally:
            master_engine.dispose()
            tenant_engine.dispose()

