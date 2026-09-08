"""Seed a safe, deterministic KIBAK customer demo dataset.

The command only targets development/demo environments and never connects to a
provider. Demo mailbox records deliberately contain no IMAP/SMTP credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


DEMO_COMPANY_NAME = "Empresa Demo"
DEMO_COMPANY_SLUG = "empresa-demo"
DEMO_COMPANY_ID = 1
DEMO_ADMIN_EMAIL = "admin@empresa-demo.local"
DEMO_RESET_CONFIRMATION = "KIBAK_DEMO_RESET"
DEMO_MAILBOX_PROVIDER = "demo"
DEMO_EXTERNAL_PREFIX = "kibak-demo-"


@dataclass(frozen=True)
class KnowledgeSpec:
    title: str
    content: str
    knowledge_type: str


@dataclass(frozen=True)
class DepartmentSpec:
    name: str
    description: str
    destination_email: str
    knowledge: tuple[KnowledgeSpec, ...]


@dataclass(frozen=True)
class CommunicationSpec:
    key: str
    subject: str
    body: str
    sender: str
    department: str | None
    alternative_department: str | None
    category: str
    confidence: float
    status: str
    scenario: str
    reason: str
    ambiguity_reason: str | None = None
    corrected_department: str | None = None
    action_status: str | None = None


DEPARTMENTS = (
    DepartmentSpec(
        "Comercial",
        "Presupuestos, precios, oportunidades y solicitudes comerciales.",
        "comercial@empresa-demo.local",
        (
            KnowledgeSpec("Responsabilidades", "Gestiona presupuestos, precios, nuevas oportunidades y solicitudes comerciales.", "responsibility"),
            KnowledgeSpec("Exclusiones", "No gestiona incidencias logísticas ni facturación administrativa.", "exclusion"),
            KnowledgeSpec("Ejemplo", "Una petición de precio o un nuevo lead comercial pertenece a Comercial.", "example"),
        ),
    ),
    DepartmentSpec(
        "Logística",
        "Entregas, transporte, retrasos, mercancía no recibida y expediciones.",
        "logistica@empresa-demo.local",
        (
            KnowledgeSpec("Responsabilidades", "Gestiona entregas, transporte, retrasos, mercancía no recibida y expediciones.", "responsibility"),
            KnowledgeSpec("Exclusiones", "No gestiona precios ni presupuestos.", "exclusion"),
            KnowledgeSpec("Excepción", "Si una comunicación mezcla factura y entrega, revisar con Administración.", "exception"),
        ),
    ),
    DepartmentSpec(
        "Administración",
        "Facturas, pagos, documentación administrativa y datos fiscales.",
        "administracion@empresa-demo.local",
        (
            KnowledgeSpec("Responsabilidades", "Gestiona facturas, pagos, documentación administrativa y datos fiscales.", "responsibility"),
            KnowledgeSpec("Exclusiones", "No decide incidencias de transporte ni condiciones comerciales.", "exclusion"),
            KnowledgeSpec("Ejemplo", "Una factura rectificativa o una consulta de vencimiento pertenece a Administración.", "example"),
        ),
    ),
    DepartmentSpec(
        "Compras",
        "Proveedores, aprovisionamiento e incidencias de compra.",
        "compras@empresa-demo.local",
        (
            KnowledgeSpec("Responsabilidades", "Gestiona proveedores, aprovisionamiento e incidencias de compra.", "responsibility"),
            KnowledgeSpec("Exclusiones", "No gestiona leads comerciales ni vacaciones de empleados.", "exclusion"),
            KnowledgeSpec("Ejemplo", "Una consulta sobre alta de proveedor o reposición de material pertenece a Compras.", "example"),
        ),
    ),
    DepartmentSpec(
        "RRHH",
        "Candidatos, trabajadores, nóminas, vacaciones y documentación laboral.",
        "rrhh@empresa-demo.local",
        (
            KnowledgeSpec("Responsabilidades", "Gestiona candidatos, trabajadores, nóminas, vacaciones y documentación laboral.", "responsibility"),
            KnowledgeSpec("Exclusiones", "No gestiona facturas de proveedores ni entregas.", "exclusion"),
            KnowledgeSpec("Ejemplo", "Una solicitud de vacaciones o una nómina pertenece a RRHH.", "example"),
        ),
    ),
    DepartmentSpec(
        "Dirección",
        "Asuntos estratégicos, comunicaciones sensibles y casos ambiguos escalados.",
        "direccion@empresa-demo.local",
        (
            KnowledgeSpec("Responsabilidades", "Gestiona asuntos estratégicos, comunicaciones sensibles y casos ambiguos escalados.", "responsibility"),
            KnowledgeSpec("Exclusiones", "No sustituye la gestión operativa diaria de cada departamento.", "exclusion"),
            KnowledgeSpec("Excepción", "Puede actuar como accountable cuando una decisión afecta a varias áreas.", "exception"),
        ),
    ),
)


USERS = (
    ("admin@empresa-demo.local", "Administrador demo", "Administrador", True),
    ("ana.comercial@empresa-demo.local", "Ana Martín", "Comercial", False),
    ("carlos.operaciones@empresa-demo.local", "Carlos Gómez", "Operaciones", False),
    ("laura.rrhh@empresa-demo.local", "Laura Sánchez", "RRHH", False),
)

MAILBOXES = (
    ("info@empresa-demo.local", "Información general"),
    ("hola@empresa-demo.local", "Atención general"),
    ("incidencias@empresa-demo.local", "Incidencias"),
)


COMMUNICATIONS = (
    CommunicationSpec("001", "Presupuesto para 250 unidades", "Buenos días, necesitamos presupuesto para 250 unidades del modelo Atlas.", "compras@cliente-ejemplo.local", "Comercial", None, "sales_quote", .96, "routed", "auto_routed", "Solicitud comercial clara de presupuesto y volumen." , action_status="sent"),
    CommunicationSpec("002", "Precio para el próximo trimestre", "¿Podéis enviarnos vuestra mejor tarifa para el próximo trimestre?", "compras@cliente-ejemplo.local", "Comercial", None, "price_request", .95, "routed", "auto_routed", "Consulta directa sobre precios."),
    CommunicationSpec("003", "Nueva oportunidad de distribución", "Queremos estudiar una nueva oportunidad de distribución en Valencia.", "direccion@cliente-ejemplo.local", "Comercial", None, "sales_lead", .93, "routed", "auto_routed", "Nueva oportunidad comercial.", action_status="pending"),
    CommunicationSpec("004", "La mercancía no ha llegado", "La mercancía que tenía que llegar ayer todavía no ha llegado.", "logistica@cliente-ejemplo.local", "Logística", None, "delivery_incident", .94, "routed", "auto_routed", "Incidencia explícita de entrega.", action_status="sent"),
    CommunicationSpec("005", "Seguimiento del transporte", "¿Podéis confirmarnos dónde está el transporte de la expedición 4812?", "almacen@cliente-ejemplo.local", "Logística", None, "transport_tracking", .92, "routed", "auto_routed", "Seguimiento de una expedición."),
    CommunicationSpec("006", "Entrega parcial", "Hemos recibido solo parte de la mercancía indicada en el albarán.", "recepcion@cliente-ejemplo.local", "Logística", None, "delivery_incident", .94, "routed", "auto_routed", "Mercancía recibida parcialmente."),
    CommunicationSpec("007", "Factura rectificativa", "Adjunto la factura rectificativa solicitada para la factura F-2026-104.", "facturacion@cliente-ejemplo.local", "Administración", None, "invoice_query", .95, "routed", "auto_routed", "Factura rectificativa adjunta."),
    CommunicationSpec("008", "Consulta de vencimiento", "¿Nos confirmáis la fecha de vencimiento de la factura F-2026-099?", "contabilidad@cliente-ejemplo.local", "Administración", None, "payment_query", .92, "routed", "auto_routed", "Consulta administrativa de pago."),
    CommunicationSpec("009", "Cambio de datos fiscales", "Necesitamos actualizar los datos fiscales de nuestra empresa.", "administracion@cliente-ejemplo.local", "Administración", None, "fiscal_data", .91, "routed", "auto_routed", "Solicitud de datos fiscales."),
    CommunicationSpec("010", "Alta de proveedor", "Queremos conocer la documentación necesaria para dar de alta un proveedor.", "proveedores@cliente-ejemplo.local", "Compras", None, "supplier_request", .91, "routed", "auto_routed", "Gestión de proveedor."),
    CommunicationSpec("011", "Incidencia con suministro", "El proveedor confirma una demora en el suministro de material.", "proveedores@cliente-ejemplo.local", "Compras", None, "purchase_incident", .93, "routed", "auto_routed", "Incidencia de aprovisionamiento."),
    CommunicationSpec("012", "Reposición de material", "Necesitamos revisar la reposición de consumibles para este mes.", "almacen@cliente-ejemplo.local", "Compras", None, "procurement", .90, "routed", "auto_routed", "Aprovisionamiento operativo."),
    CommunicationSpec("013", "Solicitud de vacaciones", "Quisiera solicitar vacaciones del 12 al 23 de agosto.", "empleado@cliente-ejemplo.local", "RRHH", None, "leave_request", .92, "routed", "auto_routed", "Solicitud laboral de vacaciones."),
    CommunicationSpec("014", "Documentación de candidato", "Adjunto mi documentación para el proceso de selección abierto.", "candidato@cliente-ejemplo.local", "RRHH", None, "candidate_documentation", .94, "routed", "auto_routed", "Documentación de candidatura."),
    CommunicationSpec("015", "Revisión estratégica", "Nos gustaría agendar una reunión para revisar la estrategia de colaboración.", "ceo@cliente-ejemplo.local", "Dirección", None, "strategic_topic", .90, "routed", "auto_routed", "Asunto estratégico."),
    CommunicationSpec("016", "Comunicación sensible", "Necesito trasladar un asunto sensible directamente a dirección.", "direccion@cliente-ejemplo.local", "Dirección", None, "sensitive_topic", .91, "routed", "auto_routed", "La comunicación pide explícitamente Dirección."),
    CommunicationSpec("017", "Factura de mercancía pendiente", "Tenemos una factura de una mercancía que todavía no hemos recibido.", "contabilidad@cliente-ejemplo.local", "Administración", "Logística", "invoice_delivery_overlap", .76, "pending_review", "review", "La factura y la entrega requieren coordinación.", "Mezcla una consulta administrativa con una incidencia logística."),
    CommunicationSpec("018", "Condiciones de entrega y precio", "Querríamos revisar las condiciones de entrega y precio para el próximo pedido.", "compras@cliente-ejemplo.local", "Comercial", "Logística", "commercial_logistics_overlap", .72, "pending_review", "review", "Combina condiciones comerciales y logísticas.", "La intención pertenece a dos áreas."),
    CommunicationSpec("019", "Factura de proveedor", "El proveedor nos pregunta cuándo se procesará su factura y cuándo llegará el material.", "proveedor@cliente-ejemplo.local", "Compras", "Administración", "supplier_invoice_overlap", .74, "pending_review", "review", "Compras debe coordinar la respuesta con Administración.", "Hay una parte de proveedor y otra de factura."),
    CommunicationSpec("020", "Asunto importante", "Necesito hablar con alguien sobre un asunto importante.", "contacto@cliente-ejemplo.local", None, "Dirección", "ambiguous", .38, "pending_review", "unclassified", "No hay suficiente contexto para clasificar con seguridad.", "Petición genérica sin intención identificable."),
    CommunicationSpec("021", "Ayuda con una gestión", "¿Podéis decirme quién puede ayudarme con esta gestión?", "contacto@cliente-ejemplo.local", None, None, "ambiguous", .42, "pending_review", "unclassified", "Falta información sobre la gestión solicitada.", "Mensaje demasiado genérico."),
    CommunicationSpec("022", "Factura y entrega no recibida", "La factura indica una mercancía que no hemos recibido; necesitamos resolverlo.", "contabilidad@cliente-ejemplo.local", "Administración", "Logística", "invoice_delivery_overlap", .74, "corrected", "corrected", "La IA propuso Administración, pero el usuario priorizó la resolución logística.", "La evidencia final indica que el problema principal es la entrega.", "Logística", "failed"),
)


def demo_runtime_guard(environment: str, app_slug: str) -> None:
    normalized_environment = (environment or "").strip().lower()
    normalized_slug = (app_slug or "").strip().lower()
    if normalized_environment not in {"development", "demo"}:
        raise RuntimeError("KIBAK demo seed solo puede ejecutarse con APP_ENV=development o APP_ENV=demo.")
    if normalized_slug != "kibak":
        raise RuntimeError("KIBAK demo seed requiere APP_SLUG=kibak.")


def _now(days_ago: int = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _ensure_master_company(master_db, tenant_db):
    from app.db.models import Company
    from app.master.models import MasterCompany

    tenant_company = tenant_db.get(Company, DEMO_COMPANY_ID)
    if tenant_company and tenant_company.name not in {"KIBAK Test", DEMO_COMPANY_NAME} and tenant_company.plan != "demo":
        raise RuntimeError("El tenant contiene una empresa no demo; se rechaza modificarla.")
    company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == DEMO_COMPANY_SLUG))
    if company is None:
        company = MasterCompany(
            id=tenant_company.id if tenant_company else DEMO_COMPANY_ID,
            name=DEMO_COMPANY_NAME,
            slug=DEMO_COMPANY_SLUG,
            legal_name=DEMO_COMPANY_NAME,
            active=True,
        )
        master_db.add(company)
    elif tenant_company and company.id != tenant_company.id:
        raise RuntimeError("La empresa demo no coincide entre master y tenant.")
    company.name = DEMO_COMPANY_NAME
    company.legal_name = DEMO_COMPANY_NAME
    company.active = True
    master_db.flush()
    return company


def _ensure_tenant_company(tenant_db, company_id: int):
    from app.db.models import Company

    company = tenant_db.get(Company, company_id)
    if company is None:
        company = Company(id=company_id, name=DEMO_COMPANY_NAME, legal_name=DEMO_COMPANY_NAME, active=True, plan="demo")
        tenant_db.add(company)
    elif company.name not in {"KIBAK Test", DEMO_COMPANY_NAME} and company.plan != "demo":
        raise RuntimeError("El tenant contiene una empresa no demo; se rechaza modificarla.")
    company.name = DEMO_COMPANY_NAME
    company.legal_name = DEMO_COMPANY_NAME
    company.active = True
    company.plan = "demo"
    company.email = "info@empresa-demo.local"
    company.notification_email = "info@empresa-demo.local"
    company.language = "es"
    company.default_language = "es"
    company.timezone = "Europe/Madrid"
    tenant_db.flush()
    return company


def _ensure_master_user(master_db, email: str, name: str, password: str):
    from app.core.security import hash_password
    from app.master.models import MasterUser

    user = master_db.scalar(select(MasterUser).where(MasterUser.email == email))
    if user is None:
        user = MasterUser(email=email, full_name=name, password_hash=hash_password(password), is_active=True)
        master_db.add(user)
        master_db.flush()
    else:
        user.full_name = name
        user.is_active = True
    return user


def _ensure_membership(master_db, user, company, *, owner: bool):
    from app.master.models import CompanyMembership

    membership = master_db.scalar(
        select(CompanyMembership).where(
            CompanyMembership.user_id == user.id,
            CompanyMembership.company_id == company.id,
        )
    )
    if membership is None:
        membership = CompanyMembership(user_id=user.id, company_id=company.id)
        master_db.add(membership)
    membership.role_key = "Administrador" if owner else "Usuario demo"
    membership.is_active = True
    membership.is_owner = owner
    master_db.flush()
    return membership


def _ensure_tenant_role(tenant_db, company_id: int, name: str):
    from app.db.models import Role

    role = tenant_db.scalar(select(Role).where(Role.company_id == company_id, Role.name == name))
    if role is None:
        role = Role(company_id=company_id, name=name, permissions="")
        tenant_db.add(role)
        tenant_db.flush()
    return role


def _ensure_tenant_user(tenant_db, company_id: int, master_user, role):
    from app.db.models import User

    user = tenant_db.scalar(select(User).where(User.email == master_user.email))
    by_id = tenant_db.get(User, master_user.id)
    if user is not None and user.id != master_user.id:
        raise RuntimeError(f"Colisión de usuario demo para {master_user.email}.")
    if by_id is not None and by_id.email != master_user.email:
        raise RuntimeError(f"Colisión de ID de usuario demo para {master_user.email}.")
    if user is None:
        user = User(
            id=master_user.id,
            company_id=company_id,
            role_id=role.id,
            email=master_user.email,
            name=master_user.full_name,
            password_hash=master_user.password_hash,
            is_active=True,
        )
        tenant_db.add(user)
    else:
        user.company_id = company_id
        user.role_id = role.id
        user.name = master_user.full_name
        user.is_active = True
    tenant_db.flush()
    return user


def _ensure_departments(tenant_db, company_id: int) -> dict[str, Any]:
    from app.db.models import Department, DepartmentKnowledge

    result = {}
    for spec in DEPARTMENTS:
        department = tenant_db.scalar(select(Department).where(Department.company_id == company_id, Department.name == spec.name))
        if department is None:
            department = Department(company_id=company_id, name=spec.name)
            tenant_db.add(department)
            tenant_db.flush()
        department.description = spec.description
        department.destination_email = spec.destination_email
        department.active = True
        tenant_db.flush()
        result[spec.name] = department
        for knowledge_spec in spec.knowledge:
            item = tenant_db.scalar(
                select(DepartmentKnowledge).where(
                    DepartmentKnowledge.department_id == department.id,
                    DepartmentKnowledge.title == knowledge_spec.title,
                    DepartmentKnowledge.knowledge_type == knowledge_spec.knowledge_type,
                )
            )
            if item is None:
                item = DepartmentKnowledge(
                    department_id=department.id,
                    title=knowledge_spec.title,
                    content=knowledge_spec.content,
                    knowledge_type=knowledge_spec.knowledge_type,
                )
                tenant_db.add(item)
            item.content = knowledge_spec.content
            item.active = True
    tenant_db.flush()
    return result


def _ensure_raci(tenant_db, company_id: int, departments: dict[str, Any], users: dict[str, Any]) -> None:
    from app.db.models import RaciAssignment

    definitions = (
        ("delivery_incident", "Logística", "carlos.operaciones@empresa-demo.local", "responsible"),
        ("delivery_incident", "Dirección", "admin@empresa-demo.local", "accountable"),
        ("delivery_incident", "Comercial", "ana.comercial@empresa-demo.local", "consulted"),
        ("sales_lead", "Comercial", "ana.comercial@empresa-demo.local", "responsible"),
        ("sales_lead", "Dirección", "admin@empresa-demo.local", "accountable"),
        ("invoice_query", "Administración", "admin@empresa-demo.local", "responsible"),
    )
    for scope, department_name, email, role in definitions:
        department = departments[department_name]
        user = users[email]
        assignment = tenant_db.scalar(
            select(RaciAssignment).where(
                RaciAssignment.company_id == company_id,
                RaciAssignment.department_id == department.id,
                RaciAssignment.user_id == user.id,
                RaciAssignment.scope == scope,
                RaciAssignment.raci_role == role,
            )
        )
        if assignment is None:
            tenant_db.add(
                RaciAssignment(
                    company_id=company_id,
                    department_id=department.id,
                    user_id=user.id,
                    scope=scope,
                    raci_role=role,
                    active=True,
                )
            )
    tenant_db.flush()


def _ensure_mailboxes(tenant_db, company_id: int) -> dict[str, Any]:
    from app.db.models import Mailbox

    result = {}
    for address, name in MAILBOXES:
        mailbox = tenant_db.scalar(select(Mailbox).where(Mailbox.company_id == company_id, Mailbox.email_address == address))
        if mailbox is None:
            mailbox = Mailbox(company_id=company_id, email_address=address)
            tenant_db.add(mailbox)
            tenant_db.flush()
        mailbox.name = name
        mailbox.provider = DEMO_MAILBOX_PROVIDER
        mailbox.connection_method = "demo"
        mailbox.connected_email = address
        mailbox.imap_host = None
        mailbox.imap_username = None
        mailbox.imap_password_encrypted = None
        mailbox.smtp_host = None
        mailbox.smtp_username = None
        mailbox.smtp_password_encrypted = None
        mailbox.smtp_enabled = False
        mailbox.auto_sync_enabled = False
        mailbox.enabled = False
        mailbox.from_email = address
        mailbox.from_name = "Empresa Demo"
        result[address] = mailbox
    tenant_db.flush()
    return result


def _ensure_mailbox_sync_states(master_db, company_id: int, mailboxes: dict[str, Any]) -> None:
    from app.master.models import MailboxSyncState

    for mailbox in mailboxes.values():
        state = master_db.scalar(
            select(MailboxSyncState).where(
                MailboxSyncState.company_id == company_id,
                MailboxSyncState.mailbox_id == mailbox.id,
            )
        )
        if state is None:
            state = MailboxSyncState(company_id=company_id, mailbox_id=mailbox.id)
            master_db.add(state)
        state.enabled = False
        state.status = "demo"
        state.sync_status = "disabled"
        state.source_provider = DEMO_MAILBOX_PROVIDER
        state.source_connected_email = mailbox.email_address
        state.next_run_at = None
    master_db.flush()


def _ensure_branding(tenant_db, company_id: int, admin_user_id: int) -> None:
    from app.db.models import BrandingSettings

    branding = tenant_db.scalar(select(BrandingSettings).where(BrandingSettings.company_id == company_id))
    if branding is None:
        branding = BrandingSettings(company_id=company_id)
        tenant_db.add(branding)
    branding.app_name = "KIBAK"
    branding.company_name = DEMO_COMPANY_NAME
    branding.primary_claim = "Comunicaciones y routing inteligente"
    branding.secondary_claim = "Demo operativa con revisión humana"
    branding.short_description = "Empresa demo para mostrar comunicaciones, departamentos y routing."
    branding.updated_by = admin_user_id
    tenant_db.flush()


def _ensure_llm_settings(tenant_db, company_id: int, admin_user_id: int) -> None:
    from app.db.models import LLMSettings

    settings = tenant_db.scalar(select(LLMSettings).where(LLMSettings.company_id == company_id))
    if settings is None:
        settings = LLMSettings(company_id=company_id)
        tenant_db.add(settings)
    settings.agent_enabled = False
    settings.auto_routing_enabled = False
    settings.auto_forwarding_enabled = False
    settings.provider = "demo"
    settings.api_key_encrypted = None
    settings.base_url = None
    settings.classification_model = "demo-static"
    settings.extraction_model = "demo-static"
    settings.validation_model = "demo-static"
    settings.updated_by = admin_user_id
    tenant_db.flush()


def _ensure_communication(tenant_db, company_id: int, mailbox, spec: CommunicationSpec, index: int):
    from app.db.models import Communication

    external_id = f"{DEMO_EXTERNAL_PREFIX}{spec.key}"
    communication = tenant_db.scalar(
        select(Communication).where(
            Communication.company_id == company_id,
            Communication.mailbox_id == mailbox.id,
            Communication.provider == DEMO_MAILBOX_PROVIDER,
            Communication.external_message_id == external_id,
        )
    )
    values = {
        "thread_id": f"{DEMO_EXTERNAL_PREFIX}thread-{spec.key}",
        "sender_email": spec.sender,
        "sender_name": spec.sender.split("@", 1)[0].replace(".", " ").title(),
        "to_recipients": _json([mailbox.email_address]),
        "subject": spec.subject,
        "body_text": spec.body,
        "metadata_json": _json({"demo": True, "scenario": spec.scenario, "source": "kibak_demo_seed"}),
        "received_at": _now(index),
        "processing_status": "processed",
        "routing_status": "routed" if spec.status in {"routed", "corrected"} else "pending_review",
        "updated_at": _now(),
    }
    if communication is None:
        communication = Communication(
            company_id=company_id,
            mailbox_id=mailbox.id,
            external_message_id=external_id,
            provider=DEMO_MAILBOX_PROVIDER,
            created_at=values["received_at"],
            **values,
        )
        tenant_db.add(communication)
    else:
        for field, value in values.items():
            setattr(communication, field, value)
    tenant_db.flush()
    return communication


def _ensure_decision(tenant_db, company_id: int, communication, departments: dict[str, Any], user_id: int, spec: CommunicationSpec):
    from app.db.models import RoutingDecision

    department = departments[spec.department] if spec.department else None
    alternative = departments[spec.alternative_department] if spec.alternative_department else None
    decision = tenant_db.scalar(
        select(RoutingDecision).where(
            RoutingDecision.company_id == company_id,
            RoutingDecision.communication_id == communication.id,
            RoutingDecision.analysis_number == 1,
        )
    )
    if decision is None:
        decision = RoutingDecision(
            company_id=company_id,
            communication_id=communication.id,
            analysis_number=1,
            category=spec.category,
            confidence=spec.confidence,
            requires_review=spec.status not in {"routed"},
            reason=spec.reason,
            ambiguity_reason=spec.ambiguity_reason,
            source="demo",
        )
        tenant_db.add(decision)
    decision.department_id = department.id if department else None
    decision.alternative_department_id = alternative.id if alternative else None
    decision.final_department_id = None
    decision.final_category = None
    decision.category = spec.category
    decision.confidence = spec.confidence
    decision.requires_review = spec.status not in {"routed"}
    decision.reason = spec.reason
    decision.ambiguity_reason = spec.ambiguity_reason
    decision.status = "pending_review" if spec.status in {"pending_review", "corrected"} else "routed"
    decision.source = "demo"
    decision.reviewed_by_user_id = None
    decision.reviewed_at = None
    if spec.status == "routed":
        decision.final_department_id = department.id if department else None
        decision.final_category = spec.category
    if spec.status == "corrected":
        decision.final_department_id = departments[spec.corrected_department].id
        decision.final_category = spec.category
        decision.status = "corrected"
        decision.reviewed_by_user_id = user_id
        decision.reviewed_at = _now()
    tenant_db.flush()
    return decision


def _ensure_correction(tenant_db, company_id: int, communication, decision, departments: dict[str, Any], user_id: int, spec: CommunicationSpec) -> None:
    from app.db.models import RoutingCorrection

    if not spec.corrected_department:
        return
    corrected_department = departments[spec.corrected_department]
    correction = tenant_db.scalar(
        select(RoutingCorrection).where(
            RoutingCorrection.company_id == company_id,
            RoutingCorrection.routing_decision_id == decision.id,
            RoutingCorrection.corrected_department_id == corrected_department.id,
        )
    )
    if correction is None:
        tenant_db.add(
            RoutingCorrection(
                company_id=company_id,
                routing_decision_id=decision.id,
                communication_id=communication.id,
                original_department_id=decision.department_id,
                corrected_department_id=corrected_department.id,
                original_category=decision.category,
                corrected_category=decision.category,
                reason="La revisión humana priorizó la incidencia de entrega.",
                corrected_by_user_id=user_id,
            )
        )
    tenant_db.flush()


def _ensure_action(tenant_db, company_id: int, communication, decision, departments: dict[str, Any], user_id: int, spec: CommunicationSpec) -> None:
    from app.db.models import Department, RoutingAction

    if not spec.action_status:
        return
    target_department_id = decision.final_department_id or decision.department_id
    if target_department_id is None:
        return
    department = tenant_db.get(Department, target_department_id)
    key = f"demo-forward:{spec.key}"
    action = tenant_db.scalar(
        select(RoutingAction).where(
            RoutingAction.company_id == company_id,
            RoutingAction.idempotency_key == key,
        )
    )
    if action is None:
        action = RoutingAction(
            company_id=company_id,
            communication_id=communication.id,
            routing_decision_id=decision.id,
            department_id=target_department_id,
            triggered_by_user_id=user_id,
            action_type="forward",
            source="demo",
            destination_email=department.destination_email,
            idempotency_key=key,
        )
        tenant_db.add(action)
    action.status = spec.action_status
    action.attempt_count = 1 if spec.action_status in {"sent", "failed"} else 0
    action.provider_message_id = f"demo-message-{spec.key}" if spec.action_status == "sent" else None
    action.error_code = "demo_delivery_failed" if spec.action_status == "failed" else None
    action.error_message = "Demo: envío simulado fallido; no se contactó ningún servidor SMTP." if spec.action_status == "failed" else None
    action.completed_at = _now() if spec.action_status in {"sent", "failed"} else None
    tenant_db.flush()


def seed_demo(master_db, tenant_db, *, admin_password: str, user_password: str | None = None, database_url: str = "", environment: str = "development", app_slug: str = "kibak", reset: bool = False, reset_confirmation: str | None = None) -> dict[str, int | str]:
    """Seed the demo into already-created KIBAK master and tenant schemas."""

    demo_runtime_guard(environment, app_slug)
    if not admin_password.strip():
        raise RuntimeError("Falta KIBAK_DEMO_ADMIN_PASSWORD o DEFAULT_ADMIN_PASSWORD para crear usuarios demo.")
    if reset:
        if reset_confirmation != DEMO_RESET_CONFIRMATION:
            raise RuntimeError(f"Reset rechazado: requiere confirmación {DEMO_RESET_CONFIRMATION}.")
        reset_demo(master_db, tenant_db, confirmation=reset_confirmation)

    tenant_company = _ensure_tenant_company(tenant_db, DEMO_COMPANY_ID)
    master_company = _ensure_master_company(master_db, tenant_db)
    if master_company.id != tenant_company.id:
        raise RuntimeError("La empresa demo no coincide entre master y tenant.")

    from app.master.models import MasterTenantDatabase

    database_url = database_url or os.getenv("TENANT_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
    tenant_row = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == master_company.id))
    if tenant_row is None:
        tenant_row = MasterTenantDatabase(
            company_id=master_company.id,
            database_key=DEMO_COMPANY_SLUG,
            database_url=database_url,
            database_type="postgresql" if database_url.startswith("postgresql") else "sqlite",
        )
        master_db.add(tenant_row)
    tenant_row.database_url = database_url or tenant_row.database_url
    tenant_row.database_type = "postgresql" if (tenant_row.database_url or "").startswith("postgresql") else "sqlite"
    tenant_row.is_active = True
    tenant_row.health_status = "ok"
    tenant_row.notes = "KIBAK demo seed; mailbox credentials intentionally absent."
    tenant_row.provisioned_at = tenant_row.provisioned_at or _now()
    master_db.flush()

    password = user_password or admin_password
    master_users = {}
    for email, name, _role, owner in USERS:
        master_user = _ensure_master_user(master_db, email, name, password)
        _ensure_membership(master_db, master_user, master_company, owner=owner)
        master_users[email] = master_user

    tenant_roles = {name: _ensure_tenant_role(tenant_db, tenant_company.id, name) for name in {item[2] for item in USERS}}
    tenant_users = {
        email: _ensure_tenant_user(tenant_db, tenant_company.id, master_users[email], tenant_roles[role])
        for email, _name, role, _owner in USERS
    }
    admin_user = tenant_users[DEMO_ADMIN_EMAIL]
    departments = _ensure_departments(tenant_db, tenant_company.id)
    _ensure_raci(tenant_db, tenant_company.id, departments, tenant_users)
    mailboxes = _ensure_mailboxes(tenant_db, tenant_company.id)
    _ensure_mailbox_sync_states(master_db, tenant_company.id, mailboxes)
    _ensure_branding(tenant_db, tenant_company.id, admin_user.id)
    _ensure_llm_settings(tenant_db, tenant_company.id, admin_user.id)

    mailbox_values = list(mailboxes.values())
    for index, spec in enumerate(COMMUNICATIONS):
        mailbox = mailbox_values[index % len(mailbox_values)]
        communication = _ensure_communication(tenant_db, tenant_company.id, mailbox, spec, index + 1)
        decision = _ensure_decision(tenant_db, tenant_company.id, communication, departments, admin_user.id, spec)
        _ensure_correction(tenant_db, tenant_company.id, communication, decision, departments, admin_user.id, spec)
        _ensure_action(tenant_db, tenant_company.id, communication, decision, departments, admin_user.id, spec)

    tenant_db.commit()
    master_db.commit()
    return {
        "company_id": tenant_company.id,
        "company": DEMO_COMPANY_NAME,
        "departments": len(DEPARTMENTS),
        "mailboxes": len(MAILBOXES),
        "communications": len(COMMUNICATIONS),
        "users": len(USERS),
        "routing_corrections": sum(1 for item in COMMUNICATIONS if item.corrected_department),
        "routing_actions": sum(1 for item in COMMUNICATIONS if item.action_status),
    }


def reset_demo(master_db, tenant_db, *, confirmation: str) -> None:
    """Delete only the marked demo company and its tenant-owned rows."""

    if confirmation != DEMO_RESET_CONFIRMATION:
        raise RuntimeError(f"Reset rechazado: requiere confirmación {DEMO_RESET_CONFIRMATION}.")
    from app.db.models import (
        AuditLog,
        BackgroundJob,
        BrandingSettings,
        Communication,
        CommunicationAttachment,
        Company,
        Department,
        DepartmentKnowledge,
        DepartmentMember,
        LLMSettings,
        Mailbox,
        RaciAssignment,
        Role,
        RoutingAction,
        RoutingCorrection,
        RoutingDecision,
        User,
    )
    from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser, MailboxSyncState

    tenant_company = tenant_db.get(Company, DEMO_COMPANY_ID)
    master_company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == DEMO_COMPANY_SLUG))
    if tenant_company and tenant_company.name != DEMO_COMPANY_NAME and tenant_company.plan != "demo":
        raise RuntimeError("Reset rechazado: la empresa tenant no está marcada como demo.")
    if master_company is None and tenant_company is None:
        return
    company_id = master_company.id if master_company else tenant_company.id
    mailbox_ids = [row[0] for row in tenant_db.execute(select(Mailbox.id).where(Mailbox.company_id == company_id)).all()]
    tenant_db.query(RoutingAction).filter(RoutingAction.company_id == company_id).delete(synchronize_session=False)
    tenant_db.query(RoutingCorrection).filter(RoutingCorrection.company_id == company_id).delete(synchronize_session=False)
    tenant_db.query(RoutingDecision).filter(RoutingDecision.company_id == company_id).delete(synchronize_session=False)
    tenant_db.query(BackgroundJob).filter(BackgroundJob.company_id == company_id).delete(synchronize_session=False)
    communication_ids = [row[0] for row in tenant_db.execute(select(Communication.id).where(Communication.company_id == company_id)).all()]
    if communication_ids:
        tenant_db.query(CommunicationAttachment).filter(CommunicationAttachment.communication_id.in_(communication_ids)).delete(synchronize_session=False)
    tenant_db.query(Communication).filter(Communication.company_id == company_id).delete(synchronize_session=False)
    tenant_db.query(AuditLog).filter(AuditLog.company_id == company_id).delete(synchronize_session=False)
    tenant_db.query(RaciAssignment).filter(RaciAssignment.company_id == company_id).delete(synchronize_session=False)
    tenant_db.query(DepartmentMember).filter(DepartmentMember.company_id == company_id).delete(synchronize_session=False)
    department_ids = [row[0] for row in tenant_db.execute(select(Department.id).where(Department.company_id == company_id)).all()]
    if department_ids:
        tenant_db.query(DepartmentKnowledge).filter(DepartmentKnowledge.department_id.in_(department_ids)).delete(synchronize_session=False)
    tenant_db.query(Department).filter(Department.company_id == company_id).delete(synchronize_session=False)
    tenant_db.query(Mailbox).filter(Mailbox.company_id == company_id).delete(synchronize_session=False)
    tenant_db.query(LLMSettings).filter(LLMSettings.company_id == company_id).delete(synchronize_session=False)
    tenant_db.query(BrandingSettings).filter(BrandingSettings.company_id == company_id).delete(synchronize_session=False)
    tenant_db.query(User).filter(User.company_id == company_id).delete(synchronize_session=False)
    tenant_db.query(Role).filter(Role.company_id == company_id).delete(synchronize_session=False)
    if tenant_company:
        tenant_db.delete(tenant_company)
    tenant_db.flush()

    if mailbox_ids:
        master_db.query(MailboxSyncState).filter(MailboxSyncState.company_id == company_id, MailboxSyncState.mailbox_id.in_(mailbox_ids)).delete(synchronize_session=False)
    user_ids = [row[0] for row in master_db.execute(select(CompanyMembership.user_id).where(CompanyMembership.company_id == company_id)).all()]
    master_db.query(CompanyMembership).filter(CompanyMembership.company_id == company_id).delete(synchronize_session=False)
    master_db.query(MasterTenantDatabase).filter(MasterTenantDatabase.company_id == company_id).delete(synchronize_session=False)
    if master_company:
        master_db.delete(master_company)
        for user_id in user_ids:
            if master_db.scalar(select(CompanyMembership.id).where(CompanyMembership.user_id == user_id)) is None:
                user = master_db.get(MasterUser, user_id)
                if user:
                    master_db.delete(user)
    tenant_db.commit()
    master_db.commit()


def _sessions_from_settings():
    os.chdir(BACKEND_DIR)
    from app.core.config import get_settings
    from app.db.database import SessionLocal
    from app.master.database import MasterSessionLocal

    settings = get_settings()
    demo_runtime_guard(settings.environment, settings.app_slug)
    password = os.getenv("KIBAK_DEMO_ADMIN_PASSWORD") or settings.default_admin_password
    if not password:
        raise RuntimeError("Define KIBAK_DEMO_ADMIN_PASSWORD o DEFAULT_ADMIN_PASSWORD antes de ejecutar el seed.")
    return MasterSessionLocal(), SessionLocal(), password, settings.database_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Carga datos demo realistas para KIBAK.")
    parser.add_argument("command", choices=("seed", "reset-seed"))
    parser.add_argument("--confirm-reset", default="", help=f"Obligatorio para reset-seed: {DEMO_RESET_CONFIRMATION}")
    args = parser.parse_args(argv)
    master_db = tenant_db = None
    try:
        master_db, tenant_db, password, database_url = _sessions_from_settings()
        summary = seed_demo(
            master_db,
            tenant_db,
            admin_password=password,
            user_password=os.getenv("KIBAK_DEMO_USER_PASSWORD") or password,
            database_url=database_url,
            environment=os.getenv("APP_ENV", "development"),
            app_slug=os.getenv("APP_SLUG", "kibak"),
            reset=args.command == "reset-seed",
            reset_confirmation=args.confirm_reset,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        if master_db is not None:
            master_db.rollback()
        if tenant_db is not None:
            tenant_db.rollback()
        print(f"KIBAK demo seed no ejecutado: {exc}", file=sys.stderr)
        return 1
    finally:
        if master_db is not None:
            master_db.close()
        if tenant_db is not None:
            tenant_db.close()


if __name__ == "__main__":
    raise SystemExit(main())
