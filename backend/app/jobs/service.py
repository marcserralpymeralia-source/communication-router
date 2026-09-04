from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.observability import encode_trace_payload, split_trace_payload
from app.db.models import BackgroundJob, JobAttempt, User


logger = logging.getLogger(__name__)

ACTIVE_JOB_STATUSES = {"queued", "running", "retrying"}
TERMINAL_JOB_STATUSES = {"success", "failed", "cancelled"}
DEFAULT_MAX_RETRIES = {
    "email_sync": 5,
    "process_recent_emails": 5,
    "backfill_imap": 5,
    "process_pending_emails": 4,
    "process_email": 3,
    "process_order": 3,
    "process_inbound_message": 3,
    "download_whatsapp_media": 3,
    "export_order": 3,
    "export_order_ftp": 3,
    "bulk_order_action": 2,
    "import_confirm": 1,
    "import_file": 1,
    "index_product_embeddings": 1,
    "index_knowledge_entries": 1,
    "route_communication": 3,
    "forward_communication": 3,
}
FORBIDDEN_PAYLOAD_KEYS = {
    "password",
    "api_key",
    "client_secret",
    "refresh_token",
    "access_token",
    "smtp_password",
    "imap_password",
    "private_key",
    "database_url",
    "dsn",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if not value:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _dumps(payload: dict | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _loads(payload_json: str | None) -> dict:
    if not payload_json:
        return {}
    try:
        data = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_payload(payload: dict | None) -> dict:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("Job payload must be a dictionary.")
    try:
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except TypeError as exc:  # pragma: no cover - validated by tests
        raise ValueError("Job payload must be JSON serializable.") from exc
    stack = [payload]
    while stack:
        current = stack.pop()
        for key, value in current.items():
            if str(key).lower() in FORBIDDEN_PAYLOAD_KEYS:
                raise ValueError("Job payload contains forbidden secret data.")
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, dict))
    return payload


def _settings():
    return get_settings()


def _safe_created_by_user_id(db: Session, created_by_user_id: int | None) -> int | None:
    if created_by_user_id is None:
        return None
    try:
        if db.get(User, created_by_user_id) is None:
            return None
    except Exception:
        return None
    return created_by_user_id


def _retry_budget(job: BackgroundJob) -> int:
    return max(0, int(job.max_retries or 0))


def _max_attempts(job: BackgroundJob) -> int:
    return _retry_budget(job) + 1


def _retry_delay_seconds(job: BackgroundJob) -> int:
    settings = _settings()
    attempt_index = max(job.retry_count, 0)
    return min(settings.job_retry_max_seconds, settings.job_retry_base_seconds * (2**attempt_index))


def _latest_attempt(db: Session, job_id: int) -> JobAttempt | None:
    return db.scalar(
        select(JobAttempt)
        .where(JobAttempt.job_id == job_id)
        .order_by(JobAttempt.attempt_number.desc(), JobAttempt.created_at.desc())
    )


def _finalize_attempt(
    db: Session,
    job: BackgroundJob,
    *,
    status: str,
    error_type: str | None = None,
    error_message: str | None = None,
    next_retry_at: datetime | None = None,
) -> None:
    attempt = _latest_attempt(db, job.id)
    if not attempt:
        return
    now = _now()
    started_at = _aware(attempt.started_at) or _aware(job.started_at) or now
    attempt.status = status
    attempt.finished_at = now
    attempt.duration_seconds = max(int((now - started_at).total_seconds()), 0)
    attempt.error_type = error_type
    attempt.error_message = error_message[:2000] if error_message else None
    attempt.next_retry_at = next_retry_at
    attempt.worker_id = job.lock_owner or attempt.worker_id
    db.flush()


def build_dedupe_key(job_type: str, payload: dict | None = None) -> str:
    raw_payload = _dumps(payload or {})
    digest = hashlib.sha1(f"{job_type}:{raw_payload or '{}'}".encode("utf-8")).hexdigest()
    return digest


def enqueue_job(
    db: Session,
    *,
    company_id: int,
    job_type: str,
    payload: dict | None = None,
    created_by_user_id: int | None = None,
    dedupe_key: str | None = None,
    max_retries: int | None = None,
) -> BackgroundJob:
    normalized_payload = _safe_payload(payload)
    key = dedupe_key or build_dedupe_key(job_type, normalized_payload)
    existing = db.scalars(
        select(BackgroundJob).where(
            BackgroundJob.company_id == company_id,
            BackgroundJob.job_type == job_type,
            BackgroundJob.dedupe_key == key,
        )
    ).first()
    if existing:
        return existing
    settings = _settings()
    configured_max_retries = max(0, int((max_retries if max_retries is not None else DEFAULT_MAX_RETRIES.get(job_type, 3))))
    max_retries_final = min(configured_max_retries, max(0, settings.job_max_attempts - 1))
    stored_payload = encode_trace_payload(normalized_payload)
    safe_created_by_user_id = _safe_created_by_user_id(db, created_by_user_id)
    job = BackgroundJob(
        company_id=company_id,
        job_type=job_type,
        dedupe_key=key,
        status="queued",
        payload_json=_dumps(stored_payload),
        created_by_user_id=safe_created_by_user_id,
        max_retries=max_retries_final,
        queued_at=_now(),
        attempt_count=0,
        updated_at=_now(),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalars(
            select(BackgroundJob).where(
                BackgroundJob.company_id == company_id,
                BackgroundJob.job_type == job_type,
                BackgroundJob.dedupe_key == key,
            )
        ).first()
        if existing:
            return existing
        raise
    db.refresh(job)
    return job


def list_jobs(
    db: Session,
    company_id: int,
    status: str | None = None,
    limit: int = 50,
    job_type: str | None = None,
    search: str | None = None,
) -> list[BackgroundJob]:
    stmt = select(BackgroundJob).where(BackgroundJob.company_id == company_id)
    if status and status != "all":
        stmt = stmt.where(BackgroundJob.status == status)
    if job_type and job_type != "all":
        stmt = stmt.where(BackgroundJob.job_type == job_type)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            or_(
                BackgroundJob.job_type.ilike(like),
                BackgroundJob.status.ilike(like),
                BackgroundJob.error_message.ilike(like),
                BackgroundJob.last_error_type.ilike(like),
                BackgroundJob.dedupe_key.ilike(like),
                BackgroundJob.payload_json.ilike(like),
                BackgroundJob.result_json.ilike(like),
            )
        )
    return db.scalars(stmt.order_by(BackgroundJob.queued_at.desc()).limit(max(min(limit, 200), 1))).all()


def get_job(db: Session, company_id: int, job_id: int) -> BackgroundJob | None:
    job = db.get(BackgroundJob, job_id)
    if not job or job.company_id != company_id:
        return None
    return job


def cancel_job(db: Session, company_id: int, job_id: int) -> BackgroundJob | None:
    job = get_job(db, company_id, job_id)
    if not job:
        return None
    if job.status == "running":
        return None
    if job.status in TERMINAL_JOB_STATUSES:
        return job
    job.status = "cancelled"
    job.finished_at = _now()
    job.updated_at = _now()
    job.lock_until = None
    job.lock_owner = None
    job.next_retry_at = None
    db.commit()
    db.refresh(job)
    return job


def claim_next_job(db: Session, *, owner: str, job_types: set[str] | None = None) -> BackgroundJob | None:
    now = _now()
    settings = _settings()
    conditions = [
        BackgroundJob.status.in_(("queued", "retrying")),
        or_(BackgroundJob.lock_until.is_(None), BackgroundJob.lock_until <= now),
        or_(BackgroundJob.next_retry_at.is_(None), BackgroundJob.next_retry_at <= now),
    ]
    if job_types:
        conditions.append(BackgroundJob.job_type.in_(sorted(job_types)))
    candidate_id = db.scalar(
        select(BackgroundJob.id)
        .where(*conditions)
        .order_by(BackgroundJob.queued_at.asc(), BackgroundJob.created_at.asc())
        .limit(1)
    )
    if not candidate_id:
        return None
    result = db.execute(
        update(BackgroundJob)
        .where(
            BackgroundJob.id == candidate_id,
            BackgroundJob.status.in_(("queued", "retrying")),
            or_(BackgroundJob.lock_until.is_(None), BackgroundJob.lock_until <= now),
            or_(BackgroundJob.next_retry_at.is_(None), BackgroundJob.next_retry_at <= now),
        )
        .values(
            status="running",
            started_at=now,
            lock_owner=owner,
            lock_until=now + timedelta(seconds=settings.job_stale_after_seconds),
            attempt_count=BackgroundJob.attempt_count + 1,
            last_heartbeat_at=now,
            updated_at=now,
        )
        .returning(BackgroundJob.id)
    )
    row = result.first()
    if not row:
        db.rollback()
        return None
    job = db.get(BackgroundJob, row[0])
    if not job:
        db.rollback()
        return None
    db.add(
        JobAttempt(
            company_id=job.company_id,
            job_id=job.id,
            attempt_number=job.attempt_count,
            worker_id=owner,
            status="running",
            started_at=now,
        )
    )
    db.commit()
    db.refresh(job)
    return job


def _is_stale_running_job(job: BackgroundJob, *, now: datetime, stale_after_seconds: int) -> bool:
    if job.status != "running":
        return False
    stale_before = now - timedelta(seconds=stale_after_seconds)
    lock_until = _aware(job.lock_until)
    started_at = _aware(job.started_at)
    heartbeat_at = _aware(job.last_heartbeat_at)
    return bool(
        (lock_until is not None and lock_until <= now)
        or (lock_until is None and started_at is not None and started_at <= stale_before)
        or (heartbeat_at is not None and heartbeat_at <= stale_before)
    )


def _recover_stale_job(db: Session, job: BackgroundJob, *, now: datetime, owner: str | None = None) -> BackgroundJob:
    lock_owner = owner or job.lock_owner
    next_retry_at = now + timedelta(seconds=_retry_delay_seconds(job))
    attempt = _latest_attempt(db, job.id)
    if attempt:
        attempt.status = "abandoned"
        attempt.finished_at = now
        attempt.duration_seconds = max(int((now - (_aware(attempt.started_at) or _aware(job.started_at) or now)).total_seconds()), 0)
        attempt.error_type = "stale_worker"
        attempt.error_message = "El worker anterior supero el tiempo maximo y el job fue recuperado."
        attempt.next_retry_at = next_retry_at
        attempt.worker_id = lock_owner

    job.lock_owner = None
    job.lock_until = None
    job.last_heartbeat_at = now
    job.last_error_at = now
    job.last_error_type = "stale_worker"
    job.error_message = "El worker anterior supero el tiempo maximo y el job fue recuperado."
    job.next_retry_at = None
    job.updated_at = now
    if job.attempt_count < _max_attempts(job):
        job.status = "retrying"
        job.retry_count += 1
        job.next_retry_at = next_retry_at
    else:
        job.status = "failed"
        job.finished_at = now
    return job


def recover_stale_job(db: Session, job: BackgroundJob, *, owner: str | None = None) -> BackgroundJob | None:
    now = _now()
    settings = _settings()
    if not _is_stale_running_job(job, now=now, stale_after_seconds=settings.job_stale_after_seconds):
        return None
    _recover_stale_job(db, job, now=now, owner=owner)
    db.commit()
    db.refresh(job)
    return job


def recover_stale_jobs(db: Session, *, owner: str | None = None, job_types: set[str] | None = None) -> list[BackgroundJob]:
    now = _now()
    settings = _settings()
    stale_before = now - timedelta(seconds=settings.job_stale_after_seconds)
    conditions = [
        BackgroundJob.status == "running",
        or_(
            BackgroundJob.lock_until <= now,
            and_(BackgroundJob.lock_until.is_(None), BackgroundJob.started_at.is_not(None), BackgroundJob.started_at <= stale_before),
            and_(BackgroundJob.last_heartbeat_at.is_not(None), BackgroundJob.last_heartbeat_at <= stale_before),
        ),
    ]
    if job_types:
        conditions.append(BackgroundJob.job_type.in_(sorted(job_types)))
    jobs = db.scalars(select(BackgroundJob).where(*conditions)).all()
    recovered: list[BackgroundJob] = []
    for job in jobs:
        _recover_stale_job(db, job, now=now, owner=owner)
        recovered.append(job)
    if recovered:
        db.commit()
        for job in recovered:
            db.refresh(job)
    return recovered


def start_job_execution(
    db: Session,
    job: BackgroundJob,
    *,
    owner: str,
) -> BackgroundJob:
    now = _now()
    settings = _settings()

    job.status = "running"
    job.started_at = now
    job.finished_at = None
    job.lock_owner = owner
    job.lock_until = now + timedelta(seconds=settings.job_stale_after_seconds)
    job.attempt_count = int(job.attempt_count or 0) + 1
    job.last_heartbeat_at = now
    job.updated_at = now
    job.next_retry_at = None

    db.add(
        JobAttempt(
            company_id=job.company_id,
            job_id=job.id,
            attempt_number=job.attempt_count,
            worker_id=owner,
            status="running",
            started_at=now,
        )
    )
    db.commit()
    db.refresh(job)
    return job


def finish_job(db: Session, job: BackgroundJob, result: dict | None = None) -> BackgroundJob:
    now = _now()
    job.status = "success"
    job.result_json = _dumps(result or {})
    job.error_message = None
    job.last_error_at = None
    job.last_error_type = None
    job.next_retry_at = None
    job.finished_at = now
    job.updated_at = now
    job.progress = 100
    job.lock_owner = None
    job.lock_until = None
    job.last_heartbeat_at = now
    _finalize_attempt(db, job, status="succeeded")
    db.commit()
    db.refresh(job)
    return job


def fail_job(
    db: Session,
    job: BackgroundJob,
    error_message: str,
    *,
    retry: bool = False,
    error_type: str | None = None,
    result: dict | None = None,
) -> BackgroundJob:
    now = _now()
    settings = _settings()
    backoff_seconds = min(settings.job_retry_max_seconds, settings.job_retry_base_seconds * (2 ** max(job.retry_count, 0)))
    next_retry_at = now + timedelta(seconds=backoff_seconds)
    if retry and job.attempt_count < _max_attempts(job):
        job.status = "retrying"
        job.retry_count += 1
        job.lock_owner = None
        job.lock_until = None
        job.next_retry_at = next_retry_at
    else:
        job.status = "failed"
        job.finished_at = now
        job.lock_owner = None
        job.lock_until = None
        job.next_retry_at = None
    job.error_message = error_message[:2000]
    if result is not None:
        job.result_json = _dumps(result)
    job.last_error_at = now
    job.last_error_type = error_type or ("retryable" if retry else "fatal")
    job.last_heartbeat_at = now
    job.updated_at = now
    _finalize_attempt(
        db,
        job,
        status="retry_scheduled" if job.status == "retrying" else "failed_permanent",
        error_type=job.last_error_type,
        error_message=error_message,
        next_retry_at=job.next_retry_at,
    )
    db.commit()
    db.refresh(job)
    return job


def execute_job_inline(db: Session, job: BackgroundJob) -> dict:
    from app.workers.jobs_worker import _process_job

    try:
        if job.status == "running":
            recovered = recover_stale_job(db, job, owner=f"inline:{job.id}")
            if recovered is None:
                return {
                    "ok": False,
                    "message": "El job ya esta en ejecucion.",
                    "error_type": "job_already_running",
                }

        job = start_job_execution(db, job, owner=f"inline:{job.id}")
        result = _process_job(db, job)
        if not isinstance(result, dict):
            result = {"ok": True, "message": str(result) if result is not None else "Trabajo completado"}
        if result.get("ok") is False:
            message = str(result.get("message") or "El job devolvió ok=false.")
            fail_job(db, job, message, retry=False, error_type=str(result.get("error_type") or "job_failed"), result=result)
        else:
            finish_job(db, job, result)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "job.inline.failed",
            extra={
                "event": "job.inline.failed",
                "job_id": job.id,
                "job_type": job.job_type,
                "company_id": job.company_id,
                "error_type": exc.__class__.__name__,
            },
        )
        failure = {"ok": False, "message": str(exc), "error_type": exc.__class__.__name__}
        fail_job(db, job, str(exc), retry=False, error_type=exc.__class__.__name__, result=failure)
        return failure


def retry_job(db: Session, company_id: int, job_id: int) -> BackgroundJob | None:
    job = get_job(db, company_id, job_id)
    if not job or job.status in {"running", "success"}:
        return None
    now = _now()
    job.status = "queued"
    job.error_message = None
    job.result_json = None
    job.progress = 0
    job.started_at = None
    job.finished_at = None
    job.queued_at = now
    job.retry_count += 1
    job.next_retry_at = now
    job.last_error_at = None
    job.last_error_type = None
    job.lock_owner = None
    job.lock_until = None
    job.last_heartbeat_at = now
    job.updated_at = now
    db.commit()
    db.refresh(job)
    return job


def update_job_progress(db: Session, job: BackgroundJob, progress: int) -> BackgroundJob:
    job.progress = max(0, min(int(progress), 100))
    job.last_heartbeat_at = _now()
    job.updated_at = _now()
    db.commit()
    db.refresh(job)
    return job


def job_payload(job: BackgroundJob) -> dict:
    payload = _loads(job.payload_json)
    cleaned, _trace = split_trace_payload(payload)
    return cleaned


def job_trace(job: BackgroundJob) -> dict:
    payload = _loads(job.payload_json)
    _cleaned, trace = split_trace_payload(payload)
    return trace
