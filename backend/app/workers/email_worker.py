from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
import app.master.database as master_database
from app.db.models import Mailbox
from app.master.models import EmailSyncState, MailboxSyncState, MasterTenantDatabase
from app.tenancy.database import tenant_db_session
from app.workers.email_listener import reconcile_mailbox_email, reconcile_tenant_email

logger = logging.getLogger(__name__)
_worker_started = False


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _state_for_company(master_db: Session, company_id: int) -> EmailSyncState:
    state = master_db.scalar(
        select(EmailSyncState).where(
            EmailSyncState.company_id == company_id,
            EmailSyncState.channel_key == "email",
        )
    )
    if state:
        return state
    state = EmailSyncState(
        company_id=company_id,
        channel_key="email",
        enabled=True,
        frequency_seconds=60,
        status="idle",
        next_run_at=datetime.now(timezone.utc),
    )
    master_db.add(state)
    master_db.commit()
    return state


def _acquire_lock(master_db: Session, state: EmailSyncState, owner: str) -> bool:
    now = datetime.now(timezone.utc)
    lock_until = _as_utc(state.lock_until)
    if lock_until and lock_until > now:
        return False
    state.lock_owner = owner
    state.lock_until = now + timedelta(minutes=2)
    state.status = "running"
    state.last_sync_at = now
    master_db.commit()
    return True


def _release_lock(master_db: Session, state: EmailSyncState, *, success: bool, error: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    state.lock_owner = None
    state.lock_until = None
    state.next_run_at = now + timedelta(seconds=max(state.frequency_seconds or 60, 30))
    state.updated_at = now
    if success:
        state.status = "idle"
        state.last_success_at = now
        state.last_error_at = None
        state.last_error_message = None
    else:
        state.status = "error"
        state.last_error_at = now
        state.last_error_message = error
    master_db.commit()


def _run_due_tenant(master_db: Session, tenant: MasterTenantDatabase, state: EmailSyncState) -> None:
    try:
        reconcile_tenant_email(master_db, tenant, owner="email-worker")
        _release_lock(master_db, state, success=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error sincronizando tenant %s: %s", tenant.company_id, exc)
        _release_lock(master_db, state, success=False, error=str(exc))


def _run_due_mailbox(master_db: Session, tenant: MasterTenantDatabase, state: MailboxSyncState) -> None:
    session_factory = tenant_db_session(tenant.database_url)
    db = session_factory()
    try:
        mailbox = db.get(Mailbox, state.mailbox_id)
        if not mailbox or mailbox.company_id != tenant.company_id:
            state.enabled = False
            _release_lock(master_db, state, success=True)
            return
    finally:
        db.close()
    try:
        result = reconcile_mailbox_email(master_db, tenant, mailbox, owner="email-worker", state=state)
        _release_lock(master_db, state, success=bool(result.get("ok")), error=None if result.get("ok") else str(result.get("message") or "error"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error sincronizando buzón %s/%s: %s", tenant.company_id, state.mailbox_id, exc)
        _release_lock(master_db, state, success=False, error=str(exc))


def _worker_loop() -> None:
    settings = get_settings()
    poll_seconds = max(int(getattr(settings, "email_worker_poll_seconds", 15)), 5)
    while True:
        master_db = master_database.MasterSessionLocal()
        try:
            now = datetime.now(timezone.utc)
            due_states = master_db.scalars(
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
            for state in due_states:
                tenant = master_db.scalar(
                    select(MasterTenantDatabase).where(
                        MasterTenantDatabase.company_id == state.company_id,
                        MasterTenantDatabase.is_active.is_(True),
                    )
                )
                if not tenant or not tenant.database_url:
                    continue
                if not _acquire_lock(master_db, state, owner="email-worker"):
                    continue
                _run_due_tenant(master_db, tenant, state)
            mailbox_states = master_db.scalars(
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
            for state in mailbox_states:
                tenant = master_db.scalar(
                    select(MasterTenantDatabase).where(
                        MasterTenantDatabase.company_id == state.company_id,
                        MasterTenantDatabase.is_active.is_(True),
                    )
                )
                if not tenant or not tenant.database_url:
                    continue
                if not _acquire_lock(master_db, state, owner="email-worker"):
                    continue
                _run_due_mailbox(master_db, tenant, state)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Email worker error: %s", exc)
        finally:
            master_db.close()
        time.sleep(poll_seconds)


def start_email_sync_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_worker_loop, name="anchi-email-sync", daemon=True).start()


def is_email_sync_worker_started() -> bool:
    return _worker_started
