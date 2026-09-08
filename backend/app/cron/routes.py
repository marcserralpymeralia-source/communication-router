from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.service import is_channel_enabled
from app.core.config import get_settings
from app.db.models import EmailSettings, Mailbox
from app.master.database import get_master_db
from app.master.models import EmailSyncState, MailboxSyncState, MasterTenantDatabase
from app.mailboxes.service import sync_state_from_mailbox
from app.settings.integrations import read_latest_imap_emails
from app.settings.service import get_or_create_settings
from app.tenancy.database import tenant_db_session
from app.workers.email_worker import _acquire_lock, _release_lock  # noqa: PLC2701
from app.workers.jobs_worker import run_worker_cycle

router = APIRouter(prefix="/cron", tags=["cron"])


def _cron_authorized(request: Request) -> None:
    settings = get_settings()
    expected = (settings.cron_secret or "").strip()
    if (request.headers.get("x-vercel-cron") or "").strip().lower() in {"1", "true", "yes"}:
        return
    provided = (
        request.headers.get("x-cron-secret")
        or request.query_params.get("secret")
        or request.headers.get("authorization")
        or ""
    ).strip()
    if expected and provided == expected:
        return
    if not expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cron secret no configurado.")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cron no autorizado.")


@router.api_route("/jobs", methods=["GET", "POST"])
def jobs_cron(request: Request):
    _cron_authorized(request)
    result = run_worker_cycle(max_jobs=1)
    return JSONResponse({"ok": True, **result})


@router.api_route("/email-sync", methods=["GET", "POST"])
def email_sync_cron(request: Request, master_db: Session = Depends(get_master_db)):
    _cron_authorized(request)
    now = datetime.now(timezone.utc)
    kibak_runtime = get_settings().app_slug.strip().lower() == "kibak" and master_db.get_bind().dialect.name == "postgresql"
    due_states = [] if kibak_runtime else master_db.scalars(
        select(EmailSyncState)
        .join(MasterTenantDatabase, MasterTenantDatabase.company_id == EmailSyncState.company_id)
        .where(
            MasterTenantDatabase.is_active.is_(True),
            MasterTenantDatabase.database_url.is_not(None),
            EmailSyncState.enabled.is_(True),
            EmailSyncState.channel_key == "email",
            EmailSyncState.next_run_at.is_not(None),
            EmailSyncState.next_run_at <= now,
        )
    ).all()
    result = {
        "ok": True,
        "checked": len(due_states),
        "processed": 0,
        "skipped": 0,
        "found": 0,
        "saved": 0,
        "duplicates": 0,
        "discarded": 0,
        "errors": 0,
        "tenants": [],
    }
    for state in due_states:
        tenant = master_db.scalar(
            select(MasterTenantDatabase).where(
                MasterTenantDatabase.company_id == state.company_id,
                MasterTenantDatabase.is_active.is_(True),
            )
        )
        database_url = tenant.get_database_url() if tenant else None
        if not database_url:
            result["skipped"] += 1
            continue
        if not _acquire_lock(master_db, state, owner="cron-email-sync"):
            result["skipped"] += 1
            continue
        session_factory = tenant_db_session(database_url)
        db = session_factory()
        try:
            settings = get_or_create_settings(db, EmailSettings, tenant.company_id)

            if not is_channel_enabled(db, tenant.company_id, "email"):
                state.enabled = False
                master_db.commit()
                _release_lock(master_db, state, success=True)
                result["skipped"] += 1
                continue

            state.frequency_seconds = max(int(settings.polling_frequency_minutes or 1), 1) * 60
            master_db.commit()
            if not settings.auto_sync_enabled:
                state.enabled = False
                master_db.commit()
                _release_lock(master_db, state, success=True)
                result["skipped"] += 1
                continue
            sync_result = read_latest_imap_emails(
                db,
                settings,
                tenant.company_id,
                auto_process=settings.auto_process_on_fetch,
                unread_only=settings.read_unread_only,
                limit=max(min(int(settings.read_limit or 10), 50), 1),
                sync_state=state,
                sync_session=master_db,
            )
            tenant_result = {
                "company_id": tenant.company_id,
                "ok": bool(sync_result.get("ok")),
                "found": int(sync_result.get("found") or 0),
                "saved": int(sync_result.get("saved") or 0),
                "duplicates": int(sync_result.get("duplicates") or 0),
                "discarded": int(sync_result.get("discarded") or 0),
                "errors": int(sync_result.get("errors") or 0),
                "message": sync_result.get("message"),
            }
            result["processed"] += 1
            result["found"] += tenant_result["found"]
            result["saved"] += tenant_result["saved"]
            result["duplicates"] += tenant_result["duplicates"]
            result["discarded"] += tenant_result["discarded"]
            result["errors"] += tenant_result["errors"]
            result["tenants"].append(tenant_result)
            _release_lock(master_db, state, success=bool(sync_result.get("ok")), error=None if sync_result.get("ok") else str(sync_result.get("message") or "error"))
        except Exception as exc:  # noqa: BLE001
            _release_lock(master_db, state, success=False, error=str(exc))
            result["errors"] += 1
            result["tenants"].append({"company_id": tenant.company_id, "ok": False, "message": str(exc)})
        finally:
            db.close()
    due_mailbox_states = master_db.scalars(
        select(MailboxSyncState)
        .join(MasterTenantDatabase, MasterTenantDatabase.company_id == MailboxSyncState.company_id)
        .where(
            MasterTenantDatabase.is_active.is_(True),
            MasterTenantDatabase.database_url.is_not(None),
            MailboxSyncState.enabled.is_(True),
            MailboxSyncState.next_run_at.is_not(None),
            MailboxSyncState.next_run_at <= now,
        )
    ).all()
    result["checked"] += len(due_mailbox_states)
    for state in due_mailbox_states:
        tenant = master_db.scalar(
            select(MasterTenantDatabase).where(
                MasterTenantDatabase.company_id == state.company_id,
                MasterTenantDatabase.is_active.is_(True),
            )
        )
        database_url = tenant.get_database_url() if tenant else None
        if not database_url or not _acquire_lock(master_db, state, owner="cron-mailbox-sync"):
            result["skipped"] += 1
            continue
        session_factory = tenant_db_session(database_url)
        db = session_factory()
        try:
            mailbox = db.get(Mailbox, state.mailbox_id)
            if not mailbox or mailbox.company_id != tenant.company_id or not mailbox.enabled:
                state.enabled = False
                _release_lock(master_db, state, success=True)
                result["skipped"] += 1
                continue
            sync_state_from_mailbox(state, mailbox)
            master_db.commit()
            if not is_channel_enabled(db, tenant.company_id, "email") or not mailbox.auto_sync_enabled:
                state.enabled = False
                _release_lock(master_db, state, success=True)
                result["skipped"] += 1
                continue
            sync_result = read_latest_imap_emails(
                db,
                mailbox,
                tenant.company_id,
                auto_process=mailbox.auto_process_on_fetch,
                unread_only=mailbox.read_unread_only,
                limit=max(min(int(mailbox.read_limit or 10), 50), 1),
                sync_state=state,
                sync_session=master_db,
                mailbox_id=mailbox.id,
            )
            tenant_result = {
                "company_id": tenant.company_id,
                "mailbox_id": mailbox.id,
                "ok": bool(sync_result.get("ok")),
                "found": int(sync_result.get("found") or 0),
                "saved": int(sync_result.get("saved") or 0),
                "duplicates": int(sync_result.get("duplicates") or 0),
                "discarded": int(sync_result.get("discarded") or 0),
                "errors": int(sync_result.get("errors") or 0),
                "message": sync_result.get("message"),
            }
            result["processed"] += 1
            for key in ("found", "saved", "duplicates", "discarded", "errors"):
                result[key] += tenant_result[key]
            result["tenants"].append(tenant_result)
            _release_lock(master_db, state, success=bool(sync_result.get("ok")), error=None if sync_result.get("ok") else str(sync_result.get("message") or "error"))
        except Exception as exc:  # noqa: BLE001
            _release_lock(master_db, state, success=False, error=str(exc))
            result["errors"] += 1
            result["tenants"].append({"company_id": tenant.company_id, "mailbox_id": state.mailbox_id, "ok": False, "message": str(exc)})
        finally:
            db.close()
    master_db.commit()
    return JSONResponse(result)
