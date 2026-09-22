from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.auth.dependencies import require_tenant_role
from app.core.config import get_settings
from app.core.templating import templates
from app.db.models import (
    BackgroundJob,
    Communication,
    Department,
    Mailbox,
    PromptExecution,
    RoutingAction,
    RoutingDecision,
)
from app.master.database import get_master_db
from app.master.models import MailboxSyncState
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db


router = APIRouter(prefix="/operations", tags=["operations"])
OPERATIONS_ROLES = ("Administrador", "Supervisor", "Superadmin")


def _safe_error(value: str | None) -> str:
    if not value:
        return "Sin detalle"
    first_line = value.strip().splitlines()[0] if value.strip() else "Sin detalle"
    redacted = re.sub(
        r"(?i)(api[_ -]?key|password|token|authorization)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        first_line,
    )
    return redacted[:220]


def _operation_status(job: BackgroundJob) -> str:
    if job.status == "retrying":
        return "Transitorio"
    if job.status == "failed" and job.retry_count < (job.max_retries or 0):
        return "Requiere intervención"
    if job.status == "failed":
        return "Permanente"
    return "En curso"


def _relative_label(value: datetime | None) -> str:
    if value is None:
        return "Sin actividad"
    current = datetime.now(timezone.utc)
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    seconds = max(int((current - aware).total_seconds()), 0)
    if seconds < 60:
        return "Hace menos de 1 min"
    minutes = seconds // 60
    if minutes < 60:
        return f"Hace {minutes} min"
    hours = minutes // 60
    return f"Hace {hours} h"


def _communication_metrics(db: Session, company_id: int) -> dict[str, object]:
    company_filter = Communication.company_id == company_id
    recent_since = datetime.now(timezone.utc) - timedelta(hours=24)
    imported_total = db.scalar(select(func.count(Communication.id)).where(company_filter)) or 0
    received_total = db.scalar(
        select(func.count(Communication.id)).where(company_filter, Communication.received_at.is_not(None))
    ) or 0
    missing_received_at = imported_total - received_total
    recent_imported = db.scalar(
        select(func.count(Communication.id)).where(company_filter, Communication.created_at >= recent_since)
    ) or 0
    recent_received = db.scalar(
        select(func.count(Communication.id)).where(company_filter, Communication.received_at >= recent_since)
    ) or 0
    return {
        "imported_total": imported_total,
        "missing_received_at": missing_received_at,
        "recent_imported": recent_imported,
        "recent_received": recent_received,
        "latest_received_at": db.scalar(select(func.max(Communication.received_at)).where(company_filter)),
        "latest_import_at": db.scalar(select(func.max(Communication.created_at)).where(company_filter)),
    }


def _routing_metrics(db: Session, company_id: int) -> dict[str, object]:
    company_filter = RoutingDecision.company_id == company_id
    primary_department = aliased(Department)
    alternative_department = aliased(Department)
    final_department = aliased(Department)
    coverage_query = (
        select(func.count(func.distinct(RoutingDecision.id)))
        .select_from(RoutingDecision)
        .outerjoin(
            primary_department,
            and_(
                primary_department.company_id == company_id,
                primary_department.id == RoutingDecision.department_id,
            ),
        )
        .outerjoin(
            alternative_department,
            and_(
                alternative_department.company_id == company_id,
                alternative_department.id == RoutingDecision.alternative_department_id,
            ),
        )
        .outerjoin(
            final_department,
            and_(
                final_department.company_id == company_id,
                final_department.id == RoutingDecision.final_department_id,
            ),
        )
        .where(
            company_filter,
            or_(
                primary_department.destination_email.is_not(None),
                alternative_department.destination_email.is_not(None),
                final_department.destination_email.is_not(None),
            ),
        )
    )
    total = db.scalar(select(func.count(RoutingDecision.id)).where(company_filter)) or 0
    coverage = db.scalar(coverage_query) or 0
    return {
        "total": total,
        "with_destination": coverage,
        "coverage_percent": round((coverage / total) * 100, 1) if total else None,
        "pending_review": db.scalar(
            select(func.count(RoutingDecision.id)).where(company_filter, RoutingDecision.requires_review.is_(True))
        ) or 0,
        "without_primary": db.scalar(
            select(func.count(RoutingDecision.id)).where(company_filter, RoutingDecision.department_id.is_(None))
        ) or 0,
        "with_alternative": db.scalar(
            select(func.count(RoutingDecision.id)).where(
                company_filter, RoutingDecision.alternative_department_id.is_not(None)
            )
        ) or 0,
        "prompt_executions": db.scalar(select(func.count(PromptExecution.id)).where(PromptExecution.company_id == company_id)) or 0,
    }


@router.get("")
def operations_page(
    request: Request,
    db: Session = Depends(get_tenant_db),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(require_tenant_role(*OPERATIONS_ROLES)),
):
    if get_settings().app_slug.strip().lower() != "kibak":
        raise HTTPException(status_code=404, detail="No encontrado")

    company_filter = BackgroundJob.company_id == user.company_id
    counts = {
        "pending": db.scalar(select(func.count(BackgroundJob.id)).where(company_filter, BackgroundJob.status.in_(("queued", "running", "retrying")))) or 0,
        "failed": db.scalar(select(func.count(BackgroundJob.id)).where(company_filter, BackgroundJob.status == "failed")) or 0,
        "routing_errors": db.scalar(
            select(func.count(Communication.id)).where(
                Communication.company_id == user.company_id,
                or_(Communication.processing_status == "error", Communication.routing_status == "routing_error"),
            )
        ) or 0,
        "forwarding_errors": db.scalar(
            select(func.count(RoutingAction.id)).where(RoutingAction.company_id == user.company_id, RoutingAction.status == "failed")
        ) or 0,
        "simulated_actions": db.scalar(
            select(func.count(RoutingAction.id)).where(RoutingAction.company_id == user.company_id, RoutingAction.status == "simulated")
        ) or 0,
    }
    communication_metrics = _communication_metrics(db, user.company_id)
    routing_metrics = _routing_metrics(db, user.company_id)
    jobs = db.scalars(
        select(BackgroundJob)
        .where(company_filter)
        .order_by(BackgroundJob.updated_at.desc(), BackgroundJob.id.desc())
        .limit(8)
    ).all()
    recent_jobs = [
        {
            "id": job.id,
            "type": job.job_type.replace("_", " ").title(),
            "status": job.status,
            "status_label": job.status.replace("_", " ").title(),
            "operation_status": _operation_status(job),
            "error": _safe_error(job.error_message),
            "updated_at": job.updated_at,
            "retryable": job.status in {"failed", "cancelled", "retrying"},
        }
        for job in jobs
    ]
    mailboxes = {item.id: item for item in db.scalars(select(Mailbox).where(Mailbox.company_id == user.company_id)).all()}
    states = master_db.scalars(
        select(MailboxSyncState)
        .where(MailboxSyncState.company_id == user.company_id)
        .order_by(MailboxSyncState.updated_at.desc(), MailboxSyncState.id.desc())
    ).all()
    sync_rows = []
    activity_times = []
    for state in states:
        mailbox = mailboxes.get(state.mailbox_id)
        if not mailbox:
            continue
        latest_activity = max(
            (item for item in (state.last_sync_at, state.listener_last_heartbeat_at, state.updated_at) if item is not None),
            default=None,
        )
        if latest_activity:
            activity_times.append(latest_activity)
        sync_rows.append(
            {
                "name": mailbox.name or mailbox.email_address,
                "enabled": state.enabled,
                "status": state.sync_status or state.status,
                "status_label": (state.sync_status or state.status or "idle").replace("_", " ").title(),
                "listener_status": state.listener_status,
                "last_activity": _relative_label(latest_activity),
                "error": _safe_error(state.last_error_message or state.listener_last_error_message) if (state.last_error_message or state.listener_last_error_message) else "",
            }
        )
    latest_worker_activity = max(activity_times, default=None)
    worker_active = any(item["listener_status"] in {"active", "running"} for item in sync_rows)
    return templates.TemplateResponse(
        "operations/index.html",
        {
            "request": request,
            "user": user,
            "title": "Operaciones",
            "counts": counts,
            "communication_metrics": communication_metrics,
            "routing_metrics": routing_metrics,
            "recent_jobs": recent_jobs,
            "sync_rows": sync_rows,
            "worker": {
                "status": "Activo" if worker_active else "Sin actividad",
                "last_activity": _relative_label(latest_worker_activity),
            },
        },
    )
