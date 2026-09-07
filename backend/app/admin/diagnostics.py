from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import BackgroundJob, Company, Conversation, Customer, Email, EmailSettings, InboundMessage, InputChannel, LLMSettings, Order, Product, PromptExecution
from app.core.metrics import snapshot_metrics
from app.master.models import MasterCompany, MasterTenantDatabase
from app.settings.email_config import email_config_status
from app.settings.service import get_or_create_settings
from app.tenancy.database import tenant_db_session
from app.tenancy.migrations import tenant_migration_report


def _zero_payload(company: Company | None, tenant_db: MasterTenantDatabase | None) -> dict:
    return {
        "customers_total": 0,
        "products_total": 0,
        "orders_total": 0,
        "emails_total": 0,
        "inbound_total": 0,
        "conversations_total": 0,
        "active_channels_total": 0,
        "jobs_total": 0,
        "jobs_queued": 0,
        "jobs_running": 0,
        "jobs_retrying": 0,
        "jobs_failed": 0,
        "jobs_cancelled": 0,
        "jobs_success": 0,
        "imap_ready": False,
        "smtp_ready": False,
        "llm_ready": False,
        "last_sync_at": None,
        "last_sync_ok": None,
        "last_sync_message": None,
        "email_status": None,
        "schema_report": {
            "version": None,
            "current_version": None,
            "status": "missing" if tenant_db else "missing_tenant",
            "last_checked_at": None,
            "applied_at": None,
            "last_error": None,
            "is_current": False,
        },
        "observability": snapshot_metrics(),
    }


def company_diagnostics(master_db: Session, company_id: int) -> dict:
    company = master_db.get(MasterCompany, company_id)
    tenant = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company_id))
    base = {
        "company_id": company_id,
        "company_name": company.name if company else "",
        "company_slug": company.slug if company else "",
        "company_active": bool(company.active) if company else False,
        "tenant_database_id": tenant.id if tenant else None,
        "tenant_database_configured": bool(tenant and tenant.get_database_url()),
        "tenant_database_status": tenant.health_status if tenant else "missing",
        "tenant_database_provisioned_at": tenant.provisioned_at if tenant else None,
        "tenant_database_last_health_check_at": tenant.last_health_check_at if tenant else None,
        "tenant_database_notes": tenant.notes if tenant else None,
    }
    database_url = tenant.get_database_url() if tenant else None
    if not database_url:
        return {**base, **_zero_payload(company, tenant)}

    session_factory = tenant_db_session(database_url)
    db = session_factory()
    try:
        email_settings = get_or_create_settings(db, EmailSettings, company_id)
        llm_settings = get_or_create_settings(db, LLMSettings, company_id)
        schema_report = tenant_migration_report(db, company_id)
        prompt_executions = db.scalars(
            select(PromptExecution)
            .where(
                PromptExecution.company_id == company_id,
                PromptExecution.prompt_purpose.in_(["classification", "extraction"]),
            )
            .order_by(PromptExecution.id.desc())
            .limit(6)
        ).all()
        return {
            **base,
            "customers_total": db.scalar(select(func.count()).select_from(Customer).where(Customer.company_id == company_id)) or 0,
            "products_total": db.scalar(select(func.count()).select_from(Product).where(Product.company_id == company_id)) or 0,
            "orders_total": db.scalar(select(func.count()).select_from(Order).where(Order.company_id == company_id)) or 0,
            "emails_total": db.scalar(select(func.count()).select_from(Email).where(Email.company_id == company_id)) or 0,
            "inbound_total": db.scalar(select(func.count()).select_from(InboundMessage).where(InboundMessage.company_id == company_id)) or 0,
            "conversations_total": db.scalar(select(func.count()).select_from(Conversation).where(Conversation.company_id == company_id)) or 0,
            "active_channels_total": db.scalar(select(func.count()).select_from(InputChannel).where(InputChannel.company_id == company_id, InputChannel.is_active == True)) or 0,  # noqa: E712
            "jobs_total": db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.company_id == company_id)) or 0,
            "jobs_queued": db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.company_id == company_id, BackgroundJob.status == "queued")) or 0,
            "jobs_running": db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.company_id == company_id, BackgroundJob.status == "running")) or 0,
            "jobs_retrying": db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.company_id == company_id, BackgroundJob.status == "retrying")) or 0,
            "jobs_failed": db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.company_id == company_id, BackgroundJob.status == "failed")) or 0,
            "jobs_cancelled": db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.company_id == company_id, BackgroundJob.status == "cancelled")) or 0,
            "jobs_success": db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.company_id == company_id, BackgroundJob.status == "success")) or 0,
            "imap_ready": email_config_status(email_settings)["imap_ready"],
            "smtp_ready": email_config_status(email_settings)["smtp_ready"],
            "llm_ready": bool(llm_settings.provider and llm_settings.provider != "disabled" and llm_settings.api_key_encrypted),
            "last_sync_at": email_settings.last_sync_at,
            "last_sync_ok": email_settings.last_sync_ok,
            "last_sync_message": email_settings.last_sync_message,
            "email_status": email_config_status(email_settings),
            "schema_report": schema_report,
            "prompt_executions": [
                {
                    "id": execution.id,
                    "purpose": execution.prompt_purpose,
                    "status": execution.output_status,
                    "model": execution.model,
                    "validation_errors": execution.validation_errors_json,
                    "duration_ms": execution.duration_ms,
                    "response_excerpt": execution.response_excerpt,
                    "created_at": execution.created_at,
                }
                for execution in prompt_executions
            ],
            "observability": snapshot_metrics(),
        }
    finally:
        db.close()


def company_diagnostics_overview(master_db: Session) -> list[dict]:
    companies = master_db.scalars(select(MasterCompany).order_by(MasterCompany.name)).all()
    return [company_diagnostics(master_db, company.id) for company in companies]
