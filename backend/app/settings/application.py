from __future__ import annotations

from datetime import date, datetime, timezone
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.model_catalog import DEFAULT_OPENAI_MODEL, LEGACY_OPENAI_MODEL_FALLBACK, resolve_openai_model_choice, resolve_openai_runtime_model
from app.core.config import get_settings
from app.core.encryption import mask_secret
from app.db.models import AuditLog, BackgroundJob, BrandingSettings, Company, Customer, DecisionSettings, Email, EmailSettings, ExportSettings, FTPSettings, InputChannel, InboundMessage, LLMSettings, Order, Product, PromptTemplate, PromptVersion, ScoringSettings
from app.dashboard.service import agent_status_label, recent_processed_emails_overview
from app.exports.service import FTPService
from app.logs.service import log_action
from app.master.service import TenantUser
from app.settings.agent_config import agent_metrics, agent_status, improvement_suggestions, apply_safety_level
from app.settings.branding import branding_to_dict, delete_brand_asset, get_or_create_branding, reset_branding, store_brand_asset, update_branding_from_form
from app.settings.email_config import TEMPLATE_VARIABLES, email_config_status, email_templates, ensure_default_email_templates, serialize_email_settings
from app.settings.integrations import classify_sample, extract_sample, send_test_email, test_imap_connection, test_smtp_connection
from app.settings.service import get_or_create_settings, resolve_updated_by_id, update_with_form
from app.tenancy.migrations import tenant_migration_report


def can_edit_email_settings(user: TenantUser) -> bool:
    return user.role.name == "Administrador"


def can_test_email_settings(user: TenantUser) -> bool:
    return user.role.name in {"Administrador", "Supervisor"}


async def request_data(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    form = await request.form()
    data = {}
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            continue
        data[key] = value
    return data


def save_email_section(db: Session, settings: EmailSettings, data: dict, user: TenantUser, fields: list[str], secret_fields: set[str] | None = None) -> None:
    payload = {key: data[key] for key in fields if key in data}
    update_with_form(settings, payload, secret_fields)
    settings.updated_by = resolve_updated_by_id(db, user)
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()


def redirect_or_json(request: Request, payload: dict, anchor: str = "email"):
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(payload)
    referer = request.headers.get("referer", "")
    if "channels" in referer:
        return RedirectResponse("/settings/channels?focus=email", status_code=303)
    return RedirectResponse(f"/settings#{anchor}", status_code=303)


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
        state("general", "General", "ready" if company and company.name else "pending", f"{company.name if company else 'Sin empresa'} · {(company.currency or 'EUR') if company else 'EUR'} · {('activa' if company and company.active else 'inactiva')}", "Configurar"),
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
        {"label": "Empresa e identidad básica", "state": "done" if company and company.name and branding.app_name else "pending"},
        {"label": "Canal de entrada conectado", "state": "done" if active_channels_count else "pending"},
        {"label": "Agente IA configurado", "state": "done" if llm.provider != "disabled" and llm.api_key_encrypted else "pending"},
        {"label": "Clientes y productos cargados", "state": "done" if customer_count and product_count else "pending"},
        {"label": "Scoring definido", "state": "done" if scoring.safe_threshold and scoring.review_threshold else "pending"},
        {"label": "Motor de decisión activo", "state": "done" if decision.enable_exact_match or decision.enable_alias_match else "pending"},
        {"label": "Exportación configurada", "state": "done" if export.file_type and ftp.host else "pending"},
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
    last_seed_at = db.scalar(select(func.max(AuditLog.created_at)).where(AuditLog.company_id == user.company_id, AuditLog.action == "demo.seed"))
    customers_total = db.scalar(select(func.count(Customer.id)).where(Customer.company_id == user.company_id)) or 0
    products_total = db.scalar(select(func.count(Product.id)).where(Product.company_id == user.company_id)) or 0
    orders_total = db.scalar(select(func.count(Order.id)).where(Order.company_id == user.company_id)) or 0
    emails_total = db.scalar(select(func.count(Email.id)).where(Email.company_id == user.company_id)) or 0
    processed_emails_total = db.scalar(select(func.count(Email.id)).where(Email.company_id == user.company_id, Email.agent_status != "not_processed")) or 0
    inbound_total = db.scalar(select(func.count(InboundMessage.id)).where(InboundMessage.company_id == user.company_id)) or 0
    active_channels_total = db.scalar(select(func.count(InputChannel.id)).where(InputChannel.company_id == user.company_id, InputChannel.is_active == True)) or 0  # noqa: E712
    jobs_total = db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id)) or 0
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
        "jobs_queued": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "queued")) or 0,
        "jobs_running": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "running")) or 0,
        "jobs_retrying": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "retrying")) or 0,
        "jobs_failed": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "failed")) or 0,
        "jobs_cancelled": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "cancelled")) or 0,
        "jobs_success": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "success")) or 0,
    }


async def update_settings_section_async(section: str, request: Request, db: Session, user: TenantUser):
    formdata = await request.form()
    form = {key: value for key, value in formdata.items() if not isinstance(value, UploadFile)}
    anchor = section
    if section == "company":
        company = db.get(Company, user.company_id)
        if company:
            for field in ["name", "legal_name", "tax_id", "email", "phone", "web", "address", "country", "currency", "notification_email", "responsible_contact", "language", "timezone", "date_format", "decimal_separator"]:
                if field in form:
                    setattr(company, field, form[field])
            company.active = form.get("active") == "on"
            logo_file = formdata.get("logo_file")
            if form.get("remove_logo") == "on":
                delete_brand_asset(company.logo_url)
                company.logo_url = None
            elif isinstance(logo_file, UploadFile) and logo_file.filename:
                delete_brand_asset(company.logo_url)
                company.logo_url = await store_brand_asset(user.company_id, logo_file, "company-logo")
            elif form.get("logo_url"):
                company.logo_url = form["logo_url"].strip()
            if not company.currency:
                company.currency = "EUR"
    elif section == "email":
        instance = get_or_create_settings(db, EmailSettings, user.company_id)
        form.setdefault("imap_use_ssl", "off")
        update_with_form(instance, form, {"client_secret_encrypted", "access_token_encrypted", "refresh_token_encrypted", "imap_password_encrypted"})
        anchor = "email"
    elif section == "llm":
        instance = get_or_create_settings(db, LLMSettings, user.company_id)
        bool_fields = [
            "agent_enabled", "use_same_model_for_all", "can_read_email", "can_extract_pdf", "can_classify_email", "can_extract_order",
            "can_suggest_customer", "can_suggest_products", "can_calculate_score", "can_create_pending_order", "can_mark_no_order",
            "can_reply_customer", "allow_auto_confirm", "allow_auto_export", "detailed_llm_logs", "store_llm_payloads", "anonymize_llm_logs",
            "debug_mode", "auto_routing_enabled", "auto_forwarding_enabled",
        ]
        for field in bool_fields:
            form.setdefault(field, "off")
        extracted_model = resolve_openai_model_choice(
            form.get("extraction_model_mode"),
            form.get("extraction_model_custom"),
            form.get("extraction_model") or resolve_openai_runtime_model(instance.extraction_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK),
        )
        form["extraction_model"] = extracted_model
        if form.get("use_same_model_for_all") == "on":
            model = extracted_model or form.get("classification_model") or instance.classification_model or DEFAULT_OPENAI_MODEL
            form["classification_model"] = model
            form["validation_model"] = model
        update_with_form(instance, form, {"api_key_encrypted"})
        if instance.provider == "disabled" or instance.agent_mode == "desactivado":
            instance.agent_enabled = False
        instance.updated_by = resolve_updated_by_id(db, user)
        instance.updated_at = datetime.now(timezone.utc)
        apply_safety_level(get_or_create_settings(db, ScoringSettings, user.company_id), instance.safety_level)
        anchor = "agent-ai"
    elif section == "ftp":
        instance = get_or_create_settings(db, FTPSettings, user.company_id)
        for field in ["passive_mode", "overwrite_files"]:
            form.setdefault(field, "off")
        update_with_form(instance, form, {"password_encrypted", "private_key_encrypted"})
        anchor = "ftp"
    elif section == "export":
        instance = get_or_create_settings(db, ExportSettings, user.company_id)
        form.setdefault("include_header", "off")
        update_with_form(instance, form)
        anchor = "export"
    elif section == "scoring":
        instance = get_or_create_settings(db, ScoringSettings, user.company_id)
        for field in ["block_without_customer", "block_without_reference", "block_without_quantity", "block_below_threshold"]:
            form.setdefault(field, "off")
        update_with_form(instance, form)
        llm = get_or_create_settings(db, LLMSettings, user.company_id)
        for field in ["allow_auto_confirm", "allow_auto_export"]:
            setattr(llm, field, form.get(field) == "on")
        llm.updated_by = resolve_updated_by_id(db, user)
        llm.updated_at = datetime.now(timezone.utc)
        anchor = "scoring"
    elif section == "decision":
        instance = get_or_create_settings(db, DecisionSettings, user.company_id)
        for field in [
            "enable_exact_match",
            "enable_alias_match",
            "enable_relation_match",
            "enable_history_match",
            "enable_rag_match",
            "enable_llm_support",
            "always_human_review",
            "auto_approve_aliases",
            "block_new_customer",
            "block_conflicting_aliases",
            "block_missing_quantity",
            "block_missing_reference",
        ]:
            form.setdefault(field, "off")
        update_with_form(instance, form)
        instance.updated_at = datetime.now(timezone.utc)
        anchor = "decision"
    db.commit()
    return anchor


def run_connection_test(section: str, db: Session, user: TenantUser) -> tuple[dict, str]:
    if section == "email":
        result = test_imap_connection(get_or_create_settings(db, EmailSettings, user.company_id))
        settings = get_or_create_settings(db, EmailSettings, user.company_id)
        settings.last_imap_test_at = datetime.now(timezone.utc)
        settings.last_imap_test_ok = result["ok"]
        settings.last_imap_test_message = result["message"]
        db.commit()
        return result, "email"
    if section == "llm":
        llm = get_or_create_settings(db, LLMSettings, user.company_id)
        start = perf_counter()
        result = classify_sample(db, llm, user.company_id, "Pedido de 10 unidades del articulo de prueba.", "Clasifica el texto como pedido o no_pedido. Responde solo una palabra.")
        elapsed_ms = int((perf_counter() - start) * 1000)
        llm.last_test_at = datetime.now(timezone.utc)
        llm.last_test_ok = result["ok"]
        llm.last_test_message = f"{result['message']} Modelo: {llm.classification_model}. Tiempo: {elapsed_ms} ms."
        llm.last_error = None if result["ok"] else result["message"]
        llm.last_response_ms = elapsed_ms
        db.commit()
        return result, "agent-ai"
    if section == "ftp":
        ftp = get_or_create_settings(db, FTPSettings, user.company_id)
        try:
            FTPService().test_connection(ftp)
            result = {
                "ok": True,
                "message": (
                    f"Conexion {ftp.connection_type.upper()} correcta "
                    f"con {ftp.host}:{ftp.port}."
                ),
            }
        except Exception as exc:
            result = {
                "ok": False,
                "message": (
                    f"No se pudo conectar por {ftp.connection_type.upper()}: "
                    f"{exc}"
                ),
            }
        return result, "ftp"
    return {"ok": False, "message": f"Prueba no disponible para {section}."}, "email"
