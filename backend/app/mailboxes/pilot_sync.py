"""Bounded, read-only mailbox import used by the KIBAK pilot."""

from __future__ import annotations

from datetime import datetime, timezone
from email import policy
from email import message_from_bytes
from email.utils import parseaddr, parsedate_to_datetime
from threading import Lock

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.communications.service import create_or_update_communication_from_email
from app.core.encryption import decrypt_secret
from app.db.models import Communication, Mailbox
from app.mailboxes.google_oauth import mailbox_oauth_provider
from app.settings.integrations import (
    SYNC_LOCKS,
    _decode_mime_header,
    _extract_body_parts,
    _header_addresses,
    _imap_authenticate,
    _imap_client,
    _imap_uid,
    _imap_uid_search,
    _imap_uidvalidity,
    _is_attachment,
    validate_imap_config,
)

PILOT_SYNC_MIN_DATE = datetime(2026, 9, 14, tzinfo=timezone.utc)
PILOT_SYNC_MAX_IMPORTS = 3
PILOT_SYNC_SCAN_CAP = 10


def _result(**values: object) -> dict[str, object]:
    return {
        "ok": False,
        "imported": 0,
        "duplicates": 0,
        "skipped_attachment": 0,
        "candidates_reviewed": 0,
        "errors": 0,
        "scan_cap": PILOT_SYNC_SCAN_CAP,
        "max_imports": PILOT_SYNC_MAX_IMPORTS,
        "min_date": PILOT_SYNC_MIN_DATE.date().isoformat(),
        **values,
    }


def _received_at(message) -> datetime | None:  # noqa: ANN001
    value = message.get("Date")
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _has_attachment(message) -> bool:  # noqa: ANN001
    return any(_is_attachment(part) for part in message.walk())


def run_pilot_sync(db: Session, mailbox: Mailbox, company_id: int) -> dict[str, object]:
    """Import recent unread, attachment-free Microsoft messages only.

    This intentionally does not use the normal sync state or job pipeline. The
    caller's tenant session is the only persistence context involved.
    """
    if mailbox.company_id != company_id:
        return _result(message="El buzón no pertenece al tenant actual.")
    if mailbox.provider != "microsoft365" or mailbox.connection_method != "oauth2":
        return _result(message="El piloto requiere un buzón Microsoft 365 conectado por OAuth.")
    if mailbox.enabled:
        return _result(message="El buzón piloto debe permanecer desactivado.")
    if mailbox.auto_sync_enabled:
        return _result(message="La sincronización automática debe permanecer desactivada.")
    if mailbox.mark_as_read_after_import:
        return _result(message="El piloto requiere mantener desactivado el marcado como leído.")
    if mailbox_oauth_provider(mailbox) != "microsoft365" or not decrypt_secret(mailbox.refresh_token_encrypted):
        return _result(message="El buzón Microsoft todavía no está conectado por OAuth.")

    validation = validate_imap_config(mailbox)
    if not validation["ok"]:
        return _result(message=validation["message"])

    lock_key = (company_id, mailbox.id)
    sync_lock = SYNC_LOCKS.setdefault(lock_key, Lock())
    if not sync_lock.acquire(blocking=False):
        return _result(message="Ya hay una operación IMAP en curso para este buzón.")

    client = None
    imported = duplicates = skipped_attachment = candidates_reviewed = errors = 0
    folder = (mailbox.inbox_folder or mailbox.mailbox or "INBOX").strip() or "INBOX"
    try:
        client = _imap_client(mailbox)
        _imap_authenticate(client, mailbox)
        status, _data = client.select(folder, readonly=True)
        if status != "OK":
            return _result(message="No se pudo abrir la carpeta IMAP del piloto.")
        uidvalidity = _imap_uidvalidity(client, folder)
        ids = _imap_uid_search(client, "SINCE", "14-Sep-2026", "UNSEEN")
        if ids is None:
            return _result(message="No se pudieron listar los correos del piloto.")

        # IMAP SEARCH returns ascending UIDs. Inspect the newest bounded window
        # first so a recent attachment cannot starve later eligible messages.
        candidate_ids = list(reversed(ids[-PILOT_SYNC_SCAN_CAP:]))
        for msg_id in candidate_ids:
            if imported >= PILOT_SYNC_MAX_IMPORTS:
                break
            candidates_reviewed += 1
            try:
                fetch_status, msg_data = client.uid("fetch", msg_id, "(UID RFC822)")
                if fetch_status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    errors += 1
                    continue
                fetch_meta, raw = msg_data[0]
                if not isinstance(raw, bytes):
                    errors += 1
                    continue
                message = message_from_bytes(raw, policy=policy.default)
                received_at = _received_at(message)
                if received_at is None or received_at < PILOT_SYNC_MIN_DATE:
                    continue
                uid = _imap_uid(
                    fetch_meta.decode(errors="ignore") if isinstance(fetch_meta, bytes) else str(fetch_meta or ""),
                    msg_id.decode(errors="ignore") if isinstance(msg_id, bytes) else str(msg_id),
                )
                message_id = message.get("Message-ID") or None
                external_id = f"mailbox:{mailbox.id}:{folder}:{uidvalidity or 'unknown'}:{uid}"
                existing = db.scalar(
                    select(Communication).where(
                        Communication.company_id == company_id,
                        Communication.mailbox_id == mailbox.id,
                        Communication.provider == "microsoft365",
                        Communication.external_message_id.in_([message_id, external_id] if message_id else [external_id]),
                    )
                )
                if existing:
                    duplicates += 1
                    continue
                if _has_attachment(message):
                    skipped_attachment += 1
                    continue

                sender = _decode_mime_header(message.get("From", ""))
                sender_name, sender_email = parseaddr(sender)
                body_text, body_html = _extract_body_parts(message)
                with db.begin_nested():
                    create_or_update_communication_from_email(
                        db,
                        company_id=company_id,
                        mailbox_id=mailbox.id,
                        provider="microsoft365",
                        external_message_id=message_id or external_id,
                        thread_id=message.get("In-Reply-To") or message.get("References"),
                        sender_email=sender_email or None,
                        sender_name=_decode_mime_header(sender_name) or None,
                        to_recipients=_header_addresses(message, "To"),
                        cc_recipients=_header_addresses(message, "Cc"),
                        bcc_recipients=_header_addresses(message, "Bcc"),
                        subject=_decode_mime_header(message.get("Subject", "")),
                        body_text=body_text,
                        body_html=body_html,
                        received_at=received_at,
                        processing_status="received",
                        routing_status="unclassified",
                        metadata={
                            "message_id": message_id,
                            "imap_mailbox": folder,
                            "imap_uidvalidity": uidvalidity,
                            "imap_uid": uid,
                            "import_mode": "pilot",
                        },
                    )
                imported += 1
            except Exception:  # noqa: BLE001
                errors += 1

        db.commit()
        return _result(
            ok=True,
            imported=imported,
            duplicates=duplicates,
            skipped_attachment=skipped_attachment,
            candidates_reviewed=candidates_reviewed,
            errors=errors,
            message="Piloto completado.",
        )
    except Exception:  # noqa: BLE001
        db.rollback()
        return _result(
            imported=imported,
            duplicates=duplicates,
            skipped_attachment=skipped_attachment,
            candidates_reviewed=candidates_reviewed,
            errors=errors + 1,
            message="No se pudo completar el piloto de importación.",
        )
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass
        sync_lock.release()
