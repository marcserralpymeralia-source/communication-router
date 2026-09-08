from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, require_tenant_role
from app.core.pagination import normalize_page
from app.core.templating import templates
from app.db.models import AuditLog, BackgroundJob, JobAttempt
from app.jobs.service import cancel_job, get_job, job_payload, job_trace, list_jobs, retry_job
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db
from app.logs.service import parse_audit_log_message

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _aware(value: datetime | None) -> datetime | None:
    if not value:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _serialize_job(job) -> dict:
    trace = job_trace(job)
    payload_preview = ""
    result_preview = ""
    try:
        payload_data = job_payload(job)
        if isinstance(payload_data, dict):
            payload_preview = ", ".join(f"{key}={value}" for key, value in list(payload_data.items())[:4])
    except json.JSONDecodeError:
        payload_preview = job.payload_json or ""
    try:
        result_data = json.loads(job.result_json or "{}")
        if isinstance(result_data, dict):
            result_preview = ", ".join(f"{key}={value}" for key, value in list(result_data.items())[:4])
    except json.JSONDecodeError:
        result_preview = job.result_json or ""
    now = datetime.now(timezone.utc)
    queued_at = _aware(job.queued_at)
    started_at = _aware(job.started_at)
    finished_at = _aware(job.finished_at)
    age_seconds = int((now - queued_at).total_seconds()) if queued_at else None
    runtime_seconds = None
    if started_at:
        end_time = finished_at or now
        runtime_seconds = max(int((end_time - started_at).total_seconds()), 0)
    lock_until = _aware(job.lock_until)
    lock_remaining_seconds = int((lock_until - now).total_seconds()) if lock_until and lock_until > now else 0
    heartbeat_at = _aware(job.last_heartbeat_at)
    heartbeat_age_seconds = int((now - heartbeat_at).total_seconds()) if heartbeat_at else None
    return {
        "id": job.id,
        "company_id": job.company_id,
        "job_type": job.job_type,
        "dedupe_key": job.dedupe_key,
        "status": job.status,
        "payload_json": job.payload_json,
        "result_json": job.result_json,
        "error_message": job.error_message,
        "progress": job.progress,
        "attempt_count": job.attempt_count,
        "retry_count": job.retry_count,
        "max_retries": job.max_retries,
        "max_attempts": (job.max_retries or 0) + 1,
        "lock_owner": job.lock_owner,
        "lock_until": job.lock_until,
        "next_retry_at": job.next_retry_at,
        "last_error_at": job.last_error_at,
        "last_error_type": job.last_error_type,
        "last_heartbeat_at": job.last_heartbeat_at,
        "queued_at": job.queued_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "age_seconds": age_seconds,
        "runtime_seconds": runtime_seconds,
        "lock_remaining_seconds": lock_remaining_seconds,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "payload_preview": payload_preview,
        "result_preview": result_preview,
        "trace_request_id": trace.get("request_id"),
        "trace_correlation_id": trace.get("correlation_id"),
        "trace_tenant_id": trace.get("tenant_id"),
        "trace_user_id": trace.get("user_id"),
        "trace_membership_id": trace.get("membership_id"),
        "trace_worker_id": trace.get("worker_id"),
    }


def _job_action_response(request: Request, payload: dict, fallback: str) -> JSONResponse | RedirectResponse:
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(jsonable_encoder(payload))
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


@router.get("")
def jobs_list(status: str = "all", limit: int = 50, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    jobs = list_jobs(db, user.company_id, status=status, limit=limit)
    return JSONResponse(jsonable_encoder({"items": [_serialize_job(job) for job in jobs]}))


@router.get("/monitor")
def jobs_monitor(
    request: Request,
    status: str = "all",
    job_type: str = "all",
    search: str = "",
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    page, page_size = normalize_page(page, page_size)
    base_stmt = select(BackgroundJob).where(BackgroundJob.company_id == user.company_id)
    if status and status != "all":
        base_stmt = base_stmt.where(BackgroundJob.status == status)
    if job_type and job_type != "all":
        base_stmt = base_stmt.where(BackgroundJob.job_type == job_type)
    if search:
        like = f"%{search}%"
        base_stmt = base_stmt.where(
            or_(
                BackgroundJob.job_type.ilike(like),
                BackgroundJob.status.ilike(like),
                BackgroundJob.error_message.ilike(like),
                BackgroundJob.payload_json.ilike(like),
                BackgroundJob.result_json.ilike(like),
            )
        )
    total_items = db.scalar(select(func.count()).select_from(base_stmt.subquery())) or 0
    start_index = (page - 1) * page_size
    paged_jobs = db.scalars(
        base_stmt.order_by(BackgroundJob.queued_at.desc().nullslast(), BackgroundJob.created_at.desc())
        .offset(start_index)
        .limit(page_size)
    ).all()
    serialized_jobs = [_serialize_job(job) for job in paged_jobs]
    counts = {
        "total": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id)) or 0,
        "queued": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "queued")) or 0,
        "running": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "running")) or 0,
        "retrying": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "retrying")) or 0,
        "failed": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "failed")) or 0,
        "cancelled": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "cancelled")) or 0,
        "success": db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.company_id == user.company_id, BackgroundJob.status == "success")) or 0,
    }
    job_types = db.scalars(
        select(BackgroundJob.job_type)
        .where(BackgroundJob.company_id == user.company_id)
        .distinct()
        .order_by(BackgroundJob.job_type)
    ).all()
    total_pages = (total_items + page_size - 1) // page_size if total_items else 0
    return templates.TemplateResponse(
        "jobs/monitor.html",
        {
            "request": request,
            "user": user,
            "jobs": serialized_jobs,
            "counts": counts,
            "job_types": job_types,
            "filters": {"status": status, "job_type": job_type, "search": search},
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_items": total_items,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1,
                "start_item": start_index + 1 if total_items else 0,
                "end_item": min(start_index + page_size, total_items),
                "allowed_page_sizes": (25, 50, 100),
            },
        },
    )


@router.get("/{job_id}/detail")
def jobs_detail_page(job_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    job = get_job(db, user.company_id, job_id)
    if not job:
        return JSONResponse({"detail": "No encontrado"}, status_code=404)
    serialized = _serialize_job(job)
    logs = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.company_id == user.company_id,
            AuditLog.entity_type == "job",
            AuditLog.entity_id == job.id,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(12)
    ).all()
    log_items = []
    for log in logs:
        parsed = parse_audit_log_message(log.message)
        context = parsed.get("context") or {}
        log_items.append(
            {
                "created_at": log.created_at,
                "created_label": log.created_at.strftime("%d/%m %H:%M") if log.created_at else "",
                "action": log.action,
                "message": parsed.get("message") or "",
                "request_id": context.get("request_id"),
                "correlation_id": context.get("correlation_id"),
                "job_id": context.get("job_id"),
                "worker_id": context.get("worker_id"),
            }
        )
    attempts = db.scalars(
        select(JobAttempt)
        .where(
            JobAttempt.company_id == user.company_id,
            JobAttempt.job_id == job.id,
        )
        .order_by(JobAttempt.attempt_number.asc(), JobAttempt.created_at.asc())
    ).all()
    attempt_items = [
        {
            "attempt_number": attempt.attempt_number,
            "status": attempt.status,
            "worker_id": attempt.worker_id,
            "started_at": attempt.started_at,
            "finished_at": attempt.finished_at,
            "duration_seconds": attempt.duration_seconds,
            "error_type": attempt.error_type,
            "error_message": attempt.error_message,
            "next_retry_at": attempt.next_retry_at,
        }
        for attempt in attempts
    ]
    return templates.TemplateResponse(
        "jobs/detail.html",
        {
            "request": request,
            "user": user,
            "job": serialized,
            "raw_job": job,
            "logs": log_items,
            "attempts": attempt_items,
            "request_id": getattr(request.state, "request_id", None),
            "correlation_id": getattr(request.state, "correlation_id", None) or serialized.get("trace_correlation_id") or getattr(request.state, "request_id", None),
        },
    )


@router.get("/{job_id}")
def jobs_detail(job_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    job = get_job(db, user.company_id, job_id)
    if not job:
        return JSONResponse({"detail": "No encontrado"}, status_code=404)
    return JSONResponse(jsonable_encoder(_serialize_job(job)))


@router.post("/{job_id}/retry")
def jobs_retry(request: Request, job_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(require_tenant_role("Administrador", "Supervisor", "Superadmin"))):
    job = retry_job(db, user.company_id, job_id)
    if not job:
        return JSONResponse({"detail": "No encontrado"}, status_code=404)
    payload = {"ok": True, "job": _serialize_job(job)}
    return _job_action_response(request, payload, f"/jobs/{job.id}/detail")


@router.post("/{job_id}/cancel")
def jobs_cancel(request: Request, job_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(require_tenant_role("Administrador", "Supervisor", "Superadmin"))):
    job = cancel_job(db, user.company_id, job_id)
    if not job:
        return JSONResponse({"detail": "No encontrado"}, status_code=404)
    payload = {"ok": True, "job": _serialize_job(job)}
    return _job_action_response(request, payload, f"/jobs/{job.id}/detail")
