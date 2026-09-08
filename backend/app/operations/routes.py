from __future__ import annotations

from datetime import datetime, timezone
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import require_tenant_role
from app.core.config import get_settings
from app.core.templating import templates
from app.db.models import BackgroundJob, Communication, Mailbox, RoutingAction
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
    }
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
            "recent_jobs": recent_jobs,
            "sync_rows": sync_rows,
            "worker": {
                "status": "Activo" if worker_active else "Sin actividad",
                "last_activity": _relative_label(latest_worker_activity),
            },
        },
    )
