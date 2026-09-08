import logging
from datetime import datetime, timezone
from datetime import date
from urllib.parse import urlsplit, urlunsplit
from time import perf_counter

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.core.config import get_settings
from app.core.middleware import invalidate_branding_cache
from app.auth.dependencies import current_user
from app.agent.model_catalog import DEFAULT_OPENAI_MODEL, LEGACY_OPENAI_MODEL_FALLBACK, openai_model_description, openai_model_label, openai_model_option_payload, OPENAI_MODEL_PRESET_VALUES, resolve_openai_runtime_model
from app.master.database import get_master_db
from app.master.service import TenantUser
from app.master.models import EmailSyncState
from app.core.encryption import mask_secret
from app.db.models import AuditLog, BrandingSettings, Company, Customer, DecisionSettings, Department, DepartmentKnowledge, Email, EmailSettings, EmailTemplate, ExportSettings, FTPSettings, InputChannel, InboundMessage, LLMSettings, Mailbox, Order, Product, PromptExecution, PromptTemplate, PromptVersion, ScoringSettings
from app.db.models import BackgroundJob
from app.logs.service import log_action
from app.settings.agent_config import agent_metrics, agent_status, apply_safety_level, improvement_suggestions
from app.settings.autoconfig import detect_email_configuration
from app.settings.branding import branding_to_dict, delete_brand_asset, get_or_create_branding, reset_branding, store_brand_asset, update_branding_from_form
from app.settings.email_config import TEMPLATE_VARIABLES, email_config_status, email_templates, ensure_default_email_templates, serialize_email_settings
from app.settings.integrations import classify_sample, extract_sample, preview_initial_imap_sync, run_initial_imap_sync, send_test_email, test_imap_connection, test_smtp_connection
from app.settings.application import run_connection_test, update_settings_section_async
from app.settings.service import get_or_create_settings, resolve_updated_by_id, update_with_form
from app.dashboard.service import recent_processed_emails_overview
from app.jobs.service import enqueue_job, execute_job_inline, job_payload
from app.tenancy.database import get_tenant_db
from app.tenancy.migrations import tenant_migration_report
from app.routing.policy import (
    RoutingPolicyError,
    build_kibak_readiness,
    load_routing_policy,
    parse_policy_form,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/diagnostics/prompts")
def prompt_execution_diagnostics(
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    executions = db.scalars(
        select(PromptExecution)
        .where(
            PromptExecution.company_id == user.company_id,
            PromptExecution.prompt_purpose.in_(["classification", "extraction"]),
        )
        .order_by(PromptExecution.id.desc())
        .limit(6)
    ).all()

    return JSONResponse(
        {
            "company_id": user.company_id,
            "items": [
                {
                    "id": execution.id,
                    "purpose": execution.prompt_purpose,
                    "status": execution.output_status,
                    "model": execution.model,
                    "validation_errors": execution.validation_errors_json,
                    "duration_ms": execution.duration_ms,
                    "response_excerpt": execution.response_excerpt,
                    "created_at": execution.created_at.isoformat() if execution.created_at else None,
                }
                for execution in executions
            ],
        }
    )


def _queued_job_response(request: Request, job_id: int, fallback: str = "/settings", result: dict | None = None):
    if "application/json" in (request.headers.get("accept") or ""):
        payload = {"ok": True, "job_id": job_id, "status": "queued", "message": "Trabajo encolado correctamente"}
        if result is not None:
            payload["status"] = "success" if result.get("ok") else "failed"
            payload["result"] = result
            payload["message"] = result.get("message") or payload["message"]
        return JSONResponse(payload)
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _sync_email_sync_state(master_db: Session, user: TenantUser, settings: EmailSettings) -> None:
    state = master_db.scalar(
        select(EmailSyncState).where(
            EmailSyncState.company_id == user.company_id,
            EmailSyncState.channel_key == "email",
        )
    )
    if not state:
        state = EmailSyncState(
            company_id=user.company_id,
            channel_key="email",
            enabled=bool(settings.auto_sync_enabled),
            frequency_seconds=60,
            status="idle",
        )
        master_db.add(state)
    state.enabled = bool(settings.auto_sync_enabled)
    try:
        state.frequency_seconds = max(int(settings.polling_frequency_minutes or 1), 1) * 60
    except (TypeError, ValueError):
        state.frequency_seconds = 60
    state.mailbox = settings.mailbox or settings.inbox_folder or "INBOX"
    state.source_provider = (settings.provider or "imap").strip().lower() or "imap"
    state.source_host = (settings.imap_host or "").strip() or None
    state.source_username = (settings.imap_username or "").strip() or None
    state.source_connected_email = (settings.connected_email or settings.imap_username or "").strip() or None
    state.updated_at = datetime.now(timezone.utc)
    master_db.commit()


def _run_email_sync_job_if_needed(request: Request, db: Session, user: TenantUser, job) -> dict | None:  # noqa: ANN001
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "settings.email.read.inline.start",
        extra={"event": "settings.email.read.inline.start", "request_id": request_id, "company_id": user.company_id, "user_id": user.id, "job_id": job.id, "job_type": job.job_type},
    )
    result = execute_job_inline(db, job)
    logger.info(
        "settings.email.read.inline.end",
        extra={
            "event": "settings.email.read.inline.end",
            "request_id": request_id,
            "company_id": user.company_id,
            "user_id": user.id,
            "job_id": job.id,
            "job_type": job.job_type,
            "ok": bool(result.get("ok")),
            "found": result.get("found"),
            "saved": result.get("saved"),
            "duplicates": result.get("duplicates"),
            "errors": result.get("errors"),
        },
    )
    return result


def _backfill_job_response_payload(job: BackgroundJob, result: dict, *, from_date: str | None, to_date: str | None) -> dict:
    safe_result = result if isinstance(result, dict) else {}
    continuation_job_id = safe_result.get("continuation_job_id")
    has_more = bool(safe_result.get("has_more"))
    remaining_messages = max(int(safe_result.get("remaining_messages", safe_result.get("remaining") or 0) or 0), 0)
    remaining_limit = max(int(safe_result.get("remaining_limit", safe_result.get("remaining") or 0) or 0), 0)
    total_found = max(int(safe_result.get("total_found", safe_result.get("found") or 0) or 0), 0)
    batch_count = max(int(safe_result.get("batch_count") or 0), 0)
    return {
        "ok": bool(safe_result.get("ok", True)),
        "status": "success" if safe_result.get("ok", True) else "failed",
        "job_id": job.id,
        "continuation_job_id": continuation_job_id,
        "has_more": has_more,
        "remaining": remaining_messages,
        "remaining_messages": remaining_messages,
        "remaining_limit": remaining_limit,
        "batch_count": batch_count,
        "total_found": total_found,
        "found": total_found,
        "saved": int(safe_result.get("saved") or safe_result.get("imported") or 0),
        "duplicates": int(safe_result.get("duplicates") or 0),
        "errors": int(safe_result.get("errors") or 0),
        "from_date": from_date,
        "to_date": to_date,
        "message": safe_result.get("message") or "Backfill IMAP completado",
    }


def _backfill_response(request: Request, payload: dict, fallback: str = "/settings#email-diagnostics"):
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(payload)
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _is_kibak_runtime(db: Session) -> bool:
    get_bind = getattr(db, "get_bind", None)
    if not callable(get_bind):
        return False
    try:
        return get_settings().app_slug.strip().lower() == "kibak" and get_bind().dialect.name == "postgresql"
    except AttributeError:
        return False


def _kibak_settings_context(request: Request, db: Session, user: TenantUser) -> dict:
    company = db.get(Company, user.company_id)
    branding = db.scalar(select(BrandingSettings).where(BrandingSettings.company_id == user.company_id))
    llm = db.scalar(select(LLMSettings).where(LLMSettings.company_id == user.company_id))
    prompt_count = db.scalar(select(func.count(PromptTemplate.id)).where(PromptTemplate.company_id == user.company_id)) or 0
    execution_count = db.scalar(select(func.count(PromptExecution.id)).where(PromptExecution.company_id == user.company_id)) or 0
    mailbox_count = db.scalar(select(func.count(Mailbox.id)).where(Mailbox.company_id == user.company_id)) or 0
    department_count = db.scalar(select(func.count(Department.id)).where(Department.company_id == user.company_id)) or 0
    knowledge_count = db.scalar(
        select(func.count(DepartmentKnowledge.id)).join(
            Department, Department.id == DepartmentKnowledge.department_id
        ).where(Department.company_id == user.company_id)
    ) or 0
    provider = (llm.provider if llm else "disabled") or "disabled"
    model = (llm.classification_model if llm else "") or "Sin modelo configurado"
    policy = load_routing_policy(db, user.company_id)
    return {
        "request": request,
        "user": user,
        "company": company,
        "branding": branding,
        "llm": llm,
        "llm_provider": provider,
        "llm_model": model,
        "llm_configured": bool(llm and llm.api_key_encrypted and provider != "disabled"),
        "automation": {
            "agent_enabled": bool(llm and llm.agent_enabled),
            **policy.as_dict(),
        },
        "readiness": build_kibak_readiness(db, user.company_id),
        "counts": {
            "mailboxes": mailbox_count,
            "departments": department_count,
            "knowledge": knowledge_count,
            "prompt_templates": prompt_count,
            "prompt_executions": execution_count,
        },
        "title": "Configuración",
    }


def _run_backfill_job_once(request: Request, db: Session, user: TenantUser, job: BackgroundJob, *, from_date: str | None, to_date: str | None) -> dict:
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "settings.email.backfill.inline.start",
        extra={
            "event": "settings.email.backfill.inline.start",
            "request_id": request_id,
            "company_id": user.company_id,
            "user_id": user.id,
            "job_id": job.id,
            "job_type": job.job_type,
        },
    )
    result = execute_job_inline(db, job)
    if not isinstance(result, dict):
        result = {"ok": True, "message": str(result) if result is not None else "Trabajo completado"}
    logger.info(
        "settings.email.backfill.inline.end",
        extra={
            "event": "settings.email.backfill.inline.end",
            "request_id": request_id,
            "company_id": user.company_id,
            "user_id": user.id,
            "job_id": job.id,
            "job_type": job.job_type,
            "ok": bool(result.get("ok")),
            "found": result.get("found"),
            "saved": result.get("saved"),
            "duplicates": result.get("duplicates"),
            "errors": result.get("errors"),
            "continuation_job_id": result.get("continuation_job_id"),
        },
    )
    return _backfill_job_response_payload(job, result, from_date=from_date, to_date=to_date)


def _parse_date_input(raw_value: str | None) -> date | None:
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        return None


def _normalize_receive_form(data: dict) -> dict:
    for field in ["auto_sync_enabled", "read_unread_only", "mark_as_read_after_import", "move_after_processing", "imap_use_ssl"]:
        data.setdefault(field, "off")
    data.setdefault("initial_history_mode", "new")
    data.setdefault("initial_history_limit", "50")
    return data


def _clone_email_settings_for_preview(db: Session, company_id: int, data: dict) -> EmailSettings:
    current = get_or_create_settings(db, EmailSettings, company_id)
    preview = EmailSettings(company_id=company_id)
    for column in EmailSettings.__table__.columns:
        if column.name in {"id", "company_id"}:
            continue
        if hasattr(current, column.name):
            setattr(preview, column.name, getattr(current, column.name))
    update_with_form(preview, data, {"imap_password_encrypted", "client_secret_encrypted", "access_token_encrypted", "refresh_token_encrypted"})
    try:
        preview.initial_history_limit = max(min(int(preview.initial_history_limit or 50), 100), 1)
    except (TypeError, ValueError):
        preview.initial_history_limit = 50
    preview.initial_history_mode = preview.initial_history_mode if preview.initial_history_mode in {"new", "7d", "30d", "100", "custom"} else "new"
    return preview


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if _is_kibak_runtime(db):
        return templates.TemplateResponse("settings/kibak.html", _kibak_settings_context(request, db, user))
    llm_settings = get_or_create_settings(db, LLMSettings, user.company_id)
    scoring_settings = get_or_create_settings(db, ScoringSettings, user.company_id)
    decision_settings = get_or_create_settings(db, DecisionSettings, user.company_id)
    metrics = agent_metrics(db, user.company_id, scoring_settings)
    prompt_templates = db.scalars(select(PromptTemplate).where(PromptTemplate.company_id == user.company_id).order_by(PromptTemplate.purpose)).all()
    prompt_versions = {
        template.id: db.scalars(select(PromptVersion).where(PromptVersion.template_id == template.id).order_by(PromptVersion.version.desc())).all()
        for template in prompt_templates
    }
    context = {
        "request": request,
        "user": user,
        "company": db.get(Company, user.company_id),
        "email": get_or_create_settings(db, EmailSettings, user.company_id),
        "email_status": email_config_status(get_or_create_settings(db, EmailSettings, user.company_id)),
        "email_templates": email_templates(db, user.company_id),
        "email_template_variables": TEMPLATE_VARIABLES,
        "can_edit_email": user.role.name == "Administrador",
        "can_test_email": user.role.name in {"Administrador", "Supervisor"},
        "llm": llm_settings,
        "llm_provider_options": [
            {"value": "openai", "label": "OpenAI"},
            {"value": "openai_compatible", "label": "Compatible OpenAI"},
            {"value": "azure_openai", "label": "Azure OpenAI"},
            {"value": "disabled", "label": "Desactivado"},
        ],
        "agent_status": agent_status(llm_settings, metrics),
        "agent_metrics": metrics,
        "agent_improvements": improvement_suggestions(db, user.company_id),
        "extraction_model_options": openai_model_option_payload(),
        "extraction_model_values": sorted(OPENAI_MODEL_PRESET_VALUES),
        "extraction_model_value": resolve_openai_runtime_model(llm_settings.extraction_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK),
        "extraction_model_label": openai_model_label(resolve_openai_runtime_model(llm_settings.extraction_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK)),
        "extraction_model_description": openai_model_description(resolve_openai_runtime_model(llm_settings.extraction_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK)),
        "ftp": get_or_create_settings(db, FTPSettings, user.company_id),
        "export": get_or_create_settings(db, ExportSettings, user.company_id),
        "scoring": scoring_settings,
        "decision": decision_settings,
        "branding": branding_to_dict(get_or_create_branding(db, user.company_id)),
        "can_edit_branding": user.role.name == "Administrador",
        "prompts": prompt_templates,
        "prompt_versions": prompt_versions,
        "mask_secret": mask_secret,
        "is_superadmin": user.role.name == "Superadmin",
        "recent_processed_emails": recent_processed_emails_overview(db, user.company_id, days=30, limit=8),
        "diagnostics": build_environment_diagnostics(db, user),
        "dashboard": build_settings_dashboard(db, user, metrics, llm_settings, scoring_settings),
    }
    return templates.TemplateResponse("settings/index.html", context)


def build_settings_dashboard(db: Session, user: TenantUser, metrics: dict, llm: LLMSettings, scoring: ScoringSettings) -> dict:
    company = db.get(Company, user.company_id)
    branding = get_or_create_branding(db, user.company_id)
    email = get_or_create_settings(db, EmailSettings, user.company_id)
    email_status = email_config_status(email)
    ftp = get_or_create_settings(db, FTPSettings, user.company_id)
    export = get_or_create_settings(db, ExportSettings, user.company_id)
    decision = get_or_create_settings(db, DecisionSettings, user.company_id)
    customer_count = db.query(Customer).filter(Customer.company_id == user.company_id).count()
    product_count = db.query(Product).filter(Product.company_id == user.company_id).count()
    active_channels_count = db.query(InputChannel).filter(InputChannel.company_id == user.company_id, InputChannel.is_active == True).count()  # noqa: E712
    prompt_templates = db.scalars(select(PromptTemplate).where(PromptTemplate.company_id == user.company_id)).all()

    def state(key: str, label: str, kind: str, summary: str, action: str) -> dict:
        return {"key": key, "label": label, "state": kind, "summary": summary, "action": action}

    modules = [
        state(
            "general",
            "General",
            "ready" if company and company.name else "pending",
            f"{company.name if company else 'Sin empresa'} · {(company.currency or 'EUR') if company else 'EUR'} · {('activa' if company and company.active else 'inactiva')}",
            "Configurar",
        ),
        state("identity", "Identidad", "ready" if branding.app_name and (branding.logo_url or branding.dark_logo_url) else "warning" if branding.app_name else "pending", f"{branding.app_name} · {branding.secondary_claim or 'sin claim secundario'}", "Configurar"),
        state("channels", "Canales", "ready" if active_channels_count else "pending", f"{active_channels_count} canal activo" if active_channels_count == 1 else f"{active_channels_count} canales activos", "Abrir"),
        state("ai", "Agente IA", "ready" if llm.provider != "disabled" and llm.api_key_encrypted and llm.last_test_ok is not False else "warning" if llm.api_key_encrypted else "pending", f"{llm.provider or 'sin proveedor'} · {resolve_openai_runtime_model(llm.extraction_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK)} · {llm.last_test_message or 'sin prueba reciente'}", "Configurar"),
        state("customers-products", "Clientes y productos", "ready" if customer_count and product_count else "warning" if customer_count or product_count else "pending", f"{customer_count} clientes · {product_count} productos", "Abrir"),
        state("scoring", "Confianza y automatización", "ready", f"Alta confianza desde {scoring.safe_threshold}% · auto-confirmar {'sí' if llm.allow_auto_confirm else 'no'}", "Configurar"),
        state("decision", "Motor de decisión", "ready" if decision.enable_exact_match else "warning", f"Prioridad {decision.exact_priority} a {decision.llm_priority} · modo {decision.learning_mode}", "Configurar"),
        state("export", "Exportación", "ready" if export.file_type and export.filename_template else "pending", f"{export.file_type.upper() if export.file_type else 'Sin formato'} · {export.filename_template or 'sin plantilla'}", "Configurar"),
        state("ftp", "FTP/SFTP", "ready" if ftp.host and ftp.username else "pending", f"{ftp.connection_type.upper()} · {ftp.host or 'host pendiente'}", "Configurar"),
        state("alerts", "Alertas", "ready", f"{metrics['llm_errors']} errores · {metrics['doubtful_emails']} dudosos", "Ver"),
        state("users", "Usuarios y permisos", "ready", "Roles y accesos activos", "Abrir"),
        state("advanced", "Avanzado", "optional" if user.role.name == "Superadmin" else "locked", f"{len(prompt_templates)} prompts · logs técnicos", "Abrir"),
    ]
    visible_modules = [module for module in modules if module["state"] != "locked"]
    configured = len([module for module in visible_modules if module["state"] in {"ready", "warning"}])
    pending = len([module for module in visible_modules if module["state"] == "pending"])
    errors = len([module for module in visible_modules if module["state"] == "error"])
    progress = round((configured * 100) / len(visible_modules)) if visible_modules else 0
    module_map = {module["key"]: module for module in modules}
    checklist = [
        {"key": "general", "label": "Empresa e identidad básica", "state": "done" if module_map.get("general", {}).get("state") == "ready" and module_map.get("identity", {}).get("state") in ("ready", "warning") else "pending", "open_settings": "general"},
        {"key": "channels", "label": "Canal de entrada conectado", "state": "done" if module_map.get("channels", {}).get("state") == "ready" else "pending", "url": "/settings/channels"},
        {"key": "ai", "label": "Agente IA configurado", "state": "done" if module_map.get("ai", {}).get("state") in ("ready", "warning") else "pending", "open_settings": "ai"},
        {"key": "customers-products", "label": "Clientes y productos cargados", "state": "done" if module_map.get("customers-products", {}).get("state") in ("ready", "warning") else "pending", "url": "/products"},
        {"key": "scoring", "label": "Scoring definido", "state": "done" if module_map.get("scoring", {}).get("state") == "ready" else "pending", "open_settings": "scoring"},
        {"key": "decision", "label": "Motor de decisión activo", "state": "done" if module_map.get("decision", {}).get("state") in ("ready", "warning") else "pending", "open_settings": "decision"},
        {"key": "export", "label": "Exportación configurada", "state": "done" if module_map.get("export", {}).get("state") == "ready" else "pending", "open_settings": "export"},
        {"key": "ftp", "label": "FTP/SFTP configurado", "state": "done" if module_map.get("ftp", {}).get("state") == "ready" else "pending", "open_settings": "ftp"},
    ]
    return {
        "progress": progress,
        "configured": configured,
        "pending": pending,
        "errors": errors,
        "modules": modules,
        "checklist": checklist,
        "customer_count": customer_count,
        "product_count": product_count,
        "active_channels_count": active_channels_count,
        "prompt_templates": prompt_templates,
        "email_status": email_status,
        "email": email,
        "llm": llm,
        "ftp": ftp,
        "export": export,
        "decision": decision,
        "branding": branding,
        "company": company,
        "ai_module": module_map.get("ai"),
        "scoring_module": module_map.get("scoring"),
    }


def build_environment_diagnostics(db: Session, user: TenantUser) -> dict:
    company = db.get(Company, user.company_id)
    email_settings = get_or_create_settings(db, EmailSettings, user.company_id)
    llm_settings = get_or_create_settings(db, LLMSettings, user.company_id)
    last_seed_at = db.scalar(
        select(func.max(AuditLog.created_at)).where(AuditLog.company_id == user.company_id, AuditLog.action == "demo.seed")
    )
    customers_total = db.scalar(select(func.count(Customer.id)).where(Customer.company_id == user.company_id)) or 0
    products_total = db.scalar(select(func.count(Product.id)).where(Product.company_id == user.company_id)) or 0
    orders_total = db.scalar(select(func.count(Order.id)).where(Order.company_id == user.company_id)) or 0
    emails_total = db.scalar(select(func.count(Email.id)).where(Email.company_id == user.company_id)) or 0
    processed_emails_total = db.scalar(select(func.count(Email.id)).where(Email.company_id == user.company_id, Email.agent_status != "not_processed")) or 0
    inbound_total = db.scalar(select(func.count(InboundMessage.id)).where(InboundMessage.company_id == user.company_id)) or 0
    active_channels_total = db.scalar(select(func.count(InputChannel.id)).where(InputChannel.company_id == user.company_id, InputChannel.is_active == True)) or 0  # noqa: E712
    jobs_total = db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id)) or 0
    jobs_queued = db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "queued")) or 0
    jobs_running = db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "running")) or 0
    jobs_retrying = db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "retrying")) or 0
    jobs_failed = db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "failed")) or 0
    jobs_cancelled = db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "cancelled")) or 0
    email_status = email_config_status(email_settings)
    llm_ready = bool(llm_settings.provider and llm_settings.provider != "disabled" and llm_settings.api_key_encrypted and llm_settings.last_test_ok is not False)
    migration_report = tenant_migration_report(db, user.company_id)
    database_url = get_settings().database_url
    split = urlsplit(database_url)
    if split.password:
        netloc = split.netloc.replace(split.password, "***")
        database_url = urlunsplit((split.scheme, netloc, split.path, split.query, split.fragment))
    return {
        "environment": get_settings().environment,
        "database_url": database_url,
        "database_label": split.path.rsplit("/", 1)[-1] if split.path else database_url,
        "company_id": user.company_id,
        "company_name": company.name if company else "",
        "company_active": bool(company.active) if company else False,
        "user_id": user.id,
        "email": user.email,
        "role": user.role.name,
        "app_name": get_settings().app_name,
        "last_seed_at": last_seed_at,
        "imap_ready": email_status["imap_ready"],
        "smtp_ready": email_status["smtp_ready"],
        "last_imap_test_ok": email_settings.last_imap_test_ok,
        "last_imap_test_at": email_settings.last_imap_test_at,
        "last_sync_at": email_settings.last_sync_at,
        "last_sync_ok": email_settings.last_sync_ok,
        "llm_provider": llm_settings.provider,
        "llm_ready": llm_ready,
        "tenant_schema_version": migration_report["version"],
        "tenant_schema_name": migration_report["name"],
        "tenant_schema_checksum": migration_report["checksum"],
        "tenant_schema_execution_ms": migration_report["execution_ms"],
        "tenant_schema_application_version": migration_report["application_version"],
        "tenant_schema_current_version": migration_report["current_version"],
        "tenant_schema_current_name": migration_report["current_name"],
        "tenant_schema_current_checksum": migration_report["current_checksum"],
        "tenant_schema_status": migration_report["status"],
        "tenant_schema_last_checked_at": migration_report["last_checked_at"],
        "tenant_schema_applied_at": migration_report["applied_at"],
        "tenant_schema_last_error": migration_report["last_error"],
        "tenant_schema_is_current": migration_report["is_current"],
        "customers_total": customers_total,
        "products_total": products_total,
        "orders_total": orders_total,
        "emails_total": emails_total,
        "processed_emails_total": processed_emails_total,
        "inbound_total": inbound_total,
        "active_channels_total": active_channels_total,
        "jobs_total": jobs_total,
        "jobs_queued": jobs_queued,
        "jobs_running": jobs_running,
        "jobs_retrying": jobs_retrying,
        "jobs_failed": jobs_failed,
        "jobs_cancelled": jobs_cancelled,
        "jobs_success": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "success")) or 0,
    }


def can_edit_email_settings(user: TenantUser) -> bool:
    return user.role.name == "Administrador"


def can_test_email_settings(user: TenantUser) -> bool:
    return user.role.name in {"Administrador", "Supervisor"}


async def request_data(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    return dict(await request.form())


def save_email_section(db: Session, settings: EmailSettings, data: dict, user: TenantUser, fields: list[str], secret_fields: set[str] | None = None) -> None:
    update_with_form(settings, {field: data.get(field, "") for field in fields if field in data or field in (secret_fields or set())}, secret_fields)
    settings.updated_by = resolve_updated_by_id(db, user)
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()


def redirect_or_json(request: Request, payload: dict, anchor: str = "email"):
    if request.method == "PUT" or "application/json" in request.headers.get("content-type", ""):
        return JSONResponse(payload)
    referer = request.headers.get("referer", "")
    if "channels" in referer:
        return RedirectResponse("/settings/channels?focus=email", status_code=303)
    return RedirectResponse(f"/settings#{anchor}", status_code=303)


@router.get("/email")
def get_email_settings(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return JSONResponse(serialize_email_settings(db, user.company_id))


@router.post("/email/autoconfig")
def autoconfigure_email(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    user: TenantUser = Depends(current_user),
):
    """Discover and verify a mailbox without persisting its password."""

    if user.role.name not in {"Administrador", "Superadmin"}:
        return JSONResponse({"ok": False, "message": "Solo Administrador puede configurar el correo."}, status_code=403)
    try:
        result = detect_email_configuration(email, password)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    except Exception:  # noqa: BLE001
        request_id = getattr(request.state, "request_id", None) if request is not None else None
        logger.exception(
            "settings.email.autoconfig.failed",
            extra={
                "event": "settings.email.autoconfig.failed",
                "request_id": request_id,
                "company_id": user.company_id,
                "user_id": user.id,
            },
        )
        return JSONResponse(
            {"ok": False, "message": "No se ha podido comprobar la configuración de correo en este momento."},
            status_code=502,
        )
    logger.info(
        "settings.email.autoconfig.completed",
        extra={
            "event": "settings.email.autoconfig.completed",
            "request_id": getattr(request.state, "request_id", None) if request is not None else None,
            "company_id": user.company_id,
            "user_id": user.id,
            "domain": result.get("domain"),
            "provider": result.get("provider"),
            "detected": result.get("detected"),
            "can_use_in_anchi": result.get("can_use_in_anchi"),
        },
    )
    return JSONResponse(result)


@router.api_route("/email/receive", methods=["PUT", "POST"])
async def update_email_receive(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar la configuracion de correo."}, status_code=403)
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "settings.email.receive.start",
        extra={
            "event": "settings.email.receive.start",
            "request_id": request_id,
            "company_id": user.company_id,
            "user_id": user.id,
        },
    )
    data = _normalize_receive_form(await request_data(request))
    logger.info(
        "settings.email.receive.form_parsed",
        extra={
            "event": "settings.email.receive.form_parsed",
            "request_id": request_id,
            "company_id": user.company_id,
            "provider": (data.get("provider") or "").strip().lower(),
            "host": (data.get("imap_host") or "").strip(),
            "port": data.get("imap_port"),
            "security": (data.get("imap_security") or "").strip().lower(),
            "has_password": bool((data.get("imap_password_encrypted") or "").strip()),
            "history_mode": (data.get("initial_history_mode") or "").strip().lower(),
            "history_limit": data.get("initial_history_limit"),
        },
    )
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    logger.info(
        "settings.email.receive.settings_loaded",
        extra={
            "event": "settings.email.receive.settings_loaded",
            "request_id": request_id,
            "company_id": user.company_id,
            "settings_id": settings.id,
        },
    )
    fields = ["provider", "imap_host", "imap_port", "imap_security", "imap_use_ssl", "imap_username", "imap_password_encrypted", "inbox_folder", "processed_folder", "error_folder", "no_order_folder", "doubtful_folder", "read_limit", "test_read_limit", "auto_sync_enabled", "read_unread_only", "read_from_date", "initial_history_mode", "initial_history_limit", "mark_as_read_after_import", "move_after_processing", "post_process_action", "polling_frequency_minutes", "client_id", "client_secret_encrypted", "tenant_id", "redirect_uri", "oauth_scopes", "mailbox", "access_token_encrypted", "refresh_token_encrypted", "connected_email"]
    save_email_section(db, settings, data, user, fields, {"imap_password_encrypted", "client_secret_encrypted", "access_token_encrypted", "refresh_token_encrypted"})
    logger.info(
        "settings.email.receive.saved",
        extra={
            "event": "settings.email.receive.saved",
            "request_id": request_id,
            "company_id": user.company_id,
            "settings_id": settings.id,
            "provider": (settings.provider or "").strip().lower(),
            "host": (settings.imap_host or "").strip(),
            "port": settings.imap_port,
            "security": (settings.imap_security or "").strip().lower(),
            "has_password": bool(settings.imap_password_encrypted),
        },
    )
    allowed_frequencies = {5, 10, 15, 30, 60}
    try:
        frequency = int(settings.polling_frequency_minutes or 1)
    except (TypeError, ValueError):
        frequency = 5
    settings.polling_frequency_minutes = frequency if frequency in allowed_frequencies else 5
    try:
        settings.initial_history_limit = max(min(int(settings.initial_history_limit or 50), 100), 1)
    except (TypeError, ValueError):
        settings.initial_history_limit = 50
    settings.initial_history_mode = settings.initial_history_mode if settings.initial_history_mode in {"new", "7d", "30d", "100", "custom"} else "new"
    try:
        _sync_email_sync_state(master_db, user, settings)
    except Exception:
        master_db.rollback()
    log_action(db, company_id=user.company_id, user=user, action="settings.email.receive.update", entity_type="settings", entity_id=settings.id, message="Configuracion de recepcion actualizada")
    return redirect_or_json(request, {"ok": True, "receive": serialize_email_settings(db, user.company_id)["receive"]}, "email-receive")


@router.post("/email/disconnect")
def disconnect_email_account(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    if not can_edit_email_settings(user):
        return RedirectResponse("/settings#email-account", status_code=303)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    for field in [
        "provider",
        "imap_host",
        "imap_username",
        "imap_password_encrypted",
        "client_id",
        "client_secret_encrypted",
        "tenant_id",
        "redirect_uri",
        "oauth_scopes",
        "mailbox",
        "access_token_encrypted",
        "refresh_token_encrypted",
        "connected_email",
        "processed_folder",
        "error_folder",
        "no_order_folder",
        "doubtful_folder",
        "read_from_date",
    ]:
        setattr(settings, field, None)
    settings.imap_port = 993
    settings.imap_security = "ssl_tls"
    settings.imap_use_ssl = True
    settings.inbox_folder = "INBOX"
    settings.auto_sync_enabled = False
    settings.read_unread_only = True
    settings.mark_as_read_after_import = False
    settings.move_after_processing = False
    settings.updated_by = resolve_updated_by_id(db, user)
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    try:
        _sync_email_sync_state(master_db, user, settings)
    except Exception:
        master_db.rollback()
    log_action(db, company_id=user.company_id, user=user, action="settings.email.disconnect", entity_type="settings", entity_id=settings.id, message="Cuenta de correo desconectada")
    return RedirectResponse("/settings#email-account", status_code=303)


@router.api_route("/email/send", methods=["PUT", "POST"])
async def update_email_send(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar la configuracion de correo."}, status_code=403)
    data = await request_data(request)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    for field in ["smtp_enabled", "save_internal_copy", "preserve_thread_headers"]:
        data.setdefault(field, "off")
    fields = ["smtp_enabled", "smtp_provider", "smtp_host", "smtp_port", "smtp_security", "smtp_username", "smtp_password_encrypted", "from_email", "from_name", "reply_to", "default_cc", "default_bcc", "save_internal_copy", "preserve_thread_headers"]
    save_email_section(db, settings, data, user, fields, {"smtp_password_encrypted"})
    log_action(db, company_id=user.company_id, user=user, action="settings.email.send.update", entity_type="settings", entity_id=settings.id, message="Configuracion de envio actualizada")
    return redirect_or_json(request, {"ok": True, "send": serialize_email_settings(db, user.company_id)["send"]}, "email-send")


@router.api_route("/email/processing", methods=["PUT", "POST"])
async def update_email_processing(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar la configuracion de correo."}, status_code=403)
    data = await request_data(request)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    bool_fields = ["auto_process_on_fetch", "process_only_with_attachments", "process_only_with_pdf", "process_without_attachments", "process_read_emails", "avoid_duplicates_by_message_id", "allow_reprocess", "auto_create_order_if_detected", "always_human_review", "mark_doubtful_below_threshold", "mark_no_order_if_detected"]
    for field in bool_fields:
        data.setdefault(field, "off")
    fields = bool_fields + ["action_order_detected", "action_no_order", "action_doubtful", "action_error", "minimum_score_auto_order", "visible_states"]
    save_email_section(db, settings, data, user, fields)
    log_action(db, company_id=user.company_id, user=user, action="settings.email.processing.update", entity_type="settings", entity_id=settings.id, message="Reglas de procesamiento de correo actualizadas")
    return redirect_or_json(request, {"ok": True, "processing": serialize_email_settings(db, user.company_id)["processing"]}, "email-processing")


@router.api_route("/email/ui", methods=["PUT", "POST"])
async def update_email_ui(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar la configuracion de correo."}, status_code=403)
    data = await request_data(request)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    bool_fields = ["show_summary_cards", "show_score_column", "show_customer_column", "show_attachments_column", "show_order_column", "show_reply_button", "show_process_button"]
    for field in bool_fields:
        data.setdefault(field, "off")
    fields = ["default_filter", "default_date_range", "default_page_size", "default_sort"] + bool_fields
    save_email_section(db, settings, data, user, fields)
    log_action(db, company_id=user.company_id, user=user, action="settings.email.ui.update", entity_type="settings", entity_id=settings.id, message="Preferencias de bandeja de correo actualizadas")
    return redirect_or_json(request, {"ok": True, "ui": serialize_email_settings(db, user.company_id)["ui"]}, "email-ui")


@router.get("/email/templates")
def list_email_templates(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return JSONResponse(serialize_email_settings(db, user.company_id)["templates"])


@router.post("/email/templates")
def create_email_template(
    key: str = Form(...),
    name: str = Form(...),
    template_type: str = Form("other"),
    subject_template: str = Form("Re: {asunto_original}"),
    body_template: str = Form(...),
    active: str = Form("off"),
    is_default_for_type: str = Form("off"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not can_edit_email_settings(user):
        return RedirectResponse("/settings#email-templates", status_code=303)
    template = EmailTemplate(company_id=user.company_id, key=key.strip(), name=name.strip(), template_type=template_type, subject_template=subject_template, body_template=body_template, active=active == "on", is_default_for_type=is_default_for_type == "on", updated_by=resolve_updated_by_id(db, user))
    db.add(template)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="settings.email.template.create", entity_type="email_template", entity_id=template.id, message=f"Plantilla creada: {template.key}")
    return RedirectResponse("/settings#email-templates", status_code=303)


@router.api_route("/email/templates/{template_id}", methods=["PUT", "POST"])
async def update_email_template(template_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar plantillas."}, status_code=403)
    template = db.get(EmailTemplate, template_id)
    if not template or template.company_id != user.company_id:
        return JSONResponse({"error": "Plantilla no encontrada."}, status_code=404)
    data = await request_data(request)
    data.setdefault("active", "off")
    data.setdefault("is_default_for_type", "off")
    update_with_form(template, data)
    template.updated_by = resolve_updated_by_id(db, user)
    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="settings.email.template.update", entity_type="email_template", entity_id=template.id, message=f"Plantilla actualizada: {template.key}")
    return redirect_or_json(request, {"ok": True}, "email-templates")


@router.api_route("/email/templates/{template_id}", methods=["DELETE"])
def delete_email_template(template_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede eliminar plantillas."}, status_code=403)
    template = db.get(EmailTemplate, template_id)
    if not template or template.company_id != user.company_id:
        return JSONResponse({"error": "Plantilla no encontrada."}, status_code=404)
    template.active = False
    template.updated_by = resolve_updated_by_id(db, user)
    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="settings.email.template.disable", entity_type="email_template", entity_id=template.id, message=f"Plantilla desactivada: {template.key}")
    return JSONResponse({"ok": True})


@router.post("/email/templates/{template_id}/duplicate")
def duplicate_email_template(template_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if can_edit_email_settings(user):
        template = db.get(EmailTemplate, template_id)
        if template and template.company_id == user.company_id:
            copy = EmailTemplate(company_id=user.company_id, key=f"{template.key}_copia", name=f"{template.name} copia", template_type=template.template_type, subject_template=template.subject_template, body_template=template.body_template, active=False, is_default_for_type=False, updated_by=resolve_updated_by_id(db, user))
            db.add(copy)
            db.commit()
            log_action(db, company_id=user.company_id, user=user, action="settings.email.template.duplicate", entity_type="email_template", entity_id=copy.id, message=f"Plantilla duplicada: {template.key}")
    return RedirectResponse("/settings#email-templates", status_code=303)


@router.post("/email/templates/reset-defaults")
def reset_email_templates(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if can_edit_email_settings(user):
        ensure_default_email_templates(db, user.company_id, user.id)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="settings.email.templates.reset", entity_type="email_template", message="Plantillas de correo restauradas")
    return RedirectResponse("/settings#email-templates", status_code=303)


@router.get("/email/signature")
def get_email_signature(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return JSONResponse(serialize_email_settings(db, user.company_id)["signature"])


@router.api_route("/email/signature", methods=["PUT", "POST"])
async def update_email_signature(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return JSONResponse({"error": "Solo Administrador puede modificar la firma."}, status_code=403)
    data = await request_data(request)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    for field in ["use_signature", "include_logo_in_signature"]:
        data.setdefault(field, "off")
    fields = ["from_name", "from_email", "reply_to", "signature_text", "signature_html", "use_signature", "include_logo_in_signature", "legal_footer"]
    save_email_section(db, settings, data, user, fields)
    log_action(db, company_id=user.company_id, user=user, action="settings.email.signature.update", entity_type="settings", entity_id=settings.id, message="Firma de correo actualizada")
    return redirect_or_json(request, {"ok": True, "signature": serialize_email_settings(db, user.company_id)["signature"]}, "email-signature")


@router.get("/branding")
def get_branding(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return JSONResponse(branding_to_dict(get_or_create_branding(db, user.company_id)))


@router.put("/branding")
async def put_branding(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if user.role.name != "Administrador":
        return JSONResponse({"error": "Solo Administrador puede modificar identidad corporativa."}, status_code=403)
    data = await request.json()
    branding = get_or_create_branding(db, user.company_id)
    for field in ["company_name", "app_name", "primary_claim", "secondary_claim", "short_description", "logo_url", "dark_logo_url", "favicon_url"]:
        if field in data:
            setattr(branding, field, data[field])
    if "theme" in data:
        import json
        branding.theme_json = json.dumps(data["theme"])
    if "microcopy" in data:
        import json
        branding.microcopy_json = json.dumps(data["microcopy"])
    branding.updated_by = resolve_updated_by_id(db, user)
    db.commit()
    invalidate_branding_cache(user.company_id)
    log_action(db, company_id=user.company_id, user=user, action="branding.update", entity_type="branding", entity_id=branding.id, message="Identidad corporativa actualizada via API")
    return JSONResponse(branding_to_dict(branding))


@router.post("/branding")
async def update_branding(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if user.role.name != "Administrador":
        log_action(db, company_id=user.company_id, user=user, action="branding.update.denied", entity_type="branding", message="Intento de modificar identidad corporativa sin permisos")
        return RedirectResponse("/settings", status_code=303)
    formdata = await request.form()
    form = {key: value for key, value in formdata.items() if not isinstance(value, UploadFile)}
    branding = get_or_create_branding(db, user.company_id)
    update_branding_from_form(branding, form, resolve_updated_by_id(db, user))
    for field, upload_key, remove_key, prefix in [
        ("logo_url", "logo_file", "remove_logo", "logo-main"),
        ("dark_logo_url", "dark_logo_file", "remove_dark_logo", "logo-dark"),
        ("favicon_url", "favicon_file", "remove_favicon", "favicon"),
    ]:
        upload = formdata.get(upload_key)
        if form.get(remove_key) == "on":
            delete_brand_asset(getattr(branding, field))
            setattr(branding, field, None)
        elif isinstance(upload, UploadFile) and upload.filename:
            delete_brand_asset(getattr(branding, field))
            setattr(branding, field, await store_brand_asset(user.company_id, upload, prefix))
    db.commit()
    invalidate_branding_cache(user.company_id)
    log_action(db, company_id=user.company_id, user=user, action="branding.update", entity_type="branding", entity_id=branding.id, message="Identidad corporativa actualizada")
    return RedirectResponse("/settings#branding", status_code=303)


@router.post("/branding/reset-default")
def reset_branding_default(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if user.role.name == "Administrador":
        branding = get_or_create_branding(db, user.company_id)
        reset_branding(branding, resolve_updated_by_id(db, user))
        db.commit()
        invalidate_branding_cache(user.company_id)
        log_action(db, company_id=user.company_id, user=user, action="branding.reset_default", entity_type="branding", entity_id=branding.id, message="Identidad corporativa restaurada a valores por defecto")
    return RedirectResponse("/settings#branding", status_code=303)


@router.post("/company/reset-default")
def reset_company_default(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if user.role.name != "Administrador":
        return RedirectResponse("/settings#general", status_code=303)
    settings = get_settings()
    company = db.get(Company, user.company_id)
    if company:
        delete_brand_asset(company.logo_url)
        company.name = settings.default_company_name
        company.legal_name = None
        company.tax_id = None
        company.email = None
        company.phone = None
        company.web = None
        company.address = None
        company.country = None
        company.currency = "EUR"
        company.notification_email = None
        company.responsible_contact = None
        company.active = True
        company.logo_url = None
        company.language = "es"
        company.timezone = "Europe/Madrid"
        company.date_format = "%d/%m/%Y"
        company.decimal_separator = ","
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="company.reset_default", entity_type="company", entity_id=company.id, message="Configuracion general restaurada a valores por defecto")
    return RedirectResponse("/settings#general", status_code=303)


@router.post("/automation")
async def update_kibak_automation(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    """Update the tenant routing policy, with a hard guard before auto-forwarding."""
    if get_settings().app_slug.strip().lower() != "kibak":
        return JSONResponse({"ok": False, "message": "No encontrado."}, status_code=404)
    if user.role.name not in {"Administrador", "Superadmin"}:
        return JSONResponse({"ok": False, "message": "Solo Administrador puede cambiar la automatización."}, status_code=403)
    data = await request_data(request)
    current = load_routing_policy(db, user.company_id)
    try:
        desired = parse_policy_form(data, current)
        settings = get_or_create_settings(db, LLMSettings, user.company_id)
        settings.auto_routing_enabled = desired.auto_routing_enabled
        settings.auto_forwarding_enabled = desired.auto_forwarding_enabled
        settings.simulation_mode = desired.simulation_mode
        settings.routing_review_threshold = desired.review_threshold
        settings.routing_auto_threshold = desired.auto_threshold
        if desired.auto_forwarding_enabled:
            readiness = build_kibak_readiness(db, user.company_id)
            blocking = [item["message"] for item in readiness["checks"] if item["status"] == "FAIL"]
            if blocking:
                db.rollback()
                payload = {"ok": False, "message": "El reenvío automático no está preparado.", "checks": blocking}
                return JSONResponse(payload, status_code=409) if "application/json" in (request.headers.get("accept") or "") else RedirectResponse("/settings#automation", status_code=303)
        settings.updated_by = resolve_updated_by_id(db, user)
        settings.updated_at = datetime.now(timezone.utc)
        db.commit()
        log_action(
            db,
            company_id=user.company_id,
            user=user,
            action="routing.policy.updated",
            entity_type="llm_settings",
            entity_id=settings.id,
            message="Política de routing actualizada.",
            metadata={"policy": desired.as_dict()},
        )
    except RoutingPolicyError as exc:
        db.rollback()
        payload = {"ok": False, "message": str(exc)}
        return JSONResponse(payload, status_code=422) if "application/json" in (request.headers.get("accept") or "") else RedirectResponse("/settings#automation", status_code=303)
    payload = {"ok": True, "policy": load_routing_policy(db, user.company_id).as_dict()}
    return JSONResponse(payload) if "application/json" in (request.headers.get("accept") or "") else RedirectResponse("/settings#automation", status_code=303)


@router.get("/readiness")
def kibak_readiness_page(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if get_settings().app_slug.strip().lower() != "kibak":
        raise HTTPException(status_code=404, detail="No encontrado")
    readiness = build_kibak_readiness(db, user.company_id)
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(readiness)
    return templates.TemplateResponse("settings/readiness.html", {"request": request, "user": user, "title": "Preparación del sistema", "readiness": readiness})


@router.post("/{section}")
async def update_settings(section: str, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    anchor = await update_settings_section_async(section, request, db, user)
    log_action(db, company_id=user.company_id, user=user, action=f"settings.{section}.update", entity_type="settings", message=f"Configuracion actualizada: {section}")
    return RedirectResponse(f"/settings#{anchor}", status_code=303)


@router.post("/test/{section}")
def test_connection(section: str, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    result, anchor = run_connection_test(section, db, user)
    log_action(db, company_id=user.company_id, user=user, action=f"settings.{section}.test", entity_type="settings", message=result["message"])
    return RedirectResponse(f"/settings#{anchor}", status_code=303)


@router.post("/email/imap/test")
def test_email_imap(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_test_email_settings(user):
        return RedirectResponse("/settings#email-diagnostics", status_code=303)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    request_id = getattr(request.state, "request_id", None)
    provider_name = (settings.provider or "imap").strip().lower()
    host_name = (settings.imap_host or "").strip()
    port_number = settings.imap_port
    security_name = (settings.imap_security or "").strip().lower()
    try:
        result = test_imap_connection(settings, request_id=request_id)
        settings.last_imap_test_at = datetime.now(timezone.utc)
        settings.last_imap_test_ok = result["ok"]
        settings.last_imap_test_message = result["message"]
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="email.imap.test", entity_type="settings", entity_id=settings.id, message=result["message"])
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception(
            "imap.test.route_failed",
            extra={
                "event": "imap.test.route_failed",
                "request_id": request_id,
                "provider": provider_name,
                "host": host_name,
                "port": port_number,
                "security": security_name,
                "error_type": exc.__class__.__name__,
            },
        )
        settings.last_imap_test_at = datetime.now(timezone.utc)
        settings.last_imap_test_ok = False
        settings.last_imap_test_message = "No se ha podido comprobar la conexión IMAP en este momento."
        try:
            db.commit()
        except Exception:
            db.rollback()
        try:
            log_action(db, company_id=user.company_id, user=user, action="email.imap.test", entity_type="settings", entity_id=settings.id, message=settings.last_imap_test_message)
        except Exception:
            db.rollback()
    return RedirectResponse("/settings#email-diagnostics", status_code=303)


@router.post("/email/smtp/test")
def test_email_smtp(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_test_email_settings(user):
        return RedirectResponse("/settings#email-diagnostics", status_code=303)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    result = test_smtp_connection(settings)
    settings.last_smtp_test_at = datetime.now(timezone.utc)
    settings.last_smtp_test_ok = result["ok"]
    settings.last_smtp_test_message = result["message"]
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="email.smtp.test", entity_type="settings", entity_id=settings.id, message=result["message"])
    return RedirectResponse("/settings#email-diagnostics", status_code=303)


@router.post("/email/smtp/send-test")
def send_email_test(to_email: str = Form(...), subject: str = Form("Prueba SMTP"), message: str = Form("Correo de prueba enviado desde Anchi."), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_test_email_settings(user):
        return RedirectResponse("/settings#email-diagnostics", status_code=303)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    result = send_test_email(settings, to_email, subject, message)
    settings.last_smtp_test_at = datetime.now(timezone.utc)
    settings.last_smtp_test_ok = result["ok"]
    settings.last_smtp_test_message = result["message"]
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="email.smtp.send_test", entity_type="settings", entity_id=settings.id, message=result["message"])
    return RedirectResponse("/settings#email-diagnostics", status_code=303)


@router.post("/email/read")
def read_email(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "settings.email.read.start",
        extra={"event": "settings.email.read.start", "request_id": request_id, "company_id": user.company_id, "user_id": user.id},
    )
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    safe_limit = max(min(int(settings.read_limit or 10), 50), 1)
    job = enqueue_job(db, company_id=user.company_id, job_type="email_sync", payload={"auto_process": False, "unread_only": False, "limit": safe_limit}, created_by_user_id=user.id)
    logger.info(
        "settings.email.read.requested",
        extra={"event": "settings.email.read.requested", "request_id": request_id, "company_id": user.company_id, "job_id": job.id, "job_type": job.job_type},
    )
    result = _run_email_sync_job_if_needed(request, db, user, job)
    if result is not None:
        log_action(
            db,
            company_id=user.company_id,
            user=user,
            action="email.read.inline",
            entity_type="job",
            entity_id=job.id,
            message=result.get("message") or "Lectura IMAP completada",
        )
        return _queued_job_response(request, job.id, result=result)
    log_action(db, company_id=user.company_id, user=user, action="email.read", entity_type="job", entity_id=job.id, message="Lectura IMAP solicitada")
    return _queued_job_response(request, job.id)


@router.post("/email/read-unprocessed")
def read_unprocessed_email(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "settings.email.read_unprocessed.start",
        extra={"event": "settings.email.read_unprocessed.start", "request_id": request_id, "company_id": user.company_id, "user_id": user.id},
    )
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    safe_limit = max(min(int(settings.read_limit or 10), 50), 1)
    job = enqueue_job(db, company_id=user.company_id, job_type="email_sync", payload={"auto_process": False, "unread_only": True, "limit": safe_limit}, created_by_user_id=user.id)
    logger.info(
        "settings.email.read_unprocessed.requested",
        extra={"event": "settings.email.read_unprocessed.requested", "request_id": request_id, "company_id": user.company_id, "job_id": job.id, "job_type": job.job_type},
    )
    result = _run_email_sync_job_if_needed(request, db, user, job)
    if result is not None:
        log_action(
            db,
            company_id=user.company_id,
            user=user,
            action="email.read_unprocessed.inline",
            entity_type="job",
            entity_id=job.id,
            message=result.get("message") or "Lectura de correos recientes completada",
        )
        return _queued_job_response(request, job.id, "/settings#email-diagnostics", result=result)
    log_action(db, company_id=user.company_id, user=user, action="email.read_unprocessed", entity_type="job", entity_id=job.id, message="Lectura de correos recientes solicitada")
    return _queued_job_response(request, job.id, "/settings#email-diagnostics")


@router.post("/email/backfill")
def backfill_email_history(
    request: Request,
    from_date: str = Form(""),
    to_date: str = Form(""),
    limit: int = Form(100),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not can_test_email_settings(user):
        return RedirectResponse("/settings#email-diagnostics", status_code=303)
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    if from_date:
        settings.read_from_date = from_date
    from_date_value = _parse_date_input(from_date or settings.read_from_date)
    to_date_value = _parse_date_input(to_date)
    if from_date_value and to_date_value and to_date_value < from_date_value:
        return JSONResponse({"ok": False, "message": "La fecha final no puede ser anterior a la inicial."}, status_code=400)
    safe_limit = max(min(int(limit or 100), 100), 1)
    run_id = getattr(request.state, "request_id", None)

    payload = {
        "from_date": from_date or settings.read_from_date,
        "to_date": to_date or None,
        "limit": safe_limit,
    }
    if run_id:
        payload["run_id"] = run_id

    job = enqueue_job(
        db,
        company_id=user.company_id,
        job_type="backfill_imap",
        payload=payload,
        created_by_user_id=user.id,
    )
    db.commit()
    result = _run_backfill_job_once(
        request,
        db,
        user,
        job,
        from_date=payload.get("from_date"),
        to_date=payload.get("to_date"),
    )
    date_label = payload.get("from_date") or "configuración"
    if payload.get("to_date"):
        date_label = f"{date_label} a {payload.get('to_date')}"
    log_action(
        db,
        company_id=user.company_id,
        user=user,
        action="email.backfill",
        entity_type="job",
        entity_id=job.id,
        message=(
            f"Backfill IMAP ejecutado desde {date_label} (límite {safe_limit}) · "
            f"lotes={result.get('batches')} · guardados={result.get('saved')} · "
            f"duplicados={result.get('duplicates')} · errores={result.get('errors')}"
        ),
    )
    return _backfill_response(request, result, "/settings#email-diagnostics")


@router.post("/email/backfill/continue/{job_id}")
def continue_backfill_email_history(
    job_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not can_test_email_settings(user):
        return JSONResponse({"ok": False, "error_type": "permission_denied", "message": "No tienes permisos para continuar el backfill histórico."}, status_code=403)
    job = db.get(BackgroundJob, job_id)
    if not job or job.company_id != user.company_id:
        return JSONResponse({"ok": False, "error_type": "job_not_found", "message": "No se encontró la continuación solicitada."}, status_code=404)
    if job.job_type != "backfill_imap":
        return JSONResponse({"ok": False, "error_type": "invalid_job_type", "message": "La continuación solicitada no corresponde a un backfill IMAP."}, status_code=400)
    if job.status not in {"queued", "retrying"}:
        return JSONResponse({"ok": False, "error_type": "invalid_job_status", "message": "La continuación del backfill no está lista para ejecutarse."}, status_code=409)
    payload = job_payload(job)
    result = _run_backfill_job_once(
        request,
        db,
        user,
        job,
        from_date=payload.get("from_date"),
        to_date=payload.get("to_date"),
    )
    return _backfill_response(request, result, "/settings#email-diagnostics")


@router.post("/email/initial-sync/preview")
async def preview_email_initial_sync(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not can_edit_email_settings(user):
        return RedirectResponse("/settings#email-receive", status_code=303)
    data = _normalize_receive_form(await request_data(request))
    preview_settings = _clone_email_settings_for_preview(db, user.company_id, data)
    preview = preview_initial_imap_sync(preview_settings)
    return templates.TemplateResponse(
        "settings/email_initial_sync_preview.html",
        {
            "request": request,
            "user": user,
            "company": db.get(Company, user.company_id),
            "preview": preview,
            "form_data": data,
            "can_edit_email": can_edit_email_settings(user),
        },
    )


@router.post("/email/initial-sync")
async def confirm_email_initial_sync(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    if not can_edit_email_settings(user):
        return RedirectResponse("/settings#email-receive", status_code=303)
    data = _normalize_receive_form(await request_data(request))
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    save_email_section(db, settings, data, user, ["provider", "imap_host", "imap_port", "imap_security", "imap_use_ssl", "imap_username", "imap_password_encrypted", "inbox_folder", "processed_folder", "error_folder", "no_order_folder", "doubtful_folder", "read_limit", "test_read_limit", "auto_sync_enabled", "read_unread_only", "read_from_date", "initial_history_mode", "initial_history_limit", "mark_as_read_after_import", "move_after_processing", "post_process_action", "polling_frequency_minutes", "client_id", "client_secret_encrypted", "tenant_id", "redirect_uri", "oauth_scopes", "mailbox", "access_token_encrypted", "refresh_token_encrypted", "connected_email"], {"imap_password_encrypted", "client_secret_encrypted", "access_token_encrypted", "refresh_token_encrypted"})
    try:
        settings.initial_history_limit = max(min(int(settings.initial_history_limit or 50), 100), 1)
    except (TypeError, ValueError):
        settings.initial_history_limit = 50
    settings.initial_history_mode = settings.initial_history_mode if settings.initial_history_mode in {"new", "7d", "30d", "100", "custom"} else "new"
    db.commit()
    sync_state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == user.company_id, EmailSyncState.channel_key == "email"))
    if not sync_state:
        sync_state = EmailSyncState(company_id=user.company_id, channel_key="email", enabled=True, frequency_seconds=60, status="idle", next_run_at=datetime.now(timezone.utc))
        master_db.add(sync_state)
        master_db.commit()
    try:
        result = run_initial_imap_sync(db, settings, user.company_id, sync_state=sync_state, sync_session=master_db)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    log_action(db, company_id=user.company_id, user=user, action="email.initial_sync", entity_type="settings", entity_id=settings.id, message=result.get("message") or "Sincronizacion inicial ejecutada")
    return redirect_or_json(request, {"ok": result.get("ok", False), "message": result.get("message"), "preview": result}, "email-receive")


@router.get("/agent")
def get_agent_settings(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    scoring = get_or_create_settings(db, ScoringSettings, user.company_id)
    metrics = agent_metrics(db, user.company_id, scoring)
    return JSONResponse({"status": agent_status(llm, metrics), "metrics": metrics})


@router.post("/agent/activate")
def activate_agent(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    llm.agent_enabled = True
    if llm.agent_mode == "desactivado":
        llm.agent_mode = "semiautomatico"
    llm.updated_by = resolve_updated_by_id(db, user)
    llm.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="agent.settings_updated", entity_type="settings", entity_id=llm.id, message="Agente IA activado")
    return RedirectResponse("/settings#agent-ai", status_code=303)


@router.post("/agent/pause")
def pause_agent(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    llm.agent_enabled = False
    llm.updated_by = resolve_updated_by_id(db, user)
    llm.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="agent.settings_updated", entity_type="settings", entity_id=llm.id, message="Agente IA pausado")
    return RedirectResponse("/settings#agent-ai", status_code=303)


@router.post("/agent/test-full-flow")
def test_agent_full_flow(sample_text: str = Form("Cliente de prueba solicita 10 unidades del articulo de prueba."), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    start = perf_counter()
    classification = classify_sample(db, llm, user.company_id, sample_text, active_prompt_content(db, user.company_id, "classification"))
    extraction = extract_sample(db, llm, user.company_id, sample_text, active_prompt_content(db, user.company_id, "extraction")) if classification.get("ok") else {"ok": False, "message": "No se ejecuto extraccion porque fallo la clasificacion."}
    elapsed_ms = int((perf_counter() - start) * 1000)
    ok = classification.get("ok") and extraction.get("ok")
    llm.last_test_at = datetime.now(timezone.utc)
    llm.last_test_ok = bool(ok)
    llm.last_test_message = f"Flujo completo {'correcto' if ok else 'con incidencias'}. Tiempo: {elapsed_ms} ms. No se confirmo ni exporto ningun pedido."
    llm.last_error = None if ok else f"{classification.get('message')} {extraction.get('message')}"
    llm.last_response_ms = elapsed_ms
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="agent.process_email", entity_type="settings", entity_id=llm.id, message=llm.last_test_message)
    return RedirectResponse("/settings#agent-tests", status_code=303)


@router.post("/llm/classify")
def test_llm_classification(sample_text: str = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    start = perf_counter()
    prompt = active_prompt_content(db, user.company_id, "classification")
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    result = classify_sample(db, llm, user.company_id, sample_text, prompt)
    llm.last_test_at = datetime.now(timezone.utc)
    llm.last_test_ok = result["ok"]
    llm.last_test_message = f"Clasificacion: {result['message']} Tiempo: {int((perf_counter() - start) * 1000)} ms."
    llm.last_error = None if result["ok"] else result["message"]
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="agent.classification_test", entity_type="settings", message=llm.last_test_message)
    return RedirectResponse("/settings#agent-tests", status_code=303)


@router.post("/llm/extract")
def test_llm_extraction(sample_text: str = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    start = perf_counter()
    prompt = active_prompt_content(db, user.company_id, "extraction")
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    result = extract_sample(db, llm, user.company_id, sample_text, prompt)
    llm.last_test_at = datetime.now(timezone.utc)
    llm.last_test_ok = result["ok"]
    llm.last_test_message = f"Extraccion: {result['message']} Tiempo: {int((perf_counter() - start) * 1000)} ms."
    llm.last_error = None if result["ok"] else result["message"]
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="agent.extraction_test", entity_type="settings", message=llm.last_test_message)
    return RedirectResponse("/settings#agent-tests", status_code=303)


def active_prompt_content(db: Session, company_id: int, purpose: str) -> str:
    template = db.scalar(select(PromptTemplate).where(PromptTemplate.company_id == company_id, PromptTemplate.purpose == purpose))
    if not template or not template.active_version_id:
        return "Responde en JSON valido cuando se solicite extraccion."
    version = db.get(PromptVersion, template.active_version_id)
    return version.content if version else "Responde en JSON valido cuando se solicite extraccion."


DEFAULT_AGENT_PROMPTS = {
    "classification": "Clasifica el correo como pedido, no_pedido, consulta, incidencia o dudoso. Responde JSON valido con tipo_correo, confianza y motivo.",
    "extraction": "Extrae un pedido en JSON valido con cliente, fechas, observaciones y lineas con producto, referencia, cantidad y unidad.",
    "validation": "Valida el pedido extraido contra datos de cliente y producto. Devuelve JSON con advertencias, bloqueos y scoring recomendado.",
    "non_order": "Resume por que el correo no contiene pedido y clasificalo como consulta, incidencia, no_pedido o dudoso.",
    "doubtful": "Analiza un correo dudoso y devuelve JSON con motivos de duda, campos ambiguos y accion recomendada.",
}


@router.post("/prompts/reset-defaults")
def reset_agent_prompts(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if user.role.name == "Administrador":
        for purpose, content in DEFAULT_AGENT_PROMPTS.items():
            template = db.scalar(select(PromptTemplate).where(PromptTemplate.company_id == user.company_id, PromptTemplate.purpose == purpose))
            if not template:
                template = PromptTemplate(company_id=user.company_id, name=purpose.replace("_", " ").title(), purpose=purpose)
                db.add(template)
                db.flush()
            last_version = db.scalar(select(PromptVersion.version).where(PromptVersion.template_id == template.id).order_by(PromptVersion.version.desc())) or 0
            version = PromptVersion(company_id=user.company_id, template_id=template.id, version=last_version + 1, content=content, created_by_user_id=user.id)
            db.add(version)
            db.flush()
            template.active_version_id = version.id
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="agent.prompt_updated", entity_type="prompt", message="Prompts restaurados a valores por defecto")
    return RedirectResponse("/settings#agent-prompts", status_code=303)


@router.post("/prompts/{template_id}/duplicate")
def duplicate_prompt(template_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    template = db.get(PromptTemplate, template_id)
    if template and template.company_id == user.company_id and user.role.name == "Administrador":
        active = db.get(PromptVersion, template.active_version_id) if template.active_version_id else None
        if active:
            last_version = db.scalar(select(PromptVersion.version).where(PromptVersion.template_id == template.id).order_by(PromptVersion.version.desc())) or 0
            version = PromptVersion(company_id=user.company_id, template_id=template.id, version=last_version + 1, content=active.content, created_by_user_id=user.id)
            db.add(version)
            db.commit()
            log_action(db, company_id=user.company_id, user=user, action="agent.prompt_updated", entity_type="prompt", entity_id=template.id, message=f"Prompt duplicado: {template.purpose}")
    return RedirectResponse("/settings#agent-prompts", status_code=303)


@router.post("/prompts/{template_id}")
def save_prompt(template_id: int, content: str = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    template = db.get(PromptTemplate, template_id)
    if template and template.company_id == user.company_id:
        last_version = db.scalar(select(PromptVersion.version).where(PromptVersion.template_id == template.id).order_by(PromptVersion.version.desc())) or 0
        version = PromptVersion(company_id=user.company_id, template_id=template.id, version=last_version + 1, content=content, created_by_user_id=user.id)
        db.add(version)
        db.flush()
        template.active_version_id = version.id
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="agent.prompt_updated", entity_type="prompt", entity_id=template.id, message=f"Prompt actualizado: {template.purpose}")
    return RedirectResponse("/settings#agent-prompts", status_code=303)
