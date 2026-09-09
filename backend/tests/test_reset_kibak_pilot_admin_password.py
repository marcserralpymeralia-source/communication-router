from __future__ import annotations

import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.security import verify_password
from app.db.database import Base
from app.db.models import Company, Role, User
from app.master.database import MasterBase
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser
from scripts.reset_kibak_pilot_admin_password import (
    PILOT_ADMIN_EMAIL,
    PILOT_NAME,
    PILOT_SLUG,
    _assert_kibak_local_runtime,
    load_reset_targets,
    reset_password_records,
)


class ResetKibakPilotAdminPasswordTests(unittest.TestCase):
    def setUp(self):
        self.master_engine = create_engine("sqlite://")
        self.tenant_engine = create_engine("sqlite://")
        MasterBase.metadata.create_all(self.master_engine)
        Base.metadata.create_all(self.tenant_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine)
        self.TenantSession = sessionmaker(bind=self.tenant_engine)
        with self.MasterSession() as db:
            demo = MasterCompany(id=1, name="Empresa Demo", slug="empresa-demo", active=True)
            company = MasterCompany(id=2, name=PILOT_NAME, slug=PILOT_SLUG, active=True)
            demo_user = MasterUser(id=1, email="admin@empresa-demo.local", full_name="Demo Admin", password_hash="demo-hash", is_active=True)
            user = MasterUser(id=7, email=PILOT_ADMIN_EMAIL, full_name="Pilot Admin", password_hash="old-master", is_active=True)
            db.add_all([demo, company, demo_user, user])
            db.flush()
            db.add(
                CompanyMembership(
                    user_id=user.id,
                    company_id=company.id,
                    role_key="Administrador",
                    is_active=True,
                    is_owner=True,
                )
            )
            db.add(
                CompanyMembership(
                    user_id=demo_user.id,
                    company_id=demo.id,
                    role_key="Administrador",
                    is_active=True,
                    is_owner=True,
                )
            )
            db.add(
                MasterTenantDatabase(
                    company_id=company.id,
                    database_key=PILOT_SLUG,
                    database_url="postgresql://local/kibak_tenant_pilot",
                    database_type="postgresql",
                    is_active=True,
                )
            )
            db.commit()
        with self.TenantSession() as db:
            company = Company(id=2, name=PILOT_NAME, active=True)
            role = Role(id=3, company_id=company.id, name="Administrador")
            user = User(
                id=7,
                company_id=company.id,
                role_id=role.id,
                email=PILOT_ADMIN_EMAIL,
                name="Pilot Admin",
                password_hash="old-tenant",
                is_active=True,
            )
            db.add_all([company, role, user])
            db.commit()

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()

    def test_resolves_fixed_user_in_fixed_tenant_and_updates_both_hashes(self):
        password = "New pilot password 2026!"
        with self.MasterSession() as master_db, self.TenantSession() as tenant_db:
            targets = load_reset_targets(master_db, tenant_db)
            self.assertEqual(targets.company.slug, PILOT_SLUG)
            self.assertEqual(targets.master_user.email, PILOT_ADMIN_EMAIL)
            self.assertEqual(targets.tenant_user.company_id, targets.company.id)
            reset_password_records(master_db, tenant_db, new_password=password)
            master_db.commit()
            tenant_db.commit()

        with self.MasterSession() as master_db, self.TenantSession() as tenant_db:
            master_user = master_db.scalar(select(MasterUser).where(MasterUser.email == PILOT_ADMIN_EMAIL))
            tenant_user = tenant_db.scalar(select(User).where(User.email == PILOT_ADMIN_EMAIL))
            self.assertTrue(verify_password(password, master_user.password_hash))
            self.assertTrue(verify_password(password, tenant_user.password_hash))
            self.assertNotIn(password, master_user.password_hash)
            self.assertNotIn(password, tenant_user.password_hash)

    def test_does_not_change_membership_or_other_user(self):
        with self.MasterSession() as master_db:
            other = MasterUser(id=8, email="other@kibak-pilot.local", full_name="Other", password_hash="unchanged", is_active=True)
            master_db.add(other)
            master_db.commit()
        with self.TenantSession() as tenant_db:
            other_role = tenant_db.get(Role, 3)
            tenant_db.add(User(id=8, company_id=2, role_id=other_role.id, email="other@kibak-pilot.local", name="Other", password_hash="unchanged", is_active=True))
            tenant_db.commit()

        with self.MasterSession() as master_db, self.TenantSession() as tenant_db:
            targets = load_reset_targets(master_db, tenant_db)
            membership_before = (targets.membership.role_key, targets.membership.is_active, targets.membership.is_owner)
            reset_password_records(master_db, tenant_db, new_password="Another pilot password 2026!")
            master_db.commit()
            tenant_db.commit()
            self.assertEqual((targets.membership.role_key, targets.membership.is_active, targets.membership.is_owner), membership_before)
        with self.MasterSession() as master_db, self.TenantSession() as tenant_db:
            self.assertEqual(master_db.get(MasterUser, 8).password_hash, "unchanged")
            demo = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == "empresa-demo"))
            demo_user = master_db.scalar(select(MasterUser).where(MasterUser.email == "admin@empresa-demo.local"))
            self.assertEqual((demo.name, demo.slug, demo.active), ("Empresa Demo", "empresa-demo", True))
            self.assertEqual(demo_user.password_hash, "demo-hash")
            self.assertEqual(tenant_db.get(User, 8).password_hash, "unchanged")

    def test_rejects_production(self):
        with self.assertRaisesRegex(RuntimeError, "APP_ENV"):
            _assert_kibak_local_runtime(SimpleNamespace(app_slug="kibak", environment="production"))

    def test_rejects_wrong_tenant_company(self):
        with self.TenantSession() as tenant_db:
            tenant_db.get(Company, 2).name = "Empresa Demo"
            tenant_db.commit()
        with self.MasterSession() as master_db, self.TenantSession() as tenant_db:
            with self.assertRaisesRegex(RuntimeError, "Company"):
                load_reset_targets(master_db, tenant_db)

    def test_rejects_missing_target_user(self):
        with self.TenantSession() as tenant_db:
            tenant_db.query(User).filter(User.email == PILOT_ADMIN_EMAIL).delete()
            tenant_db.commit()
        with self.MasterSession() as master_db, self.TenantSession() as tenant_db:
            with self.assertRaisesRegex(RuntimeError, "usuario tenant"):
                load_reset_targets(master_db, tenant_db)


if __name__ == "__main__":
    unittest.main()
