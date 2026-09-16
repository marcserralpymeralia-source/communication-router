"""Provision a new KIBAK staging tenant without contacting external providers.

The target database must already exist and is read from the configured
``TENANT_DATABASE_URL``.  Tenant and admin inputs are interactive; passwords
are collected with getpass and are never accepted as CLI arguments.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agent.model_catalog import DEFAULT_OPENAI_MODEL  # noqa: E402
from app.agent.prompt_runtime import ROUTING_PROMPT_PURPOSE, ensure_prompt_template  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.permissions import DEFAULT_ROLE_PERMISSIONS  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.models import (  # noqa: E402
    Company,
    Department,
    DepartmentKnowledge,
    DepartmentMember,
    LLMSettings,
    Mailbox,
    RaciAssignment,
    Role,
    User,
)
from app.master.database import MasterSessionLocal, init_master_db  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.migrations.kibak_baseline import (  # noqa: E402
    KIBAK_BASELINE_CONFIRMATION,
    KIBAK_TENANT_BASELINE_VERSION,
    KIBAK_TENANT_TABLES,
    create_kibak_tenant_schema,
)
from app.routing.policy import build_kibak_readiness  # noqa: E402
from app.routing.service import DEFAULT_ROUTING_THRESHOLDS  # noqa: E402
from scripts.bootstrap_kibak_pilot import DEPARTMENT_SPECS  # noqa: E402


CONFIRMATION = "KIBAK_STAGING_TENANT_PROVISION"
PROHIBITED_MARKERS = ("anchi", "gemavi", "comavi")


def _safe_database_url(database_url: str) -> None:
    lowered = database_url.lower()
    if any(marker in lowered for marker in PROHIBITED_MARKERS):
        raise RuntimeError("Provisioning rechazado: la base contiene un marcador ANCHI/GEMAVI.")
    if not lowered.startswith("postgresql"):
        raise RuntimeError("Provisioning de staging requiere PostgreSQL.")


def _get_inputs() -> dict[str, str]:
    name = input("Nombre del tenant: ").strip()
    slug = input("Slug del tenant: ").strip().lower()
    email = input("Email del administrador: ").strip().lower()
    if not name or not slug or not email or "@" not in email:
        raise ValueError("Nombre, slug y email de administrador son obligatorios.")
    print(f"Tenant: {name} ({slug})")
    print(f"Usuario: {email}")
    print("Acción: Provisionar tenant staging")
    if input(f"Escribe {CONFIRMATION} para continuar: ").strip() != CONFIRMATION:
        raise RuntimeError("Provisioning cancelado.")
    return {"name": name, "slug": slug, "email": email}


def _ensure_master_company(db: Session, *, name: str, slug: str) -> MasterCompany:
    by_slug = db.scalar(select(MasterCompany).where(MasterCompany.slug == slug))
    by_name = db.scalar(select(MasterCompany).where(MasterCompany.name == name))
    if by_name and by_name.slug != slug:
        raise RuntimeError("El nombre del tenant ya pertenece a otro slug.")
    if by_slug:
        if by_slug.name != name:
            raise RuntimeError("El slug ya existe con otro nombre.")
        return by_slug
    company = MasterCompany(name=name, slug=slug, legal_name=name, active=True)
    db.add(company)
    db.flush()
    return company


def _ensure_master_identity(db: Session, *, company: MasterCompany, email: str, password: str | None) -> MasterUser:
    user = db.scalar(select(MasterUser).where(MasterUser.email == email))
    if user:
        membership = db.scalar(select(CompanyMembership).where(CompanyMembership.user_id == user.id, CompanyMembership.company_id == company.id))
        if membership is None:
            raise RuntimeError("El administrador ya pertenece a otro tenant.")
        return user
    if not password:
        raise RuntimeError("Se necesita password interactivo para crear el administrador.")
    user = MasterUser(email=email, full_name=f"Administrador {company.name}", password_hash=hash_password(password), is_active=True)
    db.add(user)
    db.flush()
    db.add(CompanyMembership(user_id=user.id, company_id=company.id, role_key="Administrador", is_active=True, is_owner=True))
    db.flush()
    return user


def _ensure_tenant_identity(db: Session, *, company_id: int, email: str, password: str | None) -> User:
    role = db.scalar(select(Role).where(Role.company_id == company_id, Role.name == "Administrador"))
    if not role:
        role = Role(company_id=company_id, name="Administrador", permissions=DEFAULT_ROLE_PERMISSIONS["Administrador"])
        db.add(role)
        db.flush()
    user = db.scalar(select(User).where(User.email == email))
    if user:
        if user.company_id != company_id:
            raise RuntimeError("El usuario tenant ya pertenece a otro tenant.")
        return user
    if not password:
        raise RuntimeError("Se necesita password interactivo para crear el administrador tenant.")
    user = User(company_id=company_id, role_id=role.id, email=email, name=f"Administrador {company_id}", password_hash=hash_password(password), is_active=True)
    db.add(user)
    db.flush()
    return user


def _ensure_organization(db: Session, *, company_id: int, admin: User, tenant_slug: str) -> tuple[int, int, int]:
    departments: dict[str, Department] = {}
    for spec in DEPARTMENT_SPECS:
        department = db.scalar(select(Department).where(Department.company_id == company_id, Department.name == spec.name))
        if not department:
            department = Department(company_id=company_id, name=spec.name)
            db.add(department)
            db.flush()
        department.description = spec.responsibility
        department.destination_email = f"{spec.name.lower().replace('ó', 'o').replace('í', 'i').replace(' ', '-') }@{tenant_slug}.local"
        department.active = True
        departments[spec.name] = department
    knowledge_count = 0
    raci_count = 0
    for spec in DEPARTMENT_SPECS:
        department = departments[spec.name]
        entries = (
            ("Responsabilidad semántica", "responsibility", spec.responsibility),
            ("Exclusiones semánticas", "exclusion", spec.exclusion),
            ("Ejemplo de interpretación", "example", spec.example),
            ("Guideline de decisión", "guideline", spec.guideline),
            ("Excepción y revisión", "exception", spec.exception),
        )
        for title, kind, content in entries:
            item = db.scalar(select(DepartmentKnowledge).where(DepartmentKnowledge.department_id == department.id, DepartmentKnowledge.title == title, DepartmentKnowledge.knowledge_type == kind))
            if not item:
                db.add(DepartmentKnowledge(department_id=department.id, title=title, content=content, knowledge_type=kind))
            else:
                item.content = content
                item.active = True
            knowledge_count += 1
        assignment = db.scalar(select(RaciAssignment).where(RaciAssignment.company_id == company_id, RaciAssignment.department_id == department.id, RaciAssignment.scope == spec.raci_scope, RaciAssignment.raci_role == "responsible"))
        if not assignment:
            db.add(RaciAssignment(company_id=company_id, department_id=department.id, user_id=admin.id, scope=spec.raci_scope, raci_role="responsible"))
        else:
            assignment.user_id = admin.id
            assignment.active = True
        member = db.scalar(select(DepartmentMember).where(DepartmentMember.company_id == company_id, DepartmentMember.department_id == department.id, DepartmentMember.user_id == admin.id))
        if not member:
            db.add(DepartmentMember(company_id=company_id, department_id=department.id, user_id=admin.id, role="responsible", active=True))
        raci_count += 1
    return len(departments), knowledge_count, raci_count


def _ensure_safe_settings(db: Session, *, company_id: int, admin_id: int) -> None:
    llm = db.scalar(select(LLMSettings).where(LLMSettings.company_id == company_id))
    if not llm:
        llm = LLMSettings(company_id=company_id)
        db.add(llm)
    llm.agent_enabled = False
    llm.auto_routing_enabled = True
    llm.simulation_mode = True
    llm.auto_forwarding_enabled = False
    llm.provider = "openai"
    llm.api_key_encrypted = None
    llm.classification_model = DEFAULT_OPENAI_MODEL
    llm.extraction_model = DEFAULT_OPENAI_MODEL
    llm.validation_model = DEFAULT_OPENAI_MODEL
    llm.routing_review_threshold = DEFAULT_ROUTING_THRESHOLDS.review_confidence
    llm.routing_auto_threshold = DEFAULT_ROUTING_THRESHOLDS.auto_route_confidence
    llm.store_llm_payloads = False
    llm.anonymize_llm_logs = True
    llm.detailed_llm_logs = False
    llm.debug_mode = False
    llm.updated_by = admin_id
    ensure_prompt_template(db, company_id, ROUTING_PROMPT_PURPOSE, created_by_user_id=admin_id)

    mailbox_count = db.scalar(select(Mailbox.id).where(Mailbox.company_id == company_id).limit(1))
    if mailbox_count:
        raise RuntimeError("El tenant ya tiene mailbox; no se modifica durante este provisioning genérico.")


def _tenant_schema_ready(engine, company_id: int) -> bool:
    tables = set(inspect(engine).get_table_names())
    if tables != set(KIBAK_TENANT_TABLES):
        return False
    with engine.connect() as connection:
        row = connection.execute(text("SELECT version, status FROM schema_migrations WHERE company_id = :company_id ORDER BY id DESC LIMIT 1"), {"company_id": company_id}).mappings().first()
    return bool(row and row["version"] == KIBAK_TENANT_BASELINE_VERSION and row["status"] == "current")


def provision(*, name: str, slug: str, email: str) -> dict[str, Any]:
    settings = get_settings()
    if settings.app_slug.strip().lower() != "kibak":
        raise RuntimeError("Provisioning rechazado: APP_SLUG debe ser kibak.")
    if settings.environment != "staging":
        raise RuntimeError("Provisioning cloud exige APP_ENV=staging.")
    database_url = str(settings.tenant_database_url or "").strip()
    _safe_database_url(database_url)
    init_master_db()
    password: str | None = None
    master_db = MasterSessionLocal()
    try:
        company = _ensure_master_company(master_db, name=name, slug=slug)
        master_user_exists = master_db.scalar(select(MasterUser).where(MasterUser.email == email)) is not None
        if not master_user_exists:
            password = getpass.getpass("Password del administrador: ")
            if not password or password != getpass.getpass("Repite el password: "):
                raise ValueError("Los passwords no coinciden o están vacíos.")
        master_user = _ensure_master_identity(master_db, company=company, email=email, password=password)
        tenant_row = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
        if tenant_row and tenant_row.get_database_url() != database_url:
            raise RuntimeError("El tenant ya está asociado a otra base; no se sobrescribe.")
        if not tenant_row:
            master_db.add(MasterTenantDatabase(company_id=company.id, database_key=slug, database_url=database_url, database_type="postgresql", is_active=True, health_status="pending"))
        master_db.commit()

        engine = create_engine(database_url, pool_pre_ping=True)
        if not _tenant_schema_ready(engine, company.id):
            if inspect(engine).get_table_names():
                raise RuntimeError("La base tenant no está vacía ni contiene un baseline KIBAK reconocido.")
            create_kibak_tenant_schema(engine, company_id=company.id, company_name=name, confirmation=KIBAK_BASELINE_CONFIRMATION)
        TenantSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        tenant_db = TenantSession()
        try:
            tenant_company = tenant_db.get(Company, company.id)
            if tenant_company is None:
                tenant_company = Company(id=company.id, name=name, legal_name=name, active=True)
                tenant_db.add(tenant_company)
                tenant_db.flush()
            elif tenant_company.name != name:
                raise RuntimeError("La Company tenant no coincide con el tenant solicitado.")
            tenant_user = _ensure_tenant_identity(tenant_db, company_id=company.id, email=email, password=password)
            departments, knowledge, raci = _ensure_organization(tenant_db, company_id=company.id, admin=tenant_user, tenant_slug=slug)
            _ensure_safe_settings(tenant_db, company_id=company.id, admin_id=tenant_user.id)
            tenant_db.commit()
        except Exception:
            tenant_db.rollback()
            raise
        finally:
            tenant_db.close()
            engine.dispose()
        return {"company_id": company.id, "tenant": slug, "departments": departments, "knowledge": knowledge, "raci": raci, "simulation_mode": True, "auto_forwarding": False}
    except Exception:
        master_db.rollback()
        raise
    finally:
        master_db.close()


def main() -> int:
    try:
        values = _get_inputs()
        summary = provision(**values)
        print(f"Tenant provisionado: {summary['tenant']} (id={summary['company_id']})")
        print(f"Departments={summary['departments']}; knowledge={summary['knowledge']}; RACI={summary['raci']}")
        print("Safety: simulation=ON, auto_forwarding=OFF, mailbox=disabled")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Provisioning cancelado.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Provisioning KIBAK no completado: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
