from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from socket import gethostname
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

import app.master.database as master_database
from app.core.config import get_settings
from app.db.models import EmailSettings, Mailbox
from app.master.models import EmailSyncState, MailboxSyncState, MasterTenantDatabase
from app.channels.service import is_channel_enabled
from app.mailboxes.service import get_or_create_mailbox_sync_state
from app.settings.integrations import read_latest_imap_emails
from app.settings.service import get_or_create_settings
from app.tenancy.database import tenant_db_session

logger = logging.getLogger(__name__)
_STOP = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _identity() -> str:
    return f"{gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


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
        sync_status="idle",
        listener_status="inactive",
        next_run_at=_now(),
    )
    master_db.add(state)
    master_db.commit()
    return state


def mark_listener_heartbeat(master_db: Session, state: EmailSyncState, *, owner: str, status: str = "polling") -> None:
    now = _now()
    state.listener_owner = owner
    state.listener_status = status
    state.listener_last_started_at = state.listener_last_started_at or now
    state.listener_last_heartbeat_at = now
    state.listener_last_error_at = None
    state.listener_last_error_message = None
    state.updated_at = now
    master_db.commit()


def mark_listener_error(master_db: Session, state: EmailSyncState, *, owner: str, exc: Exception) -> None:
    now = _now()
    state.listener_owner = owner
    state.listener_status = "error"
    state.listener_last_heartbeat_at = now
    state.listener_last_error_at = now
    state.listener_last_error_message = str(exc)[:1000]
    state.last_error_at = now
    state.last_error_message = str(exc)[:1000]
    state.updated_at = now
    master_db.commit()


def mark_listener_inactive(master_db: Session, state: EmailSyncState, *, owner: str) -> None:
    state.listener_owner = owner
    state.listener_status = "inactive"
    state.updated_at = _now()
    master_db.commit()


def reconcile_mailbox_email(
    master_db: Session,
    tenant: MasterTenantDatabase,
    mailbox: Mailbox,
    *,
    owner: str,
    state: MailboxSyncState | None = None,
    force: bool = False,
) -> dict:
    state = state or get_or_create_mailbox_sync_state(master_db, mailbox)
    mark_listener_heartbeat(master_db, state, owner=owner, status="reconciling")
    session_factory = tenant_db_session(tenant.get_database_url())
    db = session_factory()
    try:
        if not mailbox.enabled:
            state.enabled = False
            mark_listener_inactive(master_db, state, owner=owner)
            return {"ok": True, "skipped": True, "message": "Buzón desactivado"}
        if not is_channel_enabled(db, tenant.company_id, "email"):
            state.enabled = False
            mark_listener_inactive(master_db, state, owner=owner)
            return {"ok": True, "skipped": True, "message": "Canal Email desactivado para este tenant"}
        if not mailbox.auto_sync_enabled and not force:
            state.enabled = False
            mark_listener_inactive(master_db, state, owner=owner)
            return {"ok": True, "skipped": True, "message": "Sincronización automática desactivada"}
        result = read_latest_imap_emails(
            db,
            mailbox,
            tenant.company_id,
            auto_process=mailbox.auto_process_on_fetch,
            unread_only=mailbox.read_unread_only,
            limit=mailbox.read_limit,
            sync_state=state,
            sync_session=master_db,
            mailbox_id=mailbox.id,
        )
        mark_listener_heartbeat(master_db, state, owner=owner, status="polling")
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "email.listener.mailbox_error",
            extra={
                "event": "email.listener.mailbox_error",
                "company_id": tenant.company_id,
                "mailbox_id": mailbox.id,
                "error_type": type(exc).__name__,
            },
        )
        mark_listener_error(master_db, state, owner=owner, exc=exc)
        return {"ok": False, "errors": 1, "message": str(exc)}
    finally:
        db.close()


def reconcile_tenant_email(master_db: Session, tenant: MasterTenantDatabase, *, owner: str, force: bool = False) -> dict:
    state = _state_for_company(master_db, tenant.company_id)
    mark_listener_heartbeat(master_db, state, owner=owner, status="reconciling")
    session_factory = tenant_db_session(tenant.get_database_url())
    db = session_factory()
    try:
        settings = get_or_create_settings(db, EmailSettings, tenant.company_id)

        if not is_channel_enabled(db, tenant.company_id, "email"):
            state.enabled = False
            mark_listener_inactive(master_db, state, owner=owner)
            return {
                "ok": True,
                "skipped": True,
                "message": "Canal Email desactivado para este tenant",
            }

        if not settings.auto_sync_enabled and not force:
            state.enabled = False
            mark_listener_inactive(master_db, state, owner=owner)
            return {"ok": True, "skipped": True, "message": "Sincronización automática desactivada"}
        result = read_latest_imap_emails(
            db,
            settings,
            tenant.company_id,
            auto_process=settings.auto_process_on_fetch,
            unread_only=settings.read_unread_only,
            limit=settings.read_limit,
            sync_state=state,
            sync_session=master_db,
        )
        mark_listener_heartbeat(master_db, state, owner=owner, status="polling")
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "email.listener.tenant_error",
            extra={"event": "email.listener.tenant_error", "company_id": tenant.company_id, "error_type": type(exc).__name__},
        )
        mark_listener_error(master_db, state, owner=owner, exc=exc)
        return {"ok": False, "errors": 1, "message": str(exc)}
    finally:
        db.close()


def run_email_listener_once(*, owner: str | None = None, force: bool = False) -> list[dict]:
    owner = owner or _identity()
    master_db = master_database.MasterSessionLocal()
    results: list[dict] = []
    kibak_runtime = get_settings().app_slug.strip().lower() == "kibak" and master_db.get_bind().dialect.name == "postgresql"
    try:
        tenants = master_db.scalars(
            select(MasterTenantDatabase).where(
                MasterTenantDatabase.is_active.is_(True),
                MasterTenantDatabase.database_url.is_not(None),
            )
        ).all()
        for tenant in tenants:
            if not kibak_runtime:
                state = _state_for_company(master_db, tenant.company_id)
                legacy_due = force or (
                    state.enabled and not (state.next_run_at and state.next_run_at > _now())
                )
                if legacy_due:
                    result = reconcile_tenant_email(master_db, tenant, owner=owner, force=force)
                    state.next_run_at = _now() + timedelta(seconds=max(state.frequency_seconds or 60, 30))
                    master_db.commit()
                    results.append({"company_id": tenant.company_id, **result})

            mailbox_states = master_db.scalars(
                select(MailboxSyncState).where(MailboxSyncState.company_id == tenant.company_id)
            ).all()
            for mailbox_state in mailbox_states:
                if not force and (not mailbox_state.enabled or (mailbox_state.next_run_at and mailbox_state.next_run_at > _now())):
                    continue
                session_factory = tenant_db_session(tenant.get_database_url())
                db = session_factory()
                try:
                    mailbox = db.get(Mailbox, mailbox_state.mailbox_id)
                    if not mailbox or mailbox.company_id != tenant.company_id:
                        mailbox_state.enabled = False
                        mailbox_state.next_run_at = _now() + timedelta(seconds=60)
                        master_db.commit()
                        continue
                finally:
                    db.close()
                result = reconcile_mailbox_email(
                    master_db,
                    tenant,
                    mailbox,
                    owner=owner,
                    state=mailbox_state,
                    force=force,
                )
                mailbox_state.next_run_at = _now() + timedelta(seconds=max(mailbox_state.frequency_seconds or 60, 30))
                master_db.commit()
                results.append({"company_id": tenant.company_id, "mailbox_id": mailbox.id, **result})
    finally:
        master_db.close()
    return results


def _handle_stop(_signum, _frame) -> None:  # noqa: ANN001
    global _STOP
    _STOP = True


def run_email_listener_forever() -> None:
    settings = get_settings()
    owner = _identity()
    poll_seconds = max(int(getattr(settings, "email_worker_poll_seconds", 15)), 5)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    logger.info("email.listener.started", extra={"event": "email.listener.started", "owner": owner, "poll_seconds": poll_seconds})
    while not _STOP:
        run_email_listener_once(owner=owner)
        time.sleep(poll_seconds)
    master_db = master_database.MasterSessionLocal()
    try:
        if not (get_settings().app_slug.strip().lower() == "kibak" and master_db.get_bind().dialect.name == "postgresql"):
            states = master_db.scalars(select(EmailSyncState).where(EmailSyncState.listener_owner == owner)).all()
            for state in states:
                mark_listener_inactive(master_db, state, owner=owner)
        mailbox_states = master_db.scalars(select(MailboxSyncState).where(MailboxSyncState.listener_owner == owner)).all()
        for state in mailbox_states:
            mark_listener_inactive(master_db, state, owner=owner)
    finally:
        master_db.close()
    logger.info("email.listener.stopped", extra={"event": "email.listener.stopped", "owner": owner})


if __name__ == "__main__":
    run_email_listener_forever()
