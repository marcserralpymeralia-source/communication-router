from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.dependencies import current_tenant_user, require_master_admin
from app.admin.diagnostics import company_diagnostics, company_diagnostics_overview
from app.core.metrics import snapshot_metrics
from app.core.config import get_settings
from app.core.storage import ensure_directory, resolve_temp_storage_dir
from app.master.database import get_master_db
from app.master.migrations import master_migration_report
from app.tenancy.database import get_tenant_db
from app.tenancy.database import tenant_db_session
from app.tenancy.migrations import tenant_migration_report
from app.db.models import BackgroundJob, WorkerHeartbeat
from app.workers.email_worker import is_email_sync_worker_started
from app.workers.jobs_worker import is_job_worker_started

router = APIRouter()


def _ping_db(db: Session) -> bool:
    db.execute(text("SELECT 1"))
    return True


def _storage_probe() -> dict[str, object]:
    path = resolve_temp_storage_dir(".health")
    try:
        ensure_directory(path)
        with tempfile.NamedTemporaryFile(prefix="probe-", dir=path, delete=True) as handle:
            handle.write(b"ok")
            handle.flush()
            handle.seek(0)
            if handle.read() != b"ok":
                return {"ok": False, "status": "unavailable", "error_type": "StorageReadError"}
        return {"ok": True, "status": "ready"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "status": "unavailable", "error_type": exc.__class__.__name__}


def _worker_health(db: Session, company_id: int) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    ttl = max(int(getattr(get_settings(), "job_worker_poll_seconds", 10)) * 3, 60)
    heartbeat = db.scalar(
        select(WorkerHeartbeat).where(
            WorkerHeartbeat.company_id == company_id,
            WorkerHeartbeat.worker_kind == "jobs",
        )
    )
    pending = db.scalar(
        select(func.count(BackgroundJob.id)).where(
            BackgroundJob.company_id == company_id,
            BackgroundJob.status.in_(("queued", "running", "retrying")),
        )
    ) or 0
    if heartbeat is None:
        return {
            "required": bool(pending),
            "status": "fail" if pending else "warn",
            "last_heartbeat_at": None,
            "pending_jobs": pending,
        }
    last = heartbeat.last_heartbeat_at
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    fresh = bool(last and now - last <= timedelta(seconds=ttl) and heartbeat.status == "active")
    status = "pass" if fresh else ("fail" if pending else "warn")
    return {
        "required": bool(pending),
        "status": status,
        "last_heartbeat_at": last,
        "worker_id": heartbeat.worker_id,
        "pending_jobs": pending,
    }


@router.get("/health")
def health(request: Request, master_db: Session = Depends(get_master_db)):
    return health_ready(request, master_db)


@router.get("/health/live")
def health_live(request: Request):
    return {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "request_id": getattr(request.state, "request_id", None),
        "correlation_id": getattr(request.state, "correlation_id", None) or getattr(request.state, "request_id", None),
        "metrics": snapshot_metrics(),
    }


@router.get("/health/ready")
def health_ready(request: Request, master_db: Session = Depends(get_master_db)):
    tenant = getattr(request.state, "tenant", None)
    try:
        master_schema = master_migration_report(master_db, persist=False)
        master_ping = _ping_db(master_db)
        master_error = None
    except SQLAlchemyError as exc:
        master_ping = False
        master_error = exc.__class__.__name__
        master_schema = {"status": "unavailable", "is_current": False, "error_type": master_error}
    payload = {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "master": master_ping,
        "master_schema_report": master_schema,
        "tenant": bool(tenant),
        "tenant_company_id": tenant.company.id if tenant else None,
        "tenant_slug": tenant.company.slug if tenant else None,
        "tenant_database_configured": bool(tenant and getattr(tenant.company, "database_url", None)),
        "request_id": getattr(request.state, "request_id", None),
        "correlation_id": getattr(request.state, "correlation_id", None) or getattr(request.state, "request_id", None),
        "metrics": snapshot_metrics(),
        "storage_ready": True,
        "workers_ready": {"email_sync": is_email_sync_worker_started(), "jobs": is_job_worker_started()},
    }
    worker_health = None
    if master_error:
        payload["master_error_type"] = master_error
    if tenant and tenant.company.database_url:
        session_factory = tenant_db_session(tenant.company.database_url)
        tenant_db = session_factory()
        try:
            try:
                payload["tenant_ping"] = _ping_db(tenant_db)
            except SQLAlchemyError as exc:
                payload["tenant_ping"] = False
                payload["tenant_error_type"] = exc.__class__.__name__
            try:
                payload["tenant_schema_report"] = tenant_migration_report(tenant_db, tenant.company.id, persist=False)
            except SQLAlchemyError as exc:
                payload["tenant_schema_report"] = {
                    "status": "unavailable",
                    "is_current": False,
                    "error_type": exc.__class__.__name__,
                }
            try:
                worker_health = _worker_health(tenant_db, tenant.company.id)
            except SQLAlchemyError:
                worker_health = {"required": True, "status": "fail", "last_heartbeat_at": None}
        finally:
            tenant_db.close()
    storage = _storage_probe()
    payload["storage"] = storage
    payload["storage_ready"] = bool(storage["ok"])
    master_schema_ok = bool(payload["master_schema_report"].get("is_current"))
    if master_db.get_bind().dialect.name == "sqlite":
        master_schema_ok = True
    payload["master_schema_ok"] = master_schema_ok
    payload["ok"] = bool(payload["master"] and (not tenant or payload.get("tenant_ping", True)) and master_schema_ok)
    if tenant and tenant.company.database_url:
        tenant_schema_ok = bool(payload["tenant_schema_report"].get("is_current"))
        if worker_health is None:
            worker_health = {"required": True, "status": "fail", "last_heartbeat_at": None}
        payload["worker_health"] = {"jobs": worker_health}
        if tenant.company.database_url.startswith("sqlite"):
            tenant_schema_ok = True
        payload["tenant_schema_ok"] = tenant_schema_ok
        payload["ok"] = payload["ok"] and tenant_schema_ok and payload["storage_ready"] and payload["worker_health"]["jobs"]["status"] != "fail"
    return payload


@router.get("/health/master")
def master_health(master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return {"ok": True, "timestamp": datetime.now(timezone.utc), "master": _ping_db(master_db), "metrics": snapshot_metrics()}


@router.get("/health/tenant")
def tenant_health(db: Session = Depends(get_tenant_db), user=Depends(current_tenant_user)):
    schema_report = tenant_migration_report(db, user.company_id)
    return {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "company_id": user.company_id,
        "tenant": _ping_db(db),
        "schema_report": schema_report,
        "metrics": snapshot_metrics(),
    }


@router.get("/admin/tenants/{company_id}/health")
def admin_tenant_health(company_id: int, master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return company_diagnostics(master_db, company_id)


@router.get("/admin/tenants")
def admin_tenants(master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return {"items": company_diagnostics_overview(master_db)}


@router.get("/health/metrics")
def health_metrics():
    return {"ok": True, "timestamp": datetime.now(timezone.utc), "metrics": snapshot_metrics()}
