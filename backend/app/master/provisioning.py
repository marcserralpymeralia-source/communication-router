from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_password
from app.db.database import Base
from app.db.models import Company as TenantCompany
from app.db import models as operational_models  # noqa: F401
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser
from app.master.service import slugify
from app.tenancy.migrations import ensure_tenant_migration_record, ensure_tenant_schema


ROOT = Path(__file__).resolve().parents[3]
TENANT_DB_DIR = ROOT / "backend" / "tenants"


def tenant_database_path(company: MasterCompany) -> Path:
    TENANT_DB_DIR.mkdir(parents=True, exist_ok=True)
    return TENANT_DB_DIR / f"{company.id:04d}-{slugify(company.slug or company.name)}.db"


def tenant_database_url(company: MasterCompany) -> str:
    return f"sqlite:///{tenant_database_path(company).as_posix()}"


def _database_type(database_url: str) -> str:
    if "://" not in database_url:
        return "unknown"
    return database_url.split("://", 1)[0]


def _ensure_master_company(master_db: Session, name: str, slug: str) -> MasterCompany:
    company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == slug))
    if company:
        company.name = name
        company.legal_name = company.legal_name or name
        company.active = True
        return company
    company = MasterCompany(name=name, slug=slug, legal_name=name, active=True)
    master_db.add(company)
    master_db.flush()
    return company


def _ensure_master_user(master_db: Session, email: str, full_name: str, password: str) -> MasterUser:
    user = master_db.scalar(select(MasterUser).where(MasterUser.email == email))
    if not user:
        user = MasterUser(email=email, full_name=full_name, password_hash=hash_password(password), is_active=True)
        master_db.add(user)
        master_db.flush()
        return user
    user.full_name = full_name
    user.is_active = True
    master_db.flush()
    return user


def _ensure_membership(master_db: Session, user: MasterUser, company: MasterCompany) -> CompanyMembership:
    membership = master_db.scalar(
        select(CompanyMembership).where(
            CompanyMembership.user_id == user.id,
            CompanyMembership.company_id == company.id,
        )
    )
    if not membership:
        membership = CompanyMembership(user_id=user.id, company_id=company.id, role_key="Administrador", is_active=True, is_owner=True)
        master_db.add(membership)
        master_db.flush()
    else:
        membership.role_key = "Administrador"
        membership.is_active = True
        membership.is_owner = True
    return membership


def _ensure_tenant_database_row(master_db: Session, company: MasterCompany, database_url: str) -> MasterTenantDatabase:
    tenant = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
    if not tenant:
        tenant = MasterTenantDatabase(
            company_id=company.id,
            database_key=slugify(company.slug or company.name),
            database_url=database_url,
            database_type=_database_type(database_url),
            is_active=True,
            health_status="pending",
        )
        master_db.add(tenant)
        master_db.flush()
        return tenant
    tenant.database_key = slugify(company.slug or company.name)
    tenant.database_url = database_url
    tenant.database_type = _database_type(database_url)
    tenant.is_active = True
    tenant.health_status = "pending"
    return tenant


def _session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_engine(database_url, connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {})
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _copy_company_rows(source_db: Session, target_db: Session, company_id: int) -> int:
    inserted = 0
    target_db.execute(text("PRAGMA foreign_keys=OFF"))
    try:
        for table in Base.metadata.sorted_tables:
            if table.name == "companies":
                rows = source_db.execute(select(table).where(table.c.id == company_id)).mappings().all()
            elif "company_id" in table.c:
                rows = source_db.execute(select(table).where(table.c.company_id == company_id)).mappings().all()
            else:
                continue
            if not rows:
                continue
            target_db.execute(table.insert(), [dict(row) for row in rows])
            inserted += len(rows)
        target_db.commit()
    finally:
        target_db.execute(text("PRAGMA foreign_keys=ON"))
    return inserted


def provision_company_database(master_db: Session, legacy_db: Session, company: MasterCompany) -> tuple[MasterTenantDatabase, bool]:
    database_url = tenant_database_url(company)
    tenant_db = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
    was_provisioned = False
    if not tenant_db:
        tenant_db = MasterTenantDatabase(
            company_id=company.id,
            database_key=slugify(company.slug or company.name),
            database_url=database_url,
            database_type="sqlite",
            is_active=True,
            health_status="pending",
        )
        master_db.add(tenant_db)
    else:
        tenant_db.database_key = slugify(company.slug or company.name)
        tenant_db.database_url = database_url
        tenant_db.database_type = "sqlite"
        tenant_db.is_active = True

    target_path = tenant_database_path(company)
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = _session_factory(database_url)
    target_db = session_factory()
    try:
        companies_table = Base.metadata.tables["companies"]
        count = target_db.scalar(select(func.count()).select_from(companies_table))
        if not count:
            _copy_company_rows(legacy_db, target_db, company.id)
            tenant_db.provisioned_at = tenant_db.provisioned_at or company.updated_at
            tenant_db.health_status = "ok"
            tenant_db.notes = f"Provisioned at {target_path.as_posix()}"
            was_provisioned = True
        else:
            tenant_db.health_status = "ok"
        ensure_tenant_schema(database_url, company_id=company.id)
        ensure_tenant_migration_record(
            target_db,
            company.id,
            notes="Provisioned tenant schema" if was_provisioned else "Tenant schema verified during provisioning",
        )
    finally:
        target_db.close()
    master_db.commit()
    return tenant_db, was_provisioned


def provision_external_tenant(
    master_db: Session,
    *,
    tenant_database_url: str,
    company_name: str,
    company_slug: str,
    admin_email: str,
    admin_password: str,
) -> dict[str, str]:
    database_url = (tenant_database_url or "").strip()
    if not database_url:
        raise ValueError("TENANT_DATABASE_URL is required for external tenant provisioning")
    if database_url.startswith("sqlite"):
        raise ValueError("TENANT_DATABASE_URL cannot use sqlite in external tenant provisioning")

    company = _ensure_master_company(master_db, company_name, company_slug)
    user = _ensure_master_user(master_db, admin_email, f"Administrador {company_name}", admin_password)
    membership = _ensure_membership(master_db, user, company)
    tenant = _ensure_tenant_database_row(master_db, company, database_url)

    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
    )
    Base.metadata.create_all(bind=engine)

    tenant_session = _session_factory(database_url)()
    try:
        tenant_company = tenant_session.get(TenantCompany, company.id)
        if tenant_company is None:
            tenant_company = TenantCompany(
                id=company.id,
                name=company.name,
                legal_name=company.legal_name or company.name,
                active=True,
            )
            tenant_session.add(tenant_company)
        else:
            tenant_company.name = company.name
            tenant_company.legal_name = company.legal_name or company.name
            tenant_company.active = True
        tenant_session.commit()
    finally:
        tenant_session.close()

    ensure_tenant_schema(database_url, company_id=company.id)

    session_factory = _session_factory(database_url)
    tenant_db = session_factory()
    try:
        tenant_db.execute(text("SELECT 1"))
        tenant_db.commit()
    finally:
        tenant_db.close()

    tenant.health_status = "ok"
    tenant.notes = "Provisioned against external database"
    master_db.commit()

    return {
        "company_id": str(company.id),
        "company_slug": company.slug,
        "tenant_database": tenant.get_database_url(),
        "admin_email": admin_email,
        "membership_id": str(membership.id),
        "health_status": tenant.health_status,
    }


def provision_demo_external_tenant(
    master_db: Session,
    *,
    tenant_database_url: str,
    company_name: str = "Anchi Demo",
    company_slug: str = "anchi-demo",
    admin_email: str = "admin@anchi.local",
    admin_password: str = "AnchiDemo2026!",
) -> dict[str, str]:
    result = provision_external_tenant(
        master_db,
        tenant_database_url=tenant_database_url,
        company_name=company_name,
        company_slug=company_slug,
        admin_email=admin_email,
        admin_password=admin_password,
    )
    return {
        **result,
        "admin_password": admin_password,
    }
