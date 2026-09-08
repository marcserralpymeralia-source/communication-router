from __future__ import annotations

import re
import smtplib
import socket
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.attachment_storage import read_attachment
from app.core.encryption import decrypt_secret
from app.communications.service import recipient_values
from app.db.models import BackgroundJob, Communication, Department, Mailbox, RoutingAction, RoutingDecision, User


FORWARD_ACTION_TYPE = "forward"
AUTO_FORWARD_JOB_TYPE = "forward_communication"
AUTO_FORWARD_DEDUPE_PREFIX = "routing-action:"
FORWARD_STATUSES = {"pending", "processing", "sent", "failed", "cancelled"}
TRANSIENT_FORWARD_ERRORS = {"timeout", "connection_failed", "smtp_4xx"}
AMBIGUOUS_FORWARD_ERRORS = {"server_disconnected"}
FORWARD_RETRYABLE_ERRORS = TRANSIENT_FORWARD_ERRORS | AMBIGUOUS_FORWARD_ERRORS
MAX_FORWARD_ATTACHMENT_SIZE = 10 * 1024 * 1024
MAX_FORWARD_TOTAL_SIZE = 25 * 1024 * 1024
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ForwardingValidationError(ValueError):
    """Raised before delivery when tenant-owned records are invalid."""


class ForwardingDeliveryError(RuntimeError):
    def __init__(self, message: str, *, error_code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


SmtpClientFactory = Callable[[Mailbox], Any]


def _valid_email(value: str | None) -> bool:
    if not value:
        return False
    address = parseaddr(value)[1].strip()
    return address == value.strip() and bool(_EMAIL_RE.fullmatch(address))


def _safe_error_message(error_code: str) -> str:
    return {
        "invalid_configuration": "La configuración SMTP del buzón no está disponible.",
        "unsupported_provider": "El proveedor de envío del buzón no está soportado para forwarding.",
        "invalid_destination": "El departamento no tiene un destination_email válido.",
        "attachment_unavailable": "No se pudo leer un adjunto de la comunicación.",
        "attachment_too_large": "Un adjunto supera el tamaño máximo permitido.",
        "message_too_large": "El mensaje supera el tamaño máximo permitido.",
        "invalid_header": "La comunicación contiene un header no válido.",
        "authentication_failed": "El servidor SMTP rechazó la autenticación.",
        "destination_rejected": "El servidor SMTP rechazó el destinatario.",
        "smtp_4xx": "El servidor SMTP devolvió un error transitorio.",
        "timeout": "Timeout durante el envío SMTP.",
        "connection_failed": "No se pudo conectar con el servidor SMTP.",
        "server_disconnected": "El servidor SMTP cerró la conexión.",
        "smtp_error": "El envío SMTP falló.",
    }.get(error_code, "El envío SMTP falló.")


def _smtp_failure(exc: Exception) -> ForwardingDeliveryError:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ForwardingDeliveryError(_safe_error_message("timeout"), error_code="timeout", retryable=True)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ForwardingDeliveryError(_safe_error_message("authentication_failed"), error_code="authentication_failed")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [
            int(code)
            for _address, (code, _message) in exc.recipients.items()
            if str(code).isdigit()
        ]
        error_code = "smtp_4xx" if codes and all(400 <= item < 500 for item in codes) else "destination_rejected"
        return ForwardingDeliveryError(
            _safe_error_message(error_code),
            error_code=error_code,
            retryable=error_code in TRANSIENT_FORWARD_ERRORS,
        )
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return ForwardingDeliveryError(
            _safe_error_message("server_disconnected"),
            error_code="server_disconnected",
            retryable=True,
        )
    if isinstance(exc, smtplib.SMTPResponseException):
        error_code = "smtp_4xx" if 400 <= int(exc.smtp_code or 0) < 500 else "smtp_error"
        return ForwardingDeliveryError(
            _safe_error_message(error_code),
            error_code=error_code,
            retryable=error_code in TRANSIENT_FORWARD_ERRORS,
        )
    if isinstance(exc, (smtplib.SMTPException, OSError)):
        return ForwardingDeliveryError(
            _safe_error_message("connection_failed"),
            error_code="connection_failed",
            retryable=True,
        )
    return ForwardingDeliveryError(_safe_error_message("smtp_error"), error_code="smtp_error")


def _action_key(company_id: int, communication_id: int, decision_id: int, department_id: int) -> str:
    return f"forward:{company_id}:{communication_id}:{decision_id}:{department_id}"


def _tenant_records(
    db: Session,
    company_id: int,
    communication_id: int,
    routing_decision_id: int,
    department_id: int,
) -> tuple[Communication, RoutingDecision, Department, Mailbox]:
    communication = db.scalar(
        select(Communication).where(
            Communication.id == communication_id,
            Communication.company_id == company_id,
        )
    )
    decision = db.scalar(
        select(RoutingDecision).where(
            RoutingDecision.id == routing_decision_id,
            RoutingDecision.company_id == company_id,
            RoutingDecision.communication_id == communication_id,
        )
    )
    department = db.scalar(
        select(Department).where(
            Department.id == department_id,
            Department.company_id == company_id,
            Department.active.is_(True),
        )
    )
    if communication is None or decision is None or department is None:
        raise ForwardingValidationError("La acción de forwarding no pertenece al tenant indicado.")
    if decision.final_department_id != department_id or decision.status not in {"confirmed", "corrected", "routed"}:
        raise ForwardingValidationError("La decisión no tiene este departamento como destino final.")
    mailbox = db.scalar(
        select(Mailbox).where(
            Mailbox.id == communication.mailbox_id,
            Mailbox.company_id == company_id,
        )
    )
    if mailbox is None:
        raise ForwardingValidationError("El buzón de la comunicación no pertenece al tenant indicado.")
    return communication, decision, department, mailbox


def _validate_triggered_user(db: Session, company_id: int, triggered_by_user_id: int | None) -> None:
    if triggered_by_user_id is not None and db.scalar(
        select(User).where(User.id == triggered_by_user_id, User.company_id == company_id)
    ) is None:
        raise ForwardingValidationError("El usuario que dispara la acción no pertenece al tenant indicado.")


def current_forward_action(db: Session, company_id: int, communication_id: int) -> RoutingAction | None:
    return db.scalar(
        select(RoutingAction)
        .where(
            RoutingAction.company_id == company_id,
            RoutingAction.communication_id == communication_id,
        )
        .order_by(RoutingAction.id.desc())
    )


def serialize_forward_action(action: RoutingAction | None) -> dict[str, Any] | None:
    if action is None:
        return None
    return {
        "id": action.id,
        "company_id": action.company_id,
        "communication_id": action.communication_id,
        "routing_decision_id": action.routing_decision_id,
        "department_id": action.department_id,
        "source": action.source,
        "destination_email": action.destination_email,
        "status": action.status,
        "attempt_count": action.attempt_count,
        "provider_message_id": action.provider_message_id,
        "error_code": action.error_code,
        "error_message": action.error_message,
        "created_at": action.created_at.isoformat() if action.created_at else None,
        "started_at": action.started_at.isoformat() if action.started_at else None,
        "completed_at": action.completed_at.isoformat() if action.completed_at else None,
    }


def ensure_routing_action(
    db: Session,
    *,
    company_id: int,
    communication_id: int,
    routing_decision_id: int,
    department_id: int,
    triggered_by_user_id: int | None = None,
    source: str = "manual",
) -> RoutingAction:
    """Create the durable action record without contacting SMTP."""

    _tenant_records(db, company_id, communication_id, routing_decision_id, department_id)
    _validate_triggered_user(db, company_id, triggered_by_user_id)
    key = _action_key(company_id, communication_id, routing_decision_id, department_id)
    action = db.scalar(
        select(RoutingAction).where(
            RoutingAction.company_id == company_id,
            RoutingAction.idempotency_key == key,
        )
    )
    if action is not None:
        return action

    department = db.scalar(
        select(Department).where(
            Department.id == department_id,
            Department.company_id == company_id,
            Department.active.is_(True),
        )
    )
    action = RoutingAction(
        company_id=company_id,
        communication_id=communication_id,
        routing_decision_id=routing_decision_id,
        department_id=department_id,
        triggered_by_user_id=triggered_by_user_id,
        action_type=FORWARD_ACTION_TYPE,
        source=source,
        destination_email=(department.destination_email if department else "") or "",
        status="pending",
        idempotency_key=key,
    )
    db.add(action)
    db.flush()
    return action


def enqueue_forwarding_job(db: Session, action: RoutingAction) -> BackgroundJob | None:
    """Queue one action, creating a new job only after a terminal job failed."""

    if action.status in {"sent", "processing", "cancelled"}:
        return None
    from app.jobs.service import enqueue_job

    base_key = f"{AUTO_FORWARD_DEDUPE_PREFIX}{action.company_id}:{action.id}"
    existing = db.scalar(
        select(BackgroundJob).where(
            BackgroundJob.company_id == action.company_id,
            BackgroundJob.job_type == AUTO_FORWARD_JOB_TYPE,
            or_(
                BackgroundJob.dedupe_key == base_key,
                BackgroundJob.dedupe_key.like(f"{base_key}:retry:%"),
            ),
        )
    )
    if existing is not None and existing.status in {"queued", "running", "retrying"}:
        return existing
    dedupe_key = base_key if existing is None else f"{base_key}:retry:{action.attempt_count}"
    return enqueue_job(
        db,
        company_id=action.company_id,
        job_type=AUTO_FORWARD_JOB_TYPE,
        payload={"company_id": action.company_id, "routing_action_id": action.id},
        created_by_user_id=action.triggered_by_user_id,
        dedupe_key=dedupe_key,
    )


def _failed_action(db: Session, action: RoutingAction, error: ForwardingDeliveryError) -> RoutingAction:
    now = datetime.now(timezone.utc)
    action.status = "failed"
    action.error_code = error.error_code
    action.error_message = _safe_error_message(error.error_code)
    action.completed_at = now
    action.updated_at = now
    db.flush()
    return action


def _build_message(
    communication: Communication,
    department: Department,
    mailbox: Mailbox,
    action: RoutingAction,
) -> EmailMessage:
    from_email = (mailbox.from_email or mailbox.smtp_username or "").strip()
    destination = (department.destination_email or "").strip()
    if not _valid_email(from_email):
        raise ForwardingDeliveryError(
            _safe_error_message("invalid_configuration"),
            error_code="invalid_configuration",
        )
    if not _valid_email(destination):
        raise ForwardingDeliveryError(
            _safe_error_message("invalid_destination"),
            error_code="invalid_destination",
        )

    subject = communication.subject or "Comunicación"
    if any(character in subject for character in ("\r", "\n")):
        raise ForwardingDeliveryError(
            _safe_error_message("invalid_header"),
            error_code="invalid_header",
        )

    message = EmailMessage()
    message["From"] = from_email
    message["To"] = destination
    message["Subject"] = f"[Routing] {subject}"
    if _valid_email(communication.sender_email):
        message["Reply-To"] = communication.sender_email.strip()
    message["Message-ID"] = f"<forward-{sha256(action.idempotency_key.encode()).hexdigest()[:24]}@comm-router.local>"
    message["X-Comm-Router-Communication"] = str(communication.id)
    message["X-Comm-Router-Decision"] = str(action.routing_decision_id)
    message["X-Comm-Router-Action"] = str(action.id)

    original_recipients = ", ".join(
        value
        for value in (
            *recipient_values(communication.to_recipients),
            *recipient_values(communication.cc_recipients),
            *recipient_values(communication.bcc_recipients),
        )
        if value
    )
    received = format_datetime(communication.received_at) if communication.received_at else "Sin fecha"
    body = (
        "Comunicación reenviada para atención del departamento.\n\n"
        f"Remitente original: {communication.sender_email or 'Sin remitente'}\n"
        f"Fecha: {received}\n"
        f"Destinatarios originales: {original_recipients or 'Sin destinatarios'}\n"
        f"Asunto original: {communication.subject or 'Sin asunto'}\n\n"
        "Contenido original:\n"
        f"{communication.body_text or 'Sin contenido'}"
    )
    message.set_content(body)
    if communication.body_html:
        message.add_alternative(communication.body_html, subtype="html")

    total_size = 0
    for attachment in communication.attachments or []:
        if not attachment.storage_ref:
            raise ForwardingDeliveryError(
                _safe_error_message("attachment_unavailable"),
                error_code="attachment_unavailable",
            )
        try:
            payload = read_attachment(attachment.storage_ref)
        except Exception as exc:
            raise ForwardingDeliveryError(
                _safe_error_message("attachment_unavailable"),
                error_code="attachment_unavailable",
            ) from exc
        if len(payload) > MAX_FORWARD_ATTACHMENT_SIZE:
            raise ForwardingDeliveryError(
                _safe_error_message("attachment_too_large"),
                error_code="attachment_too_large",
            )
        total_size += len(payload)
        if total_size > MAX_FORWARD_TOTAL_SIZE:
            raise ForwardingDeliveryError(
                _safe_error_message("message_too_large"),
                error_code="message_too_large",
            )
        maintype, _, subtype = (attachment.mime_type or "application/octet-stream").partition("/")
        message.add_attachment(
            payload,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=Path(attachment.filename).name,
        )
    return message


def forward_communication(
    db: Session,
    *,
    company_id: int,
    communication_id: int,
    routing_decision_id: int,
    department_id: int,
    triggered_by_user_id: int | None = None,
    retry_failed: bool = False,
    smtp_client_factory: SmtpClientFactory | None = None,
) -> RoutingAction:
    """Forward one final routing decision through its source Mailbox SMTP configuration."""

    communication, decision, department, mailbox = _tenant_records(
        db, company_id, communication_id, routing_decision_id, department_id
    )
    action = ensure_routing_action(
        db,
        company_id=company_id,
        communication_id=communication_id,
        routing_decision_id=routing_decision_id,
        department_id=department_id,
        triggered_by_user_id=triggered_by_user_id,
        source="manual",
    )
    if action is not None:
        if action.status in {"sent", "processing", "cancelled"}:
            return action
        from app.routing.policy import load_routing_policy

        if load_routing_policy(db, company_id).simulation_mode:
            action.action_type = "simulated_forward"
            action.source = "simulation"
            action.status = "simulated"
            action.error_code = None
            action.error_message = f"Simulación: se habría reenviado a {department.destination_email}."
            action.completed_at = datetime.now(timezone.utc)
            action.updated_at = action.completed_at
            db.flush()
            return action
        if action.status == "failed" and (
            not retry_failed or action.error_code not in FORWARD_RETRYABLE_ERRORS
        ):
            return action

    action.status = "processing"
    action.attempt_count = int(action.attempt_count or 0) + 1
    action.started_at = datetime.now(timezone.utc)
    action.completed_at = None
    action.error_code = None
    action.error_message = None
    action.updated_at = datetime.now(timezone.utc)
    db.flush()

    try:
        if mailbox.smtp_provider not in {None, "", "smtp"}:
            raise ForwardingDeliveryError(
                _safe_error_message("unsupported_provider"),
                error_code="unsupported_provider",
            )
        if (
            not mailbox.smtp_enabled
            or not mailbox.smtp_host
            or not mailbox.smtp_username
            or not decrypt_secret(mailbox.smtp_password_encrypted)
        ):
            raise ForwardingDeliveryError(
                _safe_error_message("invalid_configuration"),
                error_code="invalid_configuration",
            )

        message = _build_message(communication, department, mailbox, action)
        if smtp_client_factory is None:
            from app.settings.integrations import _smtp_client

            smtp_client_factory = _smtp_client
        client = smtp_client_factory(mailbox)
        try:
            client.login(mailbox.smtp_username, decrypt_secret(mailbox.smtp_password_encrypted))
            result = client.send_message(
                message,
                from_addr=message["From"],
                to_addrs=[message["To"]],
            )
        finally:
            try:
                client.quit()
            except Exception:
                pass

        provider_message_id = result.get("provider_message_id") if isinstance(result, dict) else None
        provider_message_id = provider_message_id or (
            result.get("message_id") if isinstance(result, dict) else None
        )
        action.provider_message_id = str(provider_message_id or message["Message-ID"])
        now = datetime.now(timezone.utc)
        action.status = "sent"
        action.completed_at = now
        action.updated_at = now
        db.flush()
        return action
    except ForwardingDeliveryError as exc:
        return _failed_action(db, action, exc)
    except Exception as exc:
        return _failed_action(db, action, _smtp_failure(exc))


def process_forwarding_job(db: Session, job: BackgroundJob) -> dict[str, Any]:
    """Execute a queued action, leaving retry policy to the job worker."""

    from app.jobs.service import job_payload

    payload = job_payload(job)
    payload_company_id = payload.get("company_id")
    action_id = payload.get("routing_action_id")
    if isinstance(payload_company_id, bool) or isinstance(action_id, bool):
        raise ForwardingValidationError("El payload de forwarding no es válido.")
    try:
        payload_company_id = int(payload_company_id)
        action_id = int(action_id)
    except (TypeError, ValueError) as exc:
        raise ForwardingValidationError("El payload de forwarding no es válido.") from exc
    if payload_company_id != job.company_id or payload_company_id <= 0 or action_id <= 0:
        raise ForwardingValidationError("El job de forwarding no pertenece al tenant indicado.")

    action = db.scalar(
        select(RoutingAction).where(
            RoutingAction.id == action_id,
            RoutingAction.company_id == job.company_id,
        )
    )
    if action is None:
        raise ForwardingValidationError("La acción de forwarding no existe en el tenant indicado.")
    if action.status in {"sent", "cancelled", "processing", "simulated"}:
        return {"ok": True, "skipped": True, "action_id": action.id, "status": action.status}

    result = forward_communication(
        db,
        company_id=job.company_id,
        communication_id=action.communication_id,
        routing_decision_id=action.routing_decision_id,
        department_id=action.department_id,
        triggered_by_user_id=action.triggered_by_user_id,
        retry_failed=action.status == "failed",
    )
    if result.status == "sent":
        return {
            "ok": True,
            "action_id": result.id,
            "status": result.status,
            "provider_message_id": result.provider_message_id,
        }
    retryable = result.error_code in TRANSIENT_FORWARD_ERRORS
    return {
        "ok": False,
        "action_id": result.id,
        "status": result.status,
        "error_type": result.error_code or "forward_failed",
        "message": result.error_message or "El forwarding ha fallado.",
        "retryable": retryable,
    }
