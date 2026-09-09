from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.config import get_settings
from app.core.templating import templates
from app.db.models import BrandingSettings, Company, Department, DepartmentKnowledge, LLMSettings, Mailbox
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db
from app.routing.policy import build_kibak_readiness
from app.settings.kibak_ai import credential_configured


router = APIRouter(prefix="/onboarding", tags=["onboarding"])


def _require_kibak() -> None:
    if get_settings().app_slug.strip().lower() != "kibak":
        raise HTTPException(status_code=404, detail="No encontrado")


def _step(key: str, label: str, detail: str, href: str, *, complete: bool, status: str | None = None) -> dict:
    return {
        "key": key,
        "label": label,
        "detail": detail,
        "href": href,
        "complete": complete,
        "status": status or ("Completado" if complete else "Pendiente"),
    }


@router.get("")
def onboarding_page(
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    _require_kibak()
    company = db.get(Company, user.company_id)
    branding = db.scalar(select(BrandingSettings).where(BrandingSettings.company_id == user.company_id))
    department_count = db.scalar(
        select(func.count(Department.id)).where(Department.company_id == user.company_id, Department.active.is_(True))
    ) or 0
    knowledge_count = db.scalar(
        select(func.count(DepartmentKnowledge.id))
        .join(Department, Department.id == DepartmentKnowledge.department_id)
        .where(Department.company_id == user.company_id, DepartmentKnowledge.active.is_(True))
    ) or 0
    mailbox_count = db.scalar(select(func.count(Mailbox.id)).where(Mailbox.company_id == user.company_id, Mailbox.enabled.is_(True))) or 0
    llm = db.scalar(select(LLMSettings).where(LLMSettings.company_id == user.company_id))
    readiness = build_kibak_readiness(db, user.company_id)

    company_ready = bool(
        company
        and company.name
        and company.country
        and company.language
        and company.timezone
        and branding
        and branding.company_name
    )
    ai_ready = bool(llm and llm.agent_enabled and llm.provider and credential_configured(llm))
    automation_enabled = bool(llm and (llm.auto_routing_enabled or llm.auto_forwarding_enabled))
    steps = [
        _step("company", "Empresa", company.name if company_ready else "Completa los datos básicos de la empresa.", "/settings#company", complete=company_ready),
        _step("departments", "Departamentos", f"{department_count} activos" if department_count else "Crea el primer departamento operativo.", "/departments", complete=department_count > 0),
        _step("knowledge", "Conocimiento", f"{knowledge_count} elementos activos" if knowledge_count else "Añade responsabilidades, exclusiones y ejemplos.", "/departments", complete=knowledge_count > 0),
        _step("mailboxes", "Buzones", f"{mailbox_count} buzones activos" if mailbox_count else "Conecta el primer buzón cuando estés listo.", "/settings/mailboxes", complete=mailbox_count > 0),
        _step("ai", "Inteligencia artificial", f"{llm.provider} configurado" if ai_ready else "Configura el proveedor y el modelo del tenant.", "/settings/ai", complete=ai_ready),
        _step(
            "automation",
            "Automatización",
            "Activa cuando quieras automatizar routing y derivaciones." if not automation_enabled else "Automatización configurada para este tenant.",
            "/settings#automation",
            complete=not automation_enabled,
            status="Activa" if automation_enabled else "Desactivada",
        ),
    ]
    completed = sum(1 for item in steps if item["complete"])
    return templates.TemplateResponse(
        "onboarding/index.html",
        {
            "request": request,
            "user": user,
            "title": "Primeros pasos",
            "steps": steps,
            "completed_steps": completed,
            "total_steps": len(steps),
            "progress_percent": round(completed * 100 / len(steps)) if steps else 0,
            "next_step": next((item for item in steps if not item["complete"]), None),
            "readiness": readiness,
        },
    )
