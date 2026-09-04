from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.encryption import mask_secret
from app.db.models import Mailbox
from app.master.models import MailboxSyncState


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_mailbox(db: Session, company_id: int, mailbox_id: int) -> Mailbox | None:
    mailbox = db.get(Mailbox, mailbox_id)
    return mailbox if mailbox and mailbox.company_id == company_id else None


def list_mailboxes(db: Session, company_id: int) -> list[Mailbox]:
    return db.scalars(
        select(Mailbox)
        .where(Mailbox.company_id == company_id)
        .order_by(Mailbox.name, Mailbox.email_address, Mailbox.id)
    ).all()


def get_or_create_mailbox_sync_state(
    master_db: Session,
    mailbox: Mailbox,
    *,
    commit: bool = True,
) -> MailboxSyncState:
    state = master_db.scalar(
        select(MailboxSyncState).where(
            MailboxSyncState.company_id == mailbox.company_id,
            MailboxSyncState.mailbox_id == mailbox.id,
        )
    )
    if not state:
        state = MailboxSyncState(
            company_id=mailbox.company_id,
            mailbox_id=mailbox.id,
            enabled=bool(mailbox.enabled and mailbox.auto_sync_enabled),
            frequency_seconds=60,
            status="idle",
            sync_status="idle",
            next_run_at=now_utc(),
        )
        master_db.add(state)
    sync_state_from_mailbox(state, mailbox)
    if commit:
        master_db.commit()
        master_db.refresh(state)
    return state


def sync_state_from_mailbox(state: MailboxSyncState, mailbox: Mailbox) -> None:
    state.enabled = bool(mailbox.enabled and mailbox.auto_sync_enabled)
    try:
        state.frequency_seconds = max(int(getattr(mailbox, "polling_frequency_minutes", 1) or 1), 1) * 60
    except (TypeError, ValueError):
        state.frequency_seconds = 60
    state.source_provider = (mailbox.provider or "imap").strip().lower() or "imap"
    state.source_host = (mailbox.imap_host or "").strip() or None
    state.source_username = (mailbox.imap_username or "").strip() or None
    state.source_connected_email = (
        (mailbox.connected_email or mailbox.email_address or mailbox.imap_username or "").strip() or None
    )
    state.updated_at = now_utc()


def serialize_mailbox(mailbox: Mailbox) -> dict:
    return {
        "id": mailbox.id,
        "company_id": mailbox.company_id,
        "name": mailbox.name,
        "email_address": mailbox.email_address,
        "provider": mailbox.provider,
        "connection_method": mailbox.connection_method,
        "connected_email": mailbox.connected_email,
        "imap_host": mailbox.imap_host,
        "imap_port": mailbox.imap_port,
        "imap_security": mailbox.imap_security,
        "imap_username": mailbox.imap_username,
        "imap_password": mask_secret(mailbox.imap_password_encrypted),
        "inbox_folder": mailbox.inbox_folder,
        "read_limit": mailbox.read_limit,
        "auto_sync_enabled": mailbox.auto_sync_enabled,
        "read_unread_only": mailbox.read_unread_only,
        "smtp_enabled": mailbox.smtp_enabled,
        "smtp_host": mailbox.smtp_host,
        "smtp_port": mailbox.smtp_port,
        "smtp_security": mailbox.smtp_security,
        "smtp_username": mailbox.smtp_username,
        "smtp_password": mask_secret(mailbox.smtp_password_encrypted),
        "from_email": mailbox.from_email or mailbox.email_address,
        "enabled": mailbox.enabled,
        "last_imap_test_at": mailbox.last_imap_test_at.isoformat() if mailbox.last_imap_test_at else None,
        "last_imap_test_ok": mailbox.last_imap_test_ok,
        "last_imap_test_message": mailbox.last_imap_test_message,
        "last_smtp_test_at": mailbox.last_smtp_test_at.isoformat() if mailbox.last_smtp_test_at else None,
        "last_smtp_test_ok": mailbox.last_smtp_test_ok,
        "last_smtp_test_message": mailbox.last_smtp_test_message,
    }
