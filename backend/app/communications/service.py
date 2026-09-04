from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.attachment_extraction import extract_attachment_text
from app.core.attachment_storage import save_attachment
from app.db.models import Communication, CommunicationAttachment

PROCESSING_STATUSES = {"received", "parsing", "processed", "error"}
ROUTING_STATUSES = {"unclassified", "pending_review", "routed"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _recipient_list(value: Iterable[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = value.replace(";", ",").split(",")
    else:
        values = list(value)
    return [str(item).strip() for item in values if str(item).strip()]


def _recipient_json(value: Iterable[str] | str | None) -> str | None:
    values = _recipient_list(value)
    return json.dumps(values, ensure_ascii=False) if values else None


def recipient_values(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return _recipient_list(value)
    return _recipient_list(parsed if isinstance(parsed, list) else str(parsed))


def get_communication(db: Session, company_id: int, communication_id: int) -> Communication | None:
    return db.scalar(
        select(Communication)
        .where(Communication.company_id == company_id, Communication.id == communication_id)
        .options(selectinload(Communication.attachments))
    )


def list_communications(
    db: Session,
    company_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
    routing_status: str | None = None,
) -> list[Communication]:
    safe_limit = max(min(int(limit or 50), 100), 1)
    safe_offset = max(int(offset or 0), 0)
    filters = [Communication.company_id == company_id]
    if routing_status and routing_status in ROUTING_STATUSES:
        filters.append(Communication.routing_status == routing_status)
    return db.scalars(
        select(Communication)
        .where(*filters)
        .options(selectinload(Communication.attachments))
        .order_by(Communication.received_at.desc(), Communication.id.desc())
        .limit(safe_limit)
        .offset(safe_offset)
    ).all()


def _attachment_from_payload(
    db: Session,
    communication: Communication,
    *,
    filename: str,
    mime_type: str | None,
    payload: bytes | None = None,
    storage_ref: str | None = None,
    extracted_text: str | None = None,
    extraction_status: str | None = None,
    extraction_error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CommunicationAttachment:
    checksum = hashlib.sha256(payload).hexdigest() if payload is not None else None
    existing = None
    if checksum:
        existing = db.scalar(
            select(CommunicationAttachment).where(
                CommunicationAttachment.communication_id == communication.id,
                CommunicationAttachment.checksum == checksum,
                CommunicationAttachment.filename == filename,
            )
        )
    if existing:
        return existing

    if payload is not None and not storage_ref:
        storage_ref = save_attachment(filename=f"communication-{communication.id}-{filename}", payload=payload, content_type=mime_type)
    if payload is not None and extraction_status is None:
        extraction = extract_attachment_text(payload, filename=filename, content_type=mime_type)
        extracted_text = extraction.text
        extraction_status = extraction.status
        extraction_error = extraction.error

    attachment = CommunicationAttachment(
        company_id=communication.company_id,
        communication_id=communication.id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=len(payload) if payload is not None else 0,
        storage_ref=storage_ref,
        extracted_text=extracted_text,
        extraction_status=extraction_status or "pending",
        extraction_error=extraction_error,
        checksum=checksum,
        metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(attachment)
    db.flush()
    return attachment


def create_or_update_communication_from_email(
    db: Session,
    *,
    company_id: int,
    mailbox_id: int,
    provider: str,
    external_message_id: str,
    thread_id: str | None = None,
    sender_email: str | None = None,
    sender_name: str | None = None,
    to_recipients: Iterable[str] | str | None = None,
    cc_recipients: Iterable[str] | str | None = None,
    bcc_recipients: Iterable[str] | str | None = None,
    subject: str | None = None,
    body_text: str | None = None,
    body_html: str | None = None,
    received_at: datetime | None = None,
    processing_status: str = "parsing",
    routing_status: str = "unclassified",
    metadata: dict[str, Any] | None = None,
    attachments: Iterable[dict[str, Any]] | None = None,
) -> Communication:
    external_message_id = str(external_message_id or "").strip()
    if not external_message_id:
        raise ValueError("external_message_id es obligatorio para crear una comunicación.")
    if processing_status not in PROCESSING_STATUSES:
        raise ValueError(f"processing_status no soportado: {processing_status}")
    if routing_status not in ROUTING_STATUSES:
        raise ValueError(f"routing_status no soportado: {routing_status}")

    normalized_provider = (provider or "imap").strip().lower() or "imap"
    communication = db.scalar(
        select(Communication).where(
            Communication.company_id == company_id,
            Communication.mailbox_id == mailbox_id,
            Communication.provider == normalized_provider,
            Communication.external_message_id == external_message_id,
        )
    )
    now = _now()
    values = {
        "thread_id": thread_id,
        "sender_email": sender_email,
        "sender_name": sender_name,
        "to_recipients": _recipient_json(to_recipients),
        "cc_recipients": _recipient_json(cc_recipients),
        "bcc_recipients": _recipient_json(bcc_recipients),
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
        "metadata_json": json.dumps(metadata, ensure_ascii=False) if metadata else None,
        "received_at": received_at,
        "routing_status": routing_status,
        "updated_at": now,
    }
    if communication is None:
        communication = Communication(
            company_id=company_id,
            mailbox_id=mailbox_id,
            external_message_id=external_message_id,
            provider=normalized_provider,
            processing_status=processing_status,
            created_at=now,
            **values,
        )
        db.add(communication)
        db.flush()
    else:
        for key, value in values.items():
            if value is not None:
                setattr(communication, key, value)
        if communication.processing_status != "processed" or processing_status == "error":
            communication.processing_status = processing_status

    for attachment in attachments or ():
        _attachment_from_payload(db, communication, **attachment)
    return communication


def add_communication_attachment(
    db: Session,
    communication: Communication,
    *,
    filename: str,
    mime_type: str | None,
    payload: bytes,
    storage_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CommunicationAttachment:
    return _attachment_from_payload(
        db,
        communication,
        filename=filename,
        mime_type=mime_type,
        payload=payload,
        storage_ref=storage_ref,
        metadata=metadata,
    )


def mark_communication_processed(db: Session, communication: Communication) -> None:
    communication.processing_status = "processed"
    communication.updated_at = _now()


def serialize_communication(communication: Communication, *, include_detail: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": communication.id,
        "company_id": communication.company_id,
        "mailbox_id": communication.mailbox_id,
        "external_message_id": communication.external_message_id,
        "thread_id": communication.thread_id,
        "provider": communication.provider,
        "sender_email": communication.sender_email,
        "sender_name": communication.sender_name,
        "to_recipients": recipient_values(communication.to_recipients),
        "cc_recipients": recipient_values(communication.cc_recipients),
        "bcc_recipients": recipient_values(communication.bcc_recipients),
        "subject": communication.subject,
        "received_at": communication.received_at.isoformat() if communication.received_at else None,
        "processing_status": communication.processing_status,
        "routing_status": communication.routing_status,
    }
    if include_detail:
        payload.update(
            {
                "body_text": communication.body_text,
                "body_html": communication.body_html,
                "metadata": json.loads(communication.metadata_json) if communication.metadata_json else {},
                "attachments": [
                    {
                        "id": attachment.id,
                        "filename": attachment.filename,
                        "mime_type": attachment.mime_type,
                        "size_bytes": attachment.size_bytes,
                        "storage_ref": attachment.storage_ref,
                        "extracted_text": attachment.extracted_text,
                        "extraction_status": attachment.extraction_status,
                        "extraction_error": attachment.extraction_error,
                        "checksum": attachment.checksum,
                        "metadata": json.loads(attachment.metadata_json) if attachment.metadata_json else {},
                    }
                    for attachment in communication.attachments
                ],
            }
        )
    return payload
