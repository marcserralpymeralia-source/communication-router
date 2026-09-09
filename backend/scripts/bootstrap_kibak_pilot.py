"""Bootstrap a disabled, local-only KIBAK pilot tenant.

This script is intentionally manual and conservative.  It never contacts an
LLM, IMAP, SMTP, or any other external provider.  The interactive entrypoint
asks for the admin password with ``getpass`` only when a new password hash is
needed; the reusable record builder accepts the password in memory and never
prints it.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine, make_url
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
from app.master.database import MasterSessionLocal  # noqa: E402
from app.master.models import (  # noqa: E402
    CompanyMembership,
    MailboxSyncState,
    MasterCompany,
    MasterTenantDatabase,
    MasterUser,
)
from app.migrations.kibak_baseline import (  # noqa: E402
    KIBAK_BASELINE_CONFIRMATION,
    KIBAK_TENANT_BASELINE_VERSION,
    KIBAK_TENANT_TABLES,
    create_kibak_tenant_schema,
)
from app.routing.policy import build_kibak_readiness  # noqa: E402
from app.routing.service import DEFAULT_ROUTING_THRESHOLDS  # noqa: E402


PILOT_NAME = "KIBAK Pilot"
PILOT_SLUG = "kibak-pilot"
PILOT_DATABASE_NAME = "kibak_tenant_pilot"
PILOT_ADMIN_EMAIL = "admin@kibak-pilot.local"
PILOT_ADMIN_NAME = "KIBAK Pilot Admin"
PILOT_MAILBOX_NAME = "Buzón piloto"
PILOT_MAILBOX_EMAIL = "pilot@kibak-pilot.local"
BOOTSTRAP_CONFIRMATION = "KIBAK_PILOT_BOOTSTRAP"
_PROHIBITED_DATABASE_MARKERS = ("anchi", "gemavi", "comavi")


@dataclass(frozen=True, slots=True)
class DepartmentSpec:
    name: str
    destination_email: str
    responsibility: str
    exclusion: str
    example: str
    guideline: str
    exception: str
    raci_scope: str


DEPARTMENT_SPECS = (
    DepartmentSpec(
        "Comercial",
        "comercial@kibak-pilot.local",
        "Atiende la intención comercial completa: presupuestos, precios, oportunidades, condiciones comerciales e información comercial.",
        "No es responsable de incidencias puras de entrega o transporte, facturas o pagos, ni asuntos de RRHH.",
        "Una petición de precio con contexto de una oportunidad se mantiene en Comercial aunque no pida un presupuesto formal.",
        "Pondera el objetivo y el contexto de la relación, no una palabra aislada del asunto.",
        "Si una consulta mezcla una oportunidad con un problema físico de entrega, conserva Logística como alternativa y pide revisión.",
        "sales_inquiry",
    ),
    DepartmentSpec(
        "Logística",
        "logistica@kibak-pilot.local",
        "Atiende entregas, transporte, retrasos, mercancía incompleta o dañada y dirección o condiciones de entrega.",
        "No es responsable de facturación pura, nuevas oportunidades comerciales ni asuntos de RRHH.",
        "Un aviso de mercancía dañada se clasifica aquí aunque también mencione una factura relacionada.",
        "Distingue el hecho operativo de la consecuencia administrativa y prioriza el problema que requiere acción.",
        "Si el mensaje solo reclama un importe sin incidencia física, deriva la responsabilidad a Administración.",
        "delivery_incident",
    ),
    DepartmentSpec(
        "Administración",
        "administracion@kibak-pilot.local",
        "Atiende facturas, pagos, vencimientos, datos fiscales y documentación administrativa.",
        "No es responsable de incidencias físicas de entrega, oportunidades comerciales ni asuntos de RRHH.",
        "Una solicitud de datos fiscales para emitir una factura pertenece a Administración aunque incluya el nombre de un producto.",
        "Separa una obligación administrativa de una conversación comercial o logística mencionada como contexto.",
        "Si la factura cuestionada deriva de una mercancía dañada, marca la posible intervención de Logística y requiere revisión.",
        "invoice",
    ),
    DepartmentSpec(
        "Compras",
        "compras@kibak-pilot.local",
        "Atiende proveedores, aprovisionamiento, condiciones de compra e incidencias de suministro.",
        "No asume por defecto ventas a clientes, facturación de cliente o solicitudes de RRHH.",
        "Una falta de suministro comunicada por un proveedor pertenece a Compras aunque afecte a una entrega futura.",
        "Usa la relación con el proveedor y la necesidad de aprovisionamiento para interpretar la intención.",
        "Si el mensaje trata de una entrega a un cliente final, considera Logística como alternativa.",
        "supplier_issue",
    ),
    DepartmentSpec(
        "RRHH",
        "rrhh@kibak-pilot.local",
        "Atiende CV, contratación, vacaciones, nóminas, ausencias y documentación laboral.",
        "No es responsable de facturación, logística, oportunidades comerciales ni asuntos operativos de proveedores.",
        "Una petición de documentación para una contratación se mantiene en RRHH aunque la persona solicite información general de la empresa.",
        "Protege la confidencialidad y solicita revisión humana cuando el asunto laboral sea sensible o ambiguo.",
        "Si el contenido puede afectar a una persona y no queda claro que sea laboral, no automatices la derivación.",
        "hr_request",
    ),
    DepartmentSpec(
        "Dirección",
        "direccion@kibak-pilot.local",
        "Atiende estrategia, partnerships, escalados, asuntos sensibles y cuestiones transversales sin responsable claro.",
        "No absorbe solicitudes que ya tienen una responsabilidad clara en otro departamento.",
        "Un escalado transversal con impacto estratégico puede pertenecer a Dirección aunque incluya tareas comerciales u operativas.",
        "Es una salida de incertidumbre responsable, no un cajón de sastre para mensajes que no se han leído con atención.",
        "Cuando exista una responsabilidad clara, conserva ese departamento y usa Dirección solo como alternativa explicada.",
        "strategic_issue",
    ),
)


@dataclass(frozen=True, slots=True)
class PilotSummary:
    company_id: int
    admin_email: str
    departments: int
    knowledge_items: int
    raci_assignments: int
    mailbox_id: int
    llm_provider: str
    readiness: dict[str, Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _database_type(database_url: str) -> str:
    return make_url(database_url).drivername.split("+", 1)[0]


def _assert_kibak_runtime() -> None:
    settings = get_settings()
    if settings.app_slug.strip().lower() != "kibak":
        raise RuntimeError("Bootstrap rechazado: APP_SLUG debe ser exactamente 'kibak'.")
    if settings.environment.strip().lower() == "production":
        raise RuntimeError("Bootstrap rechazado: no se ejecuta en production.")
    if settings.environment.strip().lower() not in {"development", "demo", "test"}:
        raise RuntimeError("Bootstrap rechazado: entorno local KIBAK no reconocido.")


def _assert_safe_database_url(database_url: str, *, require_postgres: bool = False) -> None:
    marker = database_url.lower()
    if any(token in marker for token in _PROHIBITED_DATABASE_MARKERS):
        raise RuntimeError("Bootstrap rechazado: la base identifica un recurso ANCHI/GEMAVI.")
    if require_postgres and _database_type(database_url) != "postgresql":
        raise RuntimeError("Bootstrap rechazado: el tenant piloto requiere PostgreSQL.")


def _pilot_database_url() -> str:
    settings = get_settings()
    source_url = settings.tenant_database_url or settings.database_url
    _assert_safe_database_url(source_url, require_postgres=True)
    target_url = make_url(source_url).set(database=PILOT_DATABASE_NAME)
    target = target_url.render_as_string(hide_password=False)
    _assert_safe_database_url(target, require_postgres=True)
    return target


def _database_exists(database_url: str) -> bool:
    target = make_url(database_url)
    maintenance_url = target.set(database="postgres")
    engine = create_engine(maintenance_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            return bool(
                connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                    {"database_name": target.database},
                ).scalar()
            )
    finally:
        engine.dispose()


def _create_database(database_url: str) -> None:
    target = make_url(database_url)
    maintenance_url = target.set(database="postgres")
    engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                {"database_name": target.database},
            ).scalar()
            if not exists:
                # The name is a module constant, not user input.
                connection.execute(text(f'CREATE DATABASE "{PILOT_DATABASE_NAME}"'))
    finally:
        engine.dispose()


def _sequence_needs_alignment(max_id: int | None, last_value: int | None, is_called: bool | None) -> bool:
    """Return whether the next generated company id could collide."""

    if max_id is None or last_value is None or is_called is None:
        return False
    next_value = last_value + 1 if is_called else last_value
    return next_value <= max_id


def _quote_qualified_identifier(identifier: str) -> str:
    return ".".join(f'"{part.replace(chr(34), chr(34) * 2)}"' for part in identifier.split("."))


def _synchronize_company_sequence(engine: Engine) -> bool:
    """Align only the pilot tenant's companies sequence when it is behind."""

    if engine.dialect.name != "postgresql":
        return False
    with engine.begin() as connection:
        sequence_name = connection.execute(
            text("SELECT pg_get_serial_sequence('public.companies', 'id')")
        ).scalar()
        max_id = connection.execute(text("SELECT MAX(id) FROM public.companies")).scalar()
        if not sequence_name or max_id is None:
            return False
        sequence_row = connection.execute(
            text(f"SELECT last_value, is_called FROM {_quote_qualified_identifier(str(sequence_name))}")
        ).mappings().one()
        if not _sequence_needs_alignment(int(max_id), sequence_row["last_value"], sequence_row["is_called"]):
            return False
        connection.execute(
            text("SELECT setval(CAST(:sequence_name AS regclass), :max_id, true)"),
            {"sequence_name": str(sequence_name), "max_id": int(max_id)},
        )
        return True


def _ensure_master_company(master_db: Session) -> MasterCompany:
    demo = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == "empresa-demo"))
    company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == PILOT_SLUG))
    name_collision = master_db.scalar(select(MasterCompany).where(MasterCompany.name == PILOT_NAME))
    if name_collision and name_collision.slug != PILOT_SLUG:
        raise RuntimeError("Bootstrap rechazado: KIBAK Pilot ya existe con otro slug.")
    if company and demo and company.id == demo.id:
        raise RuntimeError("Bootstrap rechazado: el tenant piloto coincide con Empresa Demo.")
    if company:
        if company.name != PILOT_NAME:
            raise RuntimeError("Bootstrap rechazado: kibak-pilot ya existe con otro nombre.")
        company.active = True
        return company
    company = MasterCompany(
        name=PILOT_NAME,
        slug=PILOT_SLUG,
        legal_name=PILOT_NAME,
        active=True,
        default_language="es",
        default_timezone="Europe/Madrid",
    )
    master_db.add(company)
    master_db.flush()
    return company


def _ensure_master_user(master_db: Session, company: MasterCompany, password: str | None) -> MasterUser:
    user = master_db.scalar(select(MasterUser).where(MasterUser.email == PILOT_ADMIN_EMAIL))
    if user is not None:
        membership = master_db.scalar(
            select(CompanyMembership).where(
                CompanyMembership.user_id == user.id,
                CompanyMembership.company_id == company.id,
            )
        )
        if membership is None:
            raise RuntimeError("Bootstrap rechazado: el email del administrador ya pertenece a otro contexto.")
        user.full_name = PILOT_ADMIN_NAME
        user.is_active = True
        return user
    if not password:
        raise RuntimeError("Se necesita una contraseña interactiva para crear el administrador piloto.")
    user = MasterUser(
        email=PILOT_ADMIN_EMAIL,
        full_name=PILOT_ADMIN_NAME,
        password_hash=hash_password(password),
        is_active=True,
    )
    master_db.add(user)
    master_db.flush()
    return user


def _ensure_membership(master_db: Session, user: MasterUser, company: MasterCompany) -> None:
    membership = master_db.scalar(
        select(CompanyMembership).where(
            CompanyMembership.user_id == user.id,
            CompanyMembership.company_id == company.id,
        )
    )
    if membership is None:
        membership = CompanyMembership(user_id=user.id, company_id=company.id)
        master_db.add(membership)
    membership.role_key = "Administrador"
    membership.is_active = True
    membership.is_owner = True
    master_db.flush()


def _ensure_tenant_company(tenant_db: Session, company_id: int) -> Company:
    companies = tenant_db.scalars(select(Company)).all()
    unexpected = [item for item in companies if item.id != company_id]
    if unexpected:
        raise RuntimeError("Bootstrap rechazado: la base del piloto contiene otro tenant.")
    company = tenant_db.get(Company, company_id)
    if company is None:
        raise RuntimeError("Bootstrap rechazado: falta la fila Company del baseline KIBAK.")
    if company.name not in {PILOT_NAME, ""}:
        raise RuntimeError("Bootstrap rechazado: Company no coincide con KIBAK Pilot.")
    company.name = PILOT_NAME
    company.legal_name = company.legal_name or PILOT_NAME
    company.active = True
    company.language = "es"
    company.default_language = "es"
    company.timezone = "Europe/Madrid"
    return company


def _ensure_tenant_admin(tenant_db: Session, company_id: int, master_user: MasterUser, password: str | None) -> User:
    role = tenant_db.scalar(select(Role).where(Role.company_id == company_id, Role.name == "Administrador"))
    if role is None:
        role = Role(
            company_id=company_id,
            name="Administrador",
            permissions=DEFAULT_ROLE_PERMISSIONS["Administrador"],
        )
        tenant_db.add(role)
        tenant_db.flush()
    else:
        role.permissions = DEFAULT_ROLE_PERMISSIONS["Administrador"]
    user = tenant_db.scalar(select(User).where(User.email == PILOT_ADMIN_EMAIL))
    if user is not None:
        if user.company_id != company_id:
            raise RuntimeError("Bootstrap rechazado: el email del administrador pertenece a otro tenant.")
        user.role_id = role.id
        user.name = PILOT_ADMIN_NAME
        user.is_active = True
        return user
    if not password:
        raise RuntimeError("Se necesita una contraseña interactiva para completar el administrador piloto.")
    user = User(
        company_id=company_id,
        role_id=role.id,
        email=master_user.email,
        name=PILOT_ADMIN_NAME,
        password_hash=hash_password(password),
        is_active=True,
    )
    tenant_db.add(user)
    tenant_db.flush()
    return user


def _ensure_departments(tenant_db: Session, company_id: int) -> dict[str, Department]:
    departments: dict[str, Department] = {}
    for spec in DEPARTMENT_SPECS:
        department = tenant_db.scalar(
            select(Department).where(Department.company_id == company_id, Department.name == spec.name)
        )
        if department is None:
            department = Department(company_id=company_id, name=spec.name)
            tenant_db.add(department)
            tenant_db.flush()
        department.description = spec.responsibility
        department.destination_email = spec.destination_email
        department.active = True
        departments[spec.name] = department
    return departments


def _ensure_knowledge(tenant_db: Session, departments: dict[str, Department]) -> int:
    count = 0
    for spec in DEPARTMENT_SPECS:
        department = departments[spec.name]
        items = (
            ("Responsabilidad semántica", "responsibility", spec.responsibility),
            ("Exclusiones semánticas", "exclusion", spec.exclusion),
            ("Ejemplo de interpretación", "example", spec.example),
            ("Guideline de decisión", "guideline", spec.guideline),
            ("Excepción y revisión", "exception", spec.exception),
        )
        for title, knowledge_type, content in items:
            item = tenant_db.scalar(
                select(DepartmentKnowledge).where(
                    DepartmentKnowledge.department_id == department.id,
                    DepartmentKnowledge.title == title,
                    DepartmentKnowledge.knowledge_type == knowledge_type,
                )
            )
            if item is None:
                item = DepartmentKnowledge(
                    department_id=department.id,
                    title=title,
                    content=content,
                    knowledge_type=knowledge_type,
                )
                tenant_db.add(item)
            else:
                item.content = content
                item.active = True
            count += 1
    tenant_db.flush()
    return count


def _ensure_raci(tenant_db: Session, company_id: int, admin: User, departments: dict[str, Department]) -> int:
    count = 0
    for spec in DEPARTMENT_SPECS:
        department = departments[spec.name]
        assignment = tenant_db.scalar(
            select(RaciAssignment).where(
                RaciAssignment.company_id == company_id,
                RaciAssignment.department_id == department.id,
                RaciAssignment.scope == spec.raci_scope,
                RaciAssignment.raci_role == "responsible",
            )
        )
        if assignment is None:
            assignment = RaciAssignment(
                company_id=company_id,
                department_id=department.id,
                user_id=admin.id,
                scope=spec.raci_scope,
                raci_role="responsible",
            )
            tenant_db.add(assignment)
        else:
            assignment.user_id = admin.id
            assignment.active = True
        member = tenant_db.scalar(
            select(DepartmentMember).where(
                DepartmentMember.company_id == company_id,
                DepartmentMember.department_id == department.id,
                DepartmentMember.user_id == admin.id,
            )
        )
        if member is None:
            tenant_db.add(
                DepartmentMember(
                    company_id=company_id,
                    department_id=department.id,
                    user_id=admin.id,
                    role="responsible",
                    active=True,
                )
            )
        else:
            member.role = "responsible"
            member.active = True
        count += 1
    tenant_db.flush()
    return count


def _ensure_llm_settings(tenant_db: Session, company_id: int, admin_id: int) -> LLMSettings:
    settings = tenant_db.scalar(select(LLMSettings).where(LLMSettings.company_id == company_id))
    if settings is None:
        settings = LLMSettings(company_id=company_id)
        tenant_db.add(settings)
    settings.agent_enabled = False
    settings.auto_routing_enabled = True
    settings.auto_forwarding_enabled = False
    settings.simulation_mode = True
    settings.provider = "openai"
    settings.api_key_encrypted = None
    settings.classification_model = DEFAULT_OPENAI_MODEL
    settings.extraction_model = DEFAULT_OPENAI_MODEL
    settings.validation_model = DEFAULT_OPENAI_MODEL
    settings.routing_review_threshold = DEFAULT_ROUTING_THRESHOLDS.review_confidence
    settings.routing_auto_threshold = DEFAULT_ROUTING_THRESHOLDS.auto_route_confidence
    settings.updated_by = admin_id
    settings.updated_at = _utcnow()
    settings.store_llm_payloads = False
    settings.anonymize_llm_logs = True
    settings.detailed_llm_logs = False
    settings.debug_mode = False
    tenant_db.flush()
    return settings


def _ensure_prompt(tenant_db: Session, company_id: int, admin_id: int) -> None:
    ensure_prompt_template(
        tenant_db,
        company_id,
        ROUTING_PROMPT_PURPOSE,
        created_by_user_id=admin_id,
    )


def _ensure_mailbox(tenant_db: Session, company_id: int, admin_id: int) -> Mailbox:
    mailboxes = tenant_db.scalars(select(Mailbox).where(Mailbox.company_id == company_id)).all()
    if len(mailboxes) > 1:
        raise RuntimeError("Bootstrap rechazado: el tenant piloto tiene más de un mailbox.")
    mailbox = mailboxes[0] if mailboxes else None
    if mailbox is None:
        mailbox = Mailbox(
            company_id=company_id,
            name=PILOT_MAILBOX_NAME,
            email_address=PILOT_MAILBOX_EMAIL,
            provider="imap",
            connection_method="password",
            inbox_folder="INBOX",
        )
        tenant_db.add(mailbox)
    elif mailbox.email_address != PILOT_MAILBOX_EMAIL or mailbox.name != PILOT_MAILBOX_NAME:
        raise RuntimeError("Bootstrap rechazado: el mailbox existente no coincide con el piloto.")
    credential_fields = (
        "client_secret_encrypted",
        "access_token_encrypted",
        "refresh_token_encrypted",
        "imap_password_encrypted",
        "smtp_password_encrypted",
    )
    if any(getattr(mailbox, field) for field in credential_fields):
        raise RuntimeError("Bootstrap rechazado: el mailbox piloto ya contiene credenciales.")
    mailbox.enabled = False
    mailbox.auto_sync_enabled = False
    mailbox.smtp_enabled = False
    mailbox.auto_process_on_fetch = False
    mailbox.imap_host = None
    mailbox.imap_username = None
    mailbox.smtp_host = None
    mailbox.smtp_username = None
    mailbox.connected_email = None
    mailbox.updated_by = admin_id
    mailbox.updated_at = _utcnow()
    tenant_db.flush()
    return mailbox


def _ensure_mailbox_sync_state(master_db: Session, company_id: int, mailbox_id: int) -> None:
    state = master_db.scalar(
        select(MailboxSyncState).where(
            MailboxSyncState.company_id == company_id,
            MailboxSyncState.mailbox_id == mailbox_id,
        )
    )
    if state is None:
        state = MailboxSyncState(company_id=company_id, mailbox_id=mailbox_id)
        master_db.add(state)
    state.enabled = False
    state.status = "disabled"
    state.sync_status = "disabled"
    state.listener_status = "inactive"
    state.backfill_status = "idle"
    state.next_run_at = None
    state.updated_at = _utcnow()
    master_db.flush()


def _ensure_tenant_database_row(master_db: Session, company: MasterCompany, database_url: str) -> None:
    tenant = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
    if tenant is None:
        tenant = MasterTenantDatabase(
            company_id=company.id,
            database_key=PILOT_SLUG,
            database_url=database_url,
            database_type=_database_type(database_url),
            is_active=True,
            health_status="ok",
            provisioned_at=_utcnow(),
        )
        master_db.add(tenant)
    else:
        if tenant.database_key != PILOT_SLUG or tenant.get_database_url() != database_url:
            raise RuntimeError("Bootstrap rechazado: el tenant database existente no coincide con el piloto.")
        tenant.is_active = True
        tenant.database_type = _database_type(database_url)
        tenant.health_status = "ok"
    master_db.flush()


def ensure_pilot_records(
    master_db: Session,
    tenant_db: Session,
    *,
    database_url: str,
    admin_password: str | None,
) -> PilotSummary:
    """Create or reconcile pilot rows in already-created KIBAK schemas.

    The caller owns both transactions.  This function never commits, performs
    no network I/O, and never changes the Empresa Demo rows.
    """

    _assert_safe_database_url(database_url, require_postgres=True)
    company = _ensure_master_company(master_db)
    master_user = _ensure_master_user(master_db, company, admin_password)
    _ensure_membership(master_db, master_user, company)
    _ensure_tenant_database_row(master_db, company, database_url)

    _ensure_tenant_company(tenant_db, company.id)
    tenant_admin = _ensure_tenant_admin(tenant_db, company.id, master_user, admin_password)
    departments = _ensure_departments(tenant_db, company.id)
    knowledge_items = _ensure_knowledge(tenant_db, departments)
    raci_assignments = _ensure_raci(tenant_db, company.id, tenant_admin, departments)
    _ensure_llm_settings(tenant_db, company.id, tenant_admin.id)
    _ensure_prompt(tenant_db, company.id, tenant_admin.id)
    mailbox = _ensure_mailbox(tenant_db, company.id, tenant_admin.id)
    _ensure_mailbox_sync_state(master_db, company.id, mailbox.id)
    tenant_db.flush()
    master_db.flush()

    readiness = build_kibak_readiness(tenant_db, company.id)
    return PilotSummary(
        company_id=company.id,
        admin_email=PILOT_ADMIN_EMAIL,
        departments=len(departments),
        knowledge_items=knowledge_items,
        raci_assignments=raci_assignments,
        mailbox_id=mailbox.id,
        llm_provider="openai",
        readiness=readiness,
    )


def _tenant_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def _validate_existing_tenant_schema(engine: Engine, company_id: int) -> None:
    tables = set(inspect(engine).get_table_names())
    if tables != set(KIBAK_TENANT_TABLES):
        missing = sorted(set(KIBAK_TENANT_TABLES) - tables)
        extra = sorted(tables - set(KIBAK_TENANT_TABLES))
        raise RuntimeError(f"Schema KIBAK inesperado; faltan={missing}, sobran={extra}.")
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT version, status FROM schema_migrations WHERE company_id = :company_id"),
            {"company_id": company_id},
        ).mappings().one_or_none()
    if row is None or row["version"] != KIBAK_TENANT_BASELINE_VERSION or row["status"] != "current":
        raise RuntimeError("Bootstrap rechazado: ledger tenant KIBAK ausente o inesperado.")


def _print_plan() -> None:
    print(f"Tenant: {PILOT_NAME} ({PILOT_SLUG})")
    print(f"Database objetivo: {PILOT_DATABASE_NAME}")
    print("Departamentos: " + ", ".join(spec.name for spec in DEPARTMENT_SPECS))
    print("Politica: AUTO_ROUTING=ON, SIMULATION_MODE=ON, AUTO_FORWARDING=OFF")
    print("LLM: provider=openai, disabled, credential=pending")
    print(f"Mailbox: {PILOT_MAILBOX_NAME}, disabled, sin IMAP/SMTP")


def _print_summary(summary: PilotSummary) -> None:
    readiness = summary.readiness
    warnings = [item["key"] for item in readiness["checks"] if item["status"] == "WARN"]
    print(f"Tenant listo: {PILOT_SLUG} (id={summary.company_id})")
    print(f"Admin: {summary.admin_email}, role=Administrador")
    print(f"Departments: {summary.departments}; knowledge: {summary.knowledge_items}; RACI: {summary.raci_assignments}")
    print("Policy: auto_routing=ON, simulation=ON, auto_forwarding=OFF")
    print(f"Mailbox: {PILOT_MAILBOX_NAME} (id={summary.mailbox_id}), enabled=OFF")
    print(f"LLM: provider={summary.llm_provider}, disabled, credential=pending")
    print(f"Readiness: ready={readiness['ready']}, failures={readiness['failures']}, warnings={readiness['warnings']}")
    if warnings:
        print("Readiness warnings: " + ", ".join(warnings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap manual y seguro del tenant piloto KIBAK.")
    parser.parse_args(argv)
    master_db = tenant_db = None
    tenant_engine = None
    password: str | None = None
    try:
        _assert_kibak_runtime()
        settings = get_settings()
        _assert_safe_database_url(settings.master_database_url)
        database_url = _pilot_database_url()
        _print_plan()
        if input("Type KIBAK_PILOT_BOOTSTRAP to continue: ").strip() != BOOTSTRAP_CONFIRMATION:
            print("Bootstrap cancelado.")
            return 1

        master_db = MasterSessionLocal()
        company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == PILOT_SLUG))
        if company is None:
            company = _ensure_master_company(master_db)
        if company.name != PILOT_NAME:
            raise RuntimeError("Bootstrap rechazado: kibak-pilot ya existe con otro nombre.")

        database_was_present = _database_exists(database_url)
        if not database_was_present:
            _create_database(database_url)
            tenant_engine = _tenant_engine(database_url)
            create_kibak_tenant_schema(
                tenant_engine,
                company_id=company.id,
                company_name=PILOT_NAME,
                confirmation=KIBAK_BASELINE_CONFIRMATION,
            )
        else:
            tenant_engine = _tenant_engine(database_url)
            _validate_existing_tenant_schema(tenant_engine, company.id)
        _synchronize_company_sequence(tenant_engine)

        tenant_factory = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        tenant_db = tenant_factory()
        master_user_exists = master_db.scalar(select(MasterUser).where(MasterUser.email == PILOT_ADMIN_EMAIL)) is not None
        tenant_user_exists = tenant_db.scalar(select(User).where(User.email == PILOT_ADMIN_EMAIL)) is not None
        if not master_user_exists or not tenant_user_exists:
            password = getpass.getpass("Temporary pilot admin password: ")
            if not password:
                raise RuntimeError("La contraseña del administrador piloto no puede estar vacía.")
        summary = ensure_pilot_records(
            master_db,
            tenant_db,
            database_url=database_url,
            admin_password=password,
        )
        tenant_db.commit()
        master_db.commit()
        _print_summary(summary)
        return 0
    except KeyboardInterrupt:
        print("Bootstrap cancelado.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        if tenant_db is not None:
            tenant_db.rollback()
        if master_db is not None:
            master_db.rollback()
        print(f"Bootstrap KIBAK no ejecutado: {exc}", file=sys.stderr)
        return 1
    finally:
        password = None
        if tenant_db is not None:
            tenant_db.close()
        if master_db is not None:
            master_db.close()
        if tenant_engine is not None:
            tenant_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
