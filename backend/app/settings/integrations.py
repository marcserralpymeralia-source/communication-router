import logging
import imaplib
import json
import re
import socket
import ssl
import smtplib
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email import policy
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from io import BytesIO
from pathlib import Path

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.agent.prompt_runtime import run_prompt_execution
from app.communications.service import add_communication_attachment, create_or_update_communication_from_email, mark_communication_processed
from app.core.encryption import decrypt_secret
from app.db.models import Communication, Email, EmailAttachment, EmailSettings, InboundMessage, LLMSettings, MessageAttachment
from app.master.models import EmailSyncState, MailboxSyncState
from app.messages.service import (
    NormalizedMessage,
    persist_normalized_message,
)
from app.jobs.service import enqueue_job
from app.logs.service import log_action
from app.core.attachment_storage import read_attachment, save_attachment


logger = logging.getLogger(__name__)
IMAP_RECENT_MESSAGES_LIMIT = 3
IMAP_DEFAULT_INITIAL_LIMIT = 20
IMAP_MAX_MESSAGES_PER_RUN = 50
IMAP_INITIAL_HISTORY_MAX = 100
IMAP_MAX_ATTACHMENTS_PER_EMAIL = 10
IMAP_MAX_ATTACHMENT_SIZE_MB = 10
IMAP_TIMEOUT_SECONDS = 20
SYNC_LOCKS: dict[tuple[int, int | None], threading.Lock] = {}
INITIAL_HISTORY_MODES = {"new", "7d", "30d", "100", "custom"}


def _imap_test_context(settings: EmailSettings, request_id: str | None = None) -> dict:
    return {
        "request_id": request_id,
        "provider": (settings.provider or "imap").strip().lower(),
        "host": (settings.imap_host or "").strip(),
        "port": settings.imap_port,
        "security": (settings.imap_security or "").strip().lower(),
        "mailbox": (settings.inbox_folder or settings.mailbox or "INBOX").strip() or "INBOX",
    }


def _imap_password_status(settings: EmailSettings) -> tuple[str | None, str | None]:
    raw_password = settings.imap_password_encrypted
    if raw_password and decrypt_secret(raw_password) is None:
        return None, "La contraseña guardada no se ha podido descifrar."
    password = (decrypt_secret(raw_password) or "").strip() if raw_password else ""
    if not password:
        return None, "La configuración IMAP está incompleta."
    return password, None


def _imap_connection_message(settings: EmailSettings, exc: Exception | None = None) -> tuple[str, str | None]:
    if exc is None:
        return "Conexion correcta.", None
    message = str(exc).strip()
    normalized = message.lower()
    provider = (settings.provider or "").strip().lower()
    host = (settings.imap_host or "").strip() or "imap.gmail.com"
    port = settings.imap_port or 993
    if isinstance(exc, ssl.SSLError) or any(marker in normalized for marker in ("ssl", "tls", "certificate")):
        return "La configuración SSL/TLS no es válida.", "ssl_error"
    if any(marker in normalized for marker in ("authentication failed", "invalid credentials", "login failed", "auth failed", "bad credentials", "[authentificationfailed]", "[authenticationfailed]")):
        if provider == "gmail" or "gmail" in host:
            return "Google ha rechazado la autenticación. Comprueba que utilizas una contraseña de aplicación.", "authentication_failed"
        return "Usuario o contraseña incorrectos.", "authentication_failed"
    if any(marker in normalized for marker in ("user unknown", "permission denied", "forbidden")):
        return "Usuario o contraseña incorrectos.", "authentication_failed"
    if any(marker in normalized for marker in ("no such mailbox", "mailbox not found", "unknown mailbox", "folder not found", "selected mailbox")):
        return f"La carpeta {settings.inbox_folder or settings.mailbox or 'INBOX'} no está disponible.", "mailbox_not_found"
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in normalized or "timeout" in normalized:
        return f"No se ha podido conectar con {host}:{port}.", "timeout"
    if isinstance(exc, ConnectionRefusedError) or "refused" in normalized or "unreachable" in normalized or "connection" in normalized or "network" in normalized or "socket" in normalized or isinstance(exc, socket.gaierror):
        return f"No se ha podido conectar con {host}:{port}.", "connection_failed"
    if isinstance(exc, imaplib.IMAP4.error):
        return "Usuario o contraseña incorrectos.", "authentication_failed"
    return f"No se ha podido conectar con {host}:{port}.", "unexpected_error"


def _log_imap_test_failure(settings: EmailSettings, exc: Exception | None, request_id: str | None = None) -> None:
    context = _imap_test_context(settings, request_id)
    message, error_type = _imap_connection_message(settings, exc)
    payload = {
        "event": "imap.test_connection_failed",
        "provider": context["provider"],
        "host": context["host"],
        "port": context["port"],
        "security": context["security"],
        "request_id": context["request_id"],
        "error_type": error_type,
    }
    if exc is None:
        logger.warning(message, extra=payload)
    else:
        if error_type == "unexpected_error":
            logger.exception(message, extra=payload)
        else:
            logger.warning(message, extra=payload)


def classify_integration_error(error: Exception | str) -> str:
    message = str(error).lower()
    if any(marker in message for marker in ("auth", "credential", "password", "invalid login", "permission denied")):
        return "authentication_failed" if "permission" not in message else "permission_denied"
    if any(marker in message for marker in ("uidvalidity", "uid validity")):
        return "uidvalidity_changed"
    if any(marker in message for marker in ("mailbox not found", "no such mailbox", "folder not found", "selected mailbox", "unknown mailbox")):
        return "mailbox_not_found"
    if any(marker in message for marker in ("parse", "could not parse", "bad message", "message parse", "header parse")):
        return "message_parse_failed"
    if any(marker in message for marker in ("attachment", "mime", "payload decode", "content transfer")):
        return "attachment_failed"
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if any(marker in message for marker in ("rate limit", "too many requests")):
        return "rate_limited"
    if any(marker in message for marker in ("faltan", "missing", "incomplet", "required", "invalid configuration")):
        return "invalid_configuration"
    if any(marker in message for marker in ("denied", "forbidden")):
        return "permission_denied"
    if any(marker in message for marker in ("network", "connection", "refused", "unreachable", "socket", "smtp", "imap")):
        return "connection_failed"
    return "unexpected_error"


def validate_imap_config(settings: EmailSettings) -> dict:
    password, error_message = _imap_password_status(settings)
    if error_message:
        return {"ok": False, "error_type": "invalid_configuration", "message": error_message}
    if not settings.imap_host or not settings.imap_username or not password:
        return {"ok": False, "error_type": "invalid_configuration", "message": "La configuración IMAP está incompleta."}
    if (settings.imap_security or "").strip().lower() not in {"ssl_tls", "starttls", "none"}:
        return {"ok": False, "error_type": "invalid_configuration", "message": "La configuración SSL/TLS no es válida."}
    return {"ok": True, "error_type": None, "message": "Configuracion IMAP valida."}


def validate_smtp_config(settings: EmailSettings) -> dict:
    password = decrypt_secret(settings.smtp_password_encrypted)
    if not settings.smtp_host or not settings.smtp_username or not password or not (settings.from_email or settings.smtp_username):
        return {"ok": False, "error_type": "invalid_configuration", "message": "Faltan host, usuario o password SMTP."}
    return {"ok": True, "error_type": None, "message": "Configuracion SMTP valida."}


def validate_openai_config(settings: LLMSettings) -> dict:
    if settings.provider not in {"openai", "openai_compatible", "azure_openai"}:
        return {"ok": False, "error_type": "invalid_configuration", "message": f"Proveedor IA no soportado: {settings.provider}."}
    if not decrypt_secret(settings.api_key_encrypted):
        return {"ok": False, "error_type": "invalid_configuration", "message": "Falta API key de OpenAI."}
    return {"ok": True, "error_type": None, "message": "Configuracion IA valida."}


def redact_email_config(settings: EmailSettings) -> dict:
    return {
        "imap_host": settings.imap_host,
        "imap_port": settings.imap_port,
        "imap_username": settings.imap_username,
        "imap_password": "••••••••" if settings.imap_password_encrypted else "",
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_username": settings.smtp_username,
        "smtp_password": "••••••••" if settings.smtp_password_encrypted else "",
        "from_email": settings.from_email,
        "provider": settings.provider,
        "smtp_provider": settings.smtp_provider,
    }


def redact_llm_config(settings: LLMSettings) -> dict:
    return {
        "provider": settings.provider,
        "base_url": settings.base_url,
        "classification_model": settings.classification_model,
        "extraction_model": settings.extraction_model,
        "validation_model": settings.validation_model,
        "api_key": "••••••••" if settings.api_key_encrypted else "",
    }


def _parse_imap_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _clamp_initial_history_limit(value: int | str | None) -> int:
    try:
        parsed = int(value or 50)
    except (TypeError, ValueError):
        parsed = 50
    return max(1, min(parsed, IMAP_INITIAL_HISTORY_MAX))


def _normalize_initial_history_mode(value: str | None) -> str:
    normalized = (value or "new").strip().lower()
    return normalized if normalized in INITIAL_HISTORY_MODES else "new"


def _advance_uid(uid: str | None) -> str | None:
    if not uid:
        return None
    try:
        return str(int(uid) + 1)
    except ValueError:
        return uid


def _initial_history_plan(settings: EmailSettings) -> dict:
    mode = _normalize_initial_history_mode(getattr(settings, "initial_history_mode", None))
    limit = _clamp_initial_history_limit(getattr(settings, "initial_history_limit", None))
    from_date = _parse_imap_date(settings.read_from_date)
    if mode == "7d":
        from_date = datetime.now(timezone.utc).date() - timedelta(days=7)
    elif mode == "30d":
        from_date = datetime.now(timezone.utc).date() - timedelta(days=30)
    elif mode == "100":
        limit = IMAP_INITIAL_HISTORY_MAX
    elif mode == "custom" and not from_date:
        raise ValueError("Indica una fecha valida para el historial inicial (AAAA-MM-DD).")
    return {
        "mode": mode,
        "limit": limit,
        "from_date": from_date,
        "date_label": from_date.strftime("%d/%m/%Y") if from_date else None,
        "title": {
            "new": "Solo correos nuevos desde ahora",
            "7d": "Últimos 7 días",
            "30d": "Últimos 30 días",
            "100": "Últimos 100 correos",
            "custom": "Desde fecha personalizada",
        }[mode],
    }


def _imap_search_criteria(
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    unread_only: bool = True,
    start_uid: str | None = None,
    end_uid: str | None = None,
) -> list[str]:
    criteria: list[str] = []
    if start_date:
        criteria.extend(["SINCE", start_date.strftime("%d-%b-%Y")])
    if end_date:
        criteria.extend(["BEFORE", (end_date + timedelta(days=1)).strftime("%d-%b-%Y")])
    if start_uid or end_uid:
        uid_range = f"{start_uid or '1'}:{end_uid or '*'}"
        criteria.extend(["UID", uid_range])
    if unread_only:
        criteria.append("UNSEEN")
    return criteria or ["ALL"]


def _imap_uid_search(client, *criteria: str) -> list[bytes] | None:  # noqa: ANN001
    status, data = client.uid("search", None, *criteria)
    if status != "OK" or not data:
        return None
    return (data[0] or b"").split()


def test_imap_connection(settings: EmailSettings, *, request_id: str | None = None) -> dict:
    validation = validate_imap_config(settings)
    if not validation["ok"]:
        return {"ok": False, "error_type": validation["error_type"], "found": 0, "new": 0, "duplicates": 0, "last_email": "", "message": validation["message"]}
    password, error_message = _imap_password_status(settings)
    if error_message:
        return {"ok": False, "error_type": "invalid_configuration", "found": 0, "new": 0, "duplicates": 0, "last_email": "", "message": error_message}
    if not password:
        return {"ok": False, "error_type": "invalid_configuration", "found": 0, "new": 0, "duplicates": 0, "last_email": "", "message": "La configuración IMAP está incompleta."}
    context = _imap_test_context(settings, request_id)
    client = None
    try:
        if context["security"] not in {"ssl_tls", "starttls", "none"}:
            return {"ok": False, "error_type": "invalid_configuration", "found": 0, "new": 0, "duplicates": 0, "last_email": "", "message": "La configuración SSL/TLS no es válida."}
        client = _imap_client(settings)
        client.login((settings.imap_username or "").strip(), password)
        status, data = client.select(context["mailbox"], readonly=True)
        if status != "OK":
            return {"ok": False, "error_type": "mailbox_not_found", "found": 0, "new": 0, "duplicates": 0, "last_email": "", "message": f"La carpeta {context['mailbox']} no está disponible."}
        ids = _imap_uid_search(client, "UNSEEN" if settings.read_unread_only else "ALL")
        if ids is None:
            return {"ok": False, "error_type": "unexpected_error", "found": 0, "new": 0, "duplicates": 0, "last_email": "", "message": "No se pudieron listar correos."}
        last_email = ""
        if ids:
            fetch_status, msg_data = client.uid("fetch", ids[-1], "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            if fetch_status == "OK" and msg_data and msg_data[0]:
                last_email = msg_data[0][1].decode(errors="ignore").replace("\r", " ").replace("\n", " ")[:300]
        return {"ok": True, "found": int((data[0] or b"0").decode(errors="ignore") or 0), "new": len(ids), "duplicates": 0, "last_email": last_email, "message": f"Conexion correcta. Correos en carpeta: {int((data[0] or b'0').decode(errors='ignore') or 0)}. Coinciden con el filtro: {len(ids)}."}
    except (imaplib.IMAP4.error, socket.timeout, socket.gaierror, ssl.SSLError, ConnectionRefusedError, TimeoutError, OSError) as exc:
        message, error_type = _imap_connection_message(settings, exc)
        _log_imap_test_failure(settings, exc, request_id)
        return {"ok": False, "error_type": error_type or classify_integration_error(exc), "found": 0, "new": 0, "duplicates": 0, "last_email": "", "message": message}
    except Exception as exc:  # noqa: BLE001
        message, error_type = _imap_connection_message(settings, exc)
        _log_imap_test_failure(settings, exc, request_id)
        return {"ok": False, "error_type": error_type or "unexpected_error", "found": 0, "new": 0, "duplicates": 0, "last_email": "", "message": message}
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                logger.debug("IMAP logout ignored", extra=_imap_test_context(settings, request_id))


def preview_initial_imap_sync(settings: EmailSettings) -> dict:
    validation = validate_imap_config(settings)
    if not validation["ok"]:
        return {"ok": False, "message": validation["message"]}
    plan = _initial_history_plan(settings)
    password = decrypt_secret(settings.imap_password_encrypted)
    mailbox = settings.mailbox or settings.inbox_folder or "INBOX"
    try:
        client = _imap_client(settings)
        client.login(settings.imap_username, password)
        status, _ = client.select(mailbox, readonly=True)
        if status != "OK":
            client.logout()
            return {"ok": False, "message": "No se pudo abrir la carpeta IMAP."}
        uidvalidity = _imap_uidvalidity(client, mailbox)
        if plan["mode"] == "new":
            ids = _imap_uid_search(client, "ALL")
            if ids is None:
                client.logout()
                return {"ok": False, "message": "No se pudo leer el histórico IMAP."}
            highest_uid = ids[-1].decode(errors="ignore") if ids else None
            client.logout()
            return {
                "ok": True,
                "mode": plan["mode"],
                "title": plan["title"],
                "limit": 0,
                "found": 0,
                "estimated": 0,
                "date_label": "desde ahora",
                "checkpoint_uid": highest_uid,
                "uidvalidity": uidvalidity,
                "message": "Se guardará el punto de partida actual y no se importará histórico.",
                "warning": None,
            }
        if plan["mode"] == "100":
            ids = _imap_uid_search(client, "ALL")
            if ids is None:
                client.logout()
                return {"ok": False, "message": "No se pudo leer el histórico IMAP."}
            total = len(ids)
            planned = min(total, plan["limit"])
            client.logout()
            warning = "Se importarán solo los 100 correos más recientes." if total > IMAP_INITIAL_HISTORY_MAX else None
            return {
                "ok": True,
                "mode": plan["mode"],
                "title": plan["title"],
                "limit": plan["limit"],
                "found": total,
                "estimated": planned,
                "date_label": None,
                "checkpoint_uid": None,
                "uidvalidity": uidvalidity,
                "message": f"Se importarán aproximadamente {planned} correos.",
                "warning": warning,
            }
        criteria = _imap_search_criteria(start_date=plan["from_date"], unread_only=False)
        ids = _imap_uid_search(client, *criteria)
        if ids is None:
            client.logout()
            return {"ok": False, "message": "No se pudieron listar correos."}
        total = len(ids)
        planned = min(total, plan["limit"])
        client.logout()
        warning = "La interfaz limita la importación inicial a 100 correos." if total > IMAP_INITIAL_HISTORY_MAX else None
        return {
            "ok": True,
            "mode": plan["mode"],
            "title": plan["title"],
            "limit": plan["limit"],
            "found": total,
            "estimated": planned,
            "date_label": plan["date_label"],
            "checkpoint_uid": None,
            "uidvalidity": uidvalidity,
            "message": f"Se importarán aproximadamente {planned} correos desde {plan['date_label']}.",
            "warning": warning,
        }
    except (imaplib.IMAP4.error, socket.timeout, OSError) as exc:
        return {"ok": False, "message": f"Error IMAP: {exc}"}


def run_initial_imap_sync(
    db: Session,
    settings: EmailSettings,
    company_id: int,
    *,
    sync_state: EmailSyncState | None = None,
    sync_session: Session | None = None,
) -> dict:
    plan = _initial_history_plan(settings)
    if plan["mode"] == "new":
        preview = preview_initial_imap_sync(settings)
        if not preview.get("ok"):
            return preview
        checkpoint_uid = preview.get("checkpoint_uid")
        checkpoint_uidvalidity = preview.get("uidvalidity")
        if sync_state and sync_session:
            _update_sync_checkpoint(
                sync_state,
                sync_session,
                mailbox=settings.mailbox or settings.inbox_folder or "INBOX",
                uidvalidity=checkpoint_uidvalidity,
                source_provider=(settings.provider or "imap").strip().lower() or "imap",
                source_host=(settings.imap_host or "").strip() or None,
                source_username=(settings.imap_username or "").strip() or None,
                source_connected_email=(settings.connected_email or settings.imap_username or "").strip() or None,
                last_uid=checkpoint_uid,
                saved=0,
                duplicates=0,
                attachments_saved=0,
                found=0,
                status="idle",
                progress=0,
                total=0,
            )
        _update_sync_status(settings, True, 0, 0, "Punto de partida guardado. No se importó histórico.")
        db.commit()
        return {"ok": True, "found": 0, "saved": 0, "duplicates": 0, "message": "Punto de partida guardado. No se importó histórico.", "checkpoint_uid": checkpoint_uid}
    if plan["mode"] == "100":
        return read_latest_imap_emails(
            db,
            settings,
            company_id,
            auto_process=False,
            unread_only=False,
            limit=plan["limit"],
            sync_state=sync_state,
            sync_session=sync_session,
        )
    return backfill_imap_emails(
        db,
        settings,
        company_id,
        from_date=plan["from_date"].isoformat() if plan["from_date"] else None,
        limit=plan["limit"],
        sync_state=sync_state,
        sync_session=sync_session,
    )


def _imap_client(settings: EmailSettings):
    timeout = IMAP_TIMEOUT_SECONDS
    if settings.imap_security == "ssl_tls" or settings.imap_use_ssl:
        return imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=timeout)
    client = imaplib.IMAP4(settings.imap_host, settings.imap_port, timeout=timeout)
    if settings.imap_security == "starttls":
        client.starttls()
    return client


def _imap_uidvalidity(client, mailbox: str) -> str | None:  # noqa: ANN001
    try:
        status, data = client.status(mailbox, "(UIDVALIDITY)")
        if status != "OK" or not data:
            return None
        text = " ".join(item.decode(errors="ignore") if isinstance(item, bytes) else str(item) for item in data)
        match = re.search(r"UIDVALIDITY\s+(\d+)", text)
        return match.group(1) if match else None
    except Exception:
        return None


def _imap_uid(fetch_meta: str, fallback: str) -> str:
    match = re.search(r"UID\s+(\d+)", fetch_meta or "")
    return match.group(1) if match else fallback


def _normalized_email_external_id(mailbox: str, uidvalidity: str | None, uid: str, mailbox_id: int | None = None) -> str:
    prefix = f"mailbox:{mailbox_id}:" if mailbox_id is not None else ""
    return f"{prefix}{mailbox}:{uidvalidity or 'unknown'}:{uid}"


def _current_imap_scope(settings: EmailSettings, mailbox: str) -> dict[str, str | None]:
    return {
        "provider": (settings.provider or "imap").strip().lower() or "imap",
        "host": (settings.imap_host or "").strip() or None,
        "username": (settings.imap_username or "").strip() or None,
        "connected_email": (settings.connected_email or settings.imap_username or "").strip() or None,
        "mailbox": mailbox,
    }


def _sync_state_matches_scope(sync_state: EmailSyncState | None, scope: dict[str, str | None], uidvalidity: str | None) -> bool:
    if not sync_state or not sync_state.last_seen_uid:
        return False
    if sync_state.mailbox and sync_state.mailbox != scope["mailbox"]:
        return False
    if sync_state.uidvalidity and uidvalidity and sync_state.uidvalidity != uidvalidity:
        return False
    if sync_state.uidvalidity and not uidvalidity:
        return False
    for field, expected in (
        ("source_provider", scope["provider"]),
        ("source_host", scope["host"]),
        ("source_username", scope["username"]),
        ("source_connected_email", scope["connected_email"]),
    ):
        current = getattr(sync_state, field, None)
        if current and expected and current != expected:
            return False
    return True


def _existing_email_for_imap(
    db: Session,
    *,
    company_id: int,
    mailbox: str,
    uidvalidity: str | None,
    uid: str,
    message_id: str | None,
    external_id: str,
    mailbox_id: int | None = None,
) -> Email | None:
    conditions = [Email.external_id == external_id]
    if message_id:
        conditions.append(
            and_(Email.message_id == message_id, Email.mailbox_id == mailbox_id)
            if mailbox_id is not None
            else Email.message_id == message_id
        )
    if uidvalidity:
        uid_conditions = [Email.imap_mailbox == mailbox, Email.imap_uidvalidity == uidvalidity, Email.imap_uid == uid]
        if mailbox_id is not None:
            uid_conditions.append(Email.mailbox_id == mailbox_id)
        conditions.append(and_(*uid_conditions))
    return db.scalar(select(Email).where(Email.company_id == company_id, or_(*conditions)))


def _update_sync_checkpoint(
    sync_state: EmailSyncState | None,
    sync_session: Session | None,
    *,
    mailbox: str | None,
    uidvalidity: str | None,
    source_provider: str | None = None,
    source_host: str | None = None,
    source_username: str | None = None,
    source_connected_email: str | None = None,
    last_uid: str | None,
    saved: int,
    duplicates: int,
    attachments_saved: int,
    found: int,
    status: str,
    error_type: str | None = None,
    error_message: str | None = None,
    progress: int | None = None,
    total: int | None = None,
) -> None:
    if not sync_state or not sync_session:
        return
    now = datetime.now(timezone.utc)
    sync_state.mailbox = mailbox or sync_state.mailbox
    sync_state.uidvalidity = uidvalidity or sync_state.uidvalidity
    sync_state.source_provider = source_provider or sync_state.source_provider
    sync_state.source_host = source_host or sync_state.source_host
    sync_state.source_username = source_username or sync_state.source_username
    sync_state.source_connected_email = source_connected_email or sync_state.source_connected_email
    sync_state.last_seen_uid = last_uid or sync_state.last_seen_uid
    sync_state.last_checkpoint_uid = last_uid or sync_state.last_checkpoint_uid
    sync_state.last_sync_at = now
    sync_state.sync_status = status
    sync_state.status = status
    current_backfill_status = sync_state.backfill_status
    if current_backfill_status in {"paused", "cancelled"} and status == "running":
        sync_state.backfill_status = current_backfill_status
    else:
        sync_state.backfill_status = status
    if status == "running" and not sync_state.backfill_started_at:
        sync_state.backfill_started_at = now
    if status == "completed" or status == "idle":
        sync_state.last_success_at = now
        sync_state.last_successful_sync_at = now
        sync_state.last_error_at = None
        sync_state.last_error_message = None
        sync_state.last_error_type = None
        sync_state.backfill_completed_at = now
    if status == "paused":
        sync_state.backfill_paused_at = now
    if status == "cancelled":
        sync_state.backfill_cancelled_at = now
    if error_message:
        sync_state.last_error_at = now
        sync_state.last_error_message = error_message
        sync_state.last_error_type = error_type
        sync_state.status = "error"
        sync_state.sync_status = "error"
    if progress is not None:
        sync_state.backfill_processed = progress
    if total is not None:
        sync_state.backfill_total = total
    sync_state.backfill_created = max(sync_state.backfill_created or 0, saved)
    sync_state.backfill_duplicates = max(sync_state.backfill_duplicates or 0, duplicates)
    sync_state.backfill_errors = max(sync_state.backfill_errors or 0, 0)
    sync_state.backfill_last_uid = last_uid or sync_state.backfill_last_uid
    sync_state.backfill_last_checkpoint_at = now
    sync_state.backfill_checkpoint_json = json.dumps(
        {
            "mailbox": mailbox,
            "uidvalidity": uidvalidity,
            "last_uid": last_uid,
            "saved": saved,
            "duplicates": duplicates,
            "attachments_saved": attachments_saved,
            "found": found,
            "status": status,
            "progress": progress,
            "total": total,
            "error_type": error_type,
            "error_message": error_message,
        },
        ensure_ascii=False,
    )
    sync_session.commit()


def _fetch_imap_emails(
    db: Session,
    settings: EmailSettings,
    company_id: int,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    start_uid: str | None = None,
    end_uid: str | None = None,
    unread_only: bool = True,
    limit: int | None = None,
    auto_process: bool | None = None,
    label: str = "Lectura IMAP",
    sync_state: EmailSyncState | MailboxSyncState | None = None,
    sync_session: Session | None = None,
    mailbox_id: int | None = None,
    batch_size: int | None = None,
    stop_after_batch: bool = False,
) -> dict:
    password = decrypt_secret(settings.imap_password_encrypted)
    if not settings.imap_host or not settings.imap_username or not password:
        return {"ok": False, "found": 0, "saved": 0, "message": "Faltan host, usuario o password IMAP."}
    sync_lock = SYNC_LOCKS.setdefault((company_id, mailbox_id), threading.Lock())
    if not sync_lock.acquire(blocking=False):
        return {"ok": False, "found": 0, "saved": 0, "message": "Ya hay una sincronizacion IMAP en curso."}
    found = saved = attachments_saved = 0
    duplicates = 0
    errors = 0
    downloaded = 0
    discarded = 0
    mailbox = settings.mailbox or settings.inbox_folder or "INBOX"
    last_processed_uid: str | None = None
    should_auto_process = settings.auto_process_on_fetch if auto_process is None else auto_process
    last_seen_uid_before = sync_state.last_seen_uid if sync_state else None
    current_scope = _current_imap_scope(settings, mailbox)
    try:
        settings.last_sync_message = f"{label} en curso"
        settings.last_sync_error = None
        db.commit()
    except Exception:
        db.rollback()
    log_action(db, company_id=company_id, user=None, action="email.fetch_started", entity_type="email", message=f"{label} iniciada")
    try:
        client = _imap_client(settings)
        client.login(settings.imap_username, password)
        client.select(mailbox, readonly=not settings.mark_as_read_after_import)
        uidvalidity = _imap_uidvalidity(client, mailbox)
        effective_start_uid = start_uid
        if not effective_start_uid and not end_uid and not start_date and not end_date and sync_state and sync_state.backfill_status not in {"running", "paused"}:
            if _sync_state_matches_scope(sync_state, current_scope, uidvalidity):
                effective_start_uid = _advance_uid(sync_state.last_seen_uid)
            else:
                logger.info(
                    "email.sync.scope_reset",
                    extra={
                        "event": "email.sync.scope_reset",
                        "company_id": company_id,
                        "settings_id": getattr(settings, "id", None),
                        "mailbox": mailbox,
                        "uidvalidity": uidvalidity,
                        "last_seen_uid_before": last_seen_uid_before,
                        "source_provider": current_scope["provider"],
                        "source_host": current_scope["host"],
                        "source_username": current_scope["username"],
                        "source_connected_email": current_scope["connected_email"],
                    },
                )
                seed_ids = _imap_uid_search(client, "ALL")
                if seed_ids is None:
                    client.logout()
                    return {"ok": False, "found": 0, "saved": 0, "downloaded": 0, "duplicates": 0, "discarded": 0, "errors": 0, "message": "No se pudo leer el buzón para fijar el nuevo punto de partida."}
                highest_uid = seed_ids[-1].decode(errors="ignore") if seed_ids else None
                if sync_state and sync_session:
                    _update_sync_checkpoint(
                        sync_state,
                        sync_session,
                        mailbox=mailbox,
                        uidvalidity=uidvalidity,
                        source_provider=current_scope["provider"],
                        source_host=current_scope["host"],
                        source_username=current_scope["username"],
                        source_connected_email=current_scope["connected_email"],
                        last_uid=highest_uid,
                        saved=0,
                        duplicates=0,
                        attachments_saved=0,
                        found=0,
                        status="idle",
                        progress=0,
                        total=0,
                    )
                _update_sync_status(settings, True, 0, 0, "Se ha detectado un cambio de buzón y se ha guardado el punto de partida actual.")
                db.commit()
                log_action(db, company_id=company_id, user=None, action="email.sync.scope_reset", entity_type="email", message=f"Punto de partida actualizado para {mailbox}")
                logger.info(
                    "email.sync.scope_reset_completed",
                    extra={
                        "event": "email.sync.scope_reset_completed",
                        "company_id": company_id,
                        "settings_id": getattr(settings, "id", None),
                        "mailbox": mailbox,
                        "uidvalidity": uidvalidity,
                        "last_seen_uid_before": last_seen_uid_before,
                        "last_seen_uid_after": highest_uid,
                    },
                )
                client.logout()
                return {
                    "ok": True,
                    "found": 0,
                    "downloaded": 0,
                    "saved": 0,
                    "duplicates": 0,
                    "discarded": 0,
                    "attachments": 0,
                    "errors": 0,
                    "uidvalidity": uidvalidity,
                    "last_seen_uid_before": last_seen_uid_before,
                    "last_seen_uid_after": highest_uid,
                    "message": settings.last_sync_message,
                }
        criteria = _imap_search_criteria(start_date=start_date, end_date=end_date, unread_only=unread_only, start_uid=effective_start_uid, end_uid=end_uid)
        logger.info(
            "email.sync.search",
            extra={
                "event": "email.sync.search",
                "company_id": company_id,
                "settings_id": getattr(settings, "id", None),
                "mailbox": mailbox,
                "uidvalidity": uidvalidity,
                "last_seen_uid_before": last_seen_uid_before,
                "search_criteria": criteria,
            },
        )
        ids = _imap_uid_search(client, *criteria)
        if ids is None:
            client.logout()
            return {"ok": False, "found": 0, "saved": 0, "downloaded": 0, "duplicates": 0, "discarded": 0, "errors": 0, "message": "No se pudieron listar correos."}
        if start_date or end_date:
            ids = sorted(ids, key=lambda raw: int(raw.decode(errors="ignore") or 0))
        if limit:
            limit = max(int(limit), 1)
            limit = min(limit, IMAP_MAX_MESSAGES_PER_RUN)
            ids = ids[:limit] if (start_date or end_date) else ids[-limit:]
        found = len(ids)
        batch_size = max(min(int(batch_size or settings.read_limit or 10), IMAP_MAX_MESSAGES_PER_RUN), 1)
        saved_email_ids: list[int] = []
        checkpoint_uid = sync_state.last_checkpoint_uid if sync_state and sync_state.backfill_status == "paused" else None
        if sync_state and sync_state.backfill_status == "running" and sync_state.backfill_last_uid:
            checkpoint_uid = sync_state.backfill_last_uid
        if not checkpoint_uid and not start_uid and not end_uid and not start_date and not end_date and sync_state and sync_state.last_seen_uid:
            checkpoint_uid = sync_state.last_seen_uid
        processed_since_checkpoint = 0
        last_processed_uid = checkpoint_uid
        has_more = False
        batch_count = 0
        for offset in range(0, len(ids), batch_size):
            batch = ids[offset : offset + batch_size]
            batch_count = len(batch)
            for msg_id in batch:
                try:
                    status, msg_data = client.uid("fetch", msg_id, "(UID RFC822)")
                    if status != "OK" or not msg_data or not msg_data[0]:
                        errors += 1
                        continue
                    downloaded += 1
                    raw = msg_data[0][1]
                    fetch_meta = msg_data[0][0].decode(errors="ignore") if isinstance(msg_data[0], tuple) else ""
                    uid = _imap_uid(fetch_meta, msg_id.decode(errors="ignore"))
                    if checkpoint_uid:
                        try:
                            if int(uid) <= int(checkpoint_uid):
                                discarded += 1
                                continue
                        except ValueError:
                            if uid <= checkpoint_uid:
                                discarded += 1
                                continue
                    msg = message_from_bytes(raw, policy=policy.default)
                    message_id = msg.get("Message-ID") or None
                    dedupe_external_id = _normalized_email_external_id(mailbox, uidvalidity, uid, mailbox_id)
                    exists = _existing_email_for_imap(
                        db,
                        company_id=company_id,
                        mailbox=mailbox,
                        uidvalidity=uidvalidity,
                        uid=uid,
                        message_id=message_id,
                        external_id=dedupe_external_id,
                        mailbox_id=mailbox_id,
                    )
                    if exists:
                        duplicates += 1
                        processed_since_checkpoint += 1
                        last_processed_uid = uid
                        if sync_state:
                            sync_state.backfill_last_uid = uid
                        log_action(db, company_id=company_id, user=None, action="email.duplicate_ignored", entity_type="email", entity_id=exists.id, message=f"Duplicado ignorado: {message_id or dedupe_external_id}")
                        continue
                    subject = _decode_mime_header(msg.get("Subject", ""))
                    sender = _decode_mime_header(msg.get("From", ""))
                    # Keep the legacy extraction hook in the IMAP path while
                    # retaining the original HTML for Communications.
                    body = _extract_body(msg)
                    body_html = _extract_body_parts(msg)[1]
                    email = Email(
                        company_id=company_id,
                        external_id=dedupe_external_id,
                        message_id=message_id,
                        imap_mailbox=mailbox,
                        imap_uidvalidity=uidvalidity,
                        imap_uid=uid,
                        mailbox_id=mailbox_id,
                        sender=sender,
                        subject=subject,
                        body=body,
                        extracted_text=body,
                        status="pending",
                        agent_status="not_processed",
                        is_read=False,
                        archived=False,
                        detected_type=None,
                    )
                    db.add(email)
                    db.flush()
                    inbound_message = _create_inbound_message(db, company_id, email, settings, msg, body)
                    inbound_message.source_message_id = message_id
                    inbound_message.source_mailbox = mailbox
                    inbound_message.source_uidvalidity = uidvalidity
                    inbound_message.source_uid = uid
                    inbound_message.mailbox_id = mailbox_id
                    communication = None
                    if mailbox_id is not None:
                        sender_name, sender_email = parseaddr(sender)
                        communication = create_or_update_communication_from_email(
                            db,
                            company_id=company_id,
                            mailbox_id=mailbox_id,
                            provider=settings.provider or "imap",
                            external_message_id=message_id or dedupe_external_id,
                            thread_id=msg.get("In-Reply-To") or msg.get("References"),
                            sender_email=sender_email or None,
                            sender_name=_decode_mime_header(sender_name) or None,
                            to_recipients=_header_addresses(msg, "To"),
                            cc_recipients=_header_addresses(msg, "Cc"),
                            bcc_recipients=_header_addresses(msg, "Bcc"),
                            subject=subject,
                            body_text=body,
                            body_html=body_html,
                            received_at=email.received_at,
                            metadata={
                                "message_id": message_id,
                                "imap_mailbox": mailbox,
                                "imap_uidvalidity": uidvalidity,
                                "imap_uid": uid,
                            },
                        )
                        email.communication_id = communication.id
                    attachment_count = _save_attachments(
                        db,
                        company_id,
                        email,
                        msg,
                        inbound_message=inbound_message,
                        communication=communication,
                        max_attachments=IMAP_MAX_ATTACHMENTS_PER_EMAIL,
                        max_attachment_size_mb=IMAP_MAX_ATTACHMENT_SIZE_MB,
                    )
                    attachments_saved += attachment_count
                    email.has_attachments = attachment_count > 0
                    email.has_pdf = any(att.is_pdf for att in email.attachments)
                    pdf_texts = [att.extracted_text for att in email.attachments if att.is_pdf and att.extracted_text]
                    if pdf_texts:
                        email.extracted_text = "\n\n".join(pdf_texts)
                        inbound_message.normalized_text = email.extracted_text
                    inbound_message.has_attachments = email.has_attachments
                    inbound_message.has_pdf = email.has_pdf
                    inbound_message.original_content = body
                    if communication:
                        mark_communication_processed(db, communication)
                    saved += 1
                    saved_email_ids.append(email.id)
                    processed_since_checkpoint += 1
                    last_processed_uid = uid
                    if sync_state:
                        sync_state.backfill_last_uid = uid
                    log_action(db, company_id=company_id, user=None, action="email.saved", entity_type="email", entity_id=email.id, message=f"Correo guardado: {subject[:120]}")
                    if settings.mark_as_read_after_import:
                        client.uid("store", msg_id, "+FLAGS", "\\Seen")
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    db.rollback()
                    logger.warning(
                        "email.sync.message_error",
                        extra={
                            "event": "email.sync.message_error",
                            "company_id": company_id,
                            "settings_id": getattr(settings, "id", None),
                            "mailbox": mailbox,
                            "uidvalidity": uidvalidity,
                            "message_uid": msg_id.decode(errors="ignore") if isinstance(msg_id, bytes) else str(msg_id),
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
            db.commit()
            if should_auto_process and saved_email_ids:
                for email_id in saved_email_ids:
                    enqueue_job(
                        db,
                        company_id=company_id,
                        job_type="process_email",
                        payload={"email_id": email_id},
                        created_by_user_id=None,
                    )
                db.commit()
            if sync_state and sync_session:
                sync_state.backfill_status = "running" if offset + batch_size < len(ids) else "idle"
                _update_sync_checkpoint(
                    sync_state,
                    sync_session,
                    mailbox=mailbox,
                    uidvalidity=uidvalidity,
                    source_provider=current_scope["provider"],
                    source_host=current_scope["host"],
                    source_username=current_scope["username"],
                    source_connected_email=current_scope["connected_email"],
                    last_uid=last_processed_uid,
                    saved=saved,
                    duplicates=duplicates,
                    attachments_saved=attachments_saved,
                    found=found,
                    status="running",
                    progress=processed_since_checkpoint,
                    total=found,
                )
            if sync_state and sync_state.backfill_status in {"paused", "cancelled"}:
                break
            saved_email_ids.clear()

            if stop_after_batch and offset + batch_size < len(ids):
                has_more = True
                break

        client.logout()
        final_status = sync_state.backfill_status if sync_state else "idle"
        if sync_state and sync_session:
            if final_status == "paused":
                _update_sync_checkpoint(
                    sync_state,
                    sync_session,
                    mailbox=mailbox,
                    uidvalidity=uidvalidity,
                    source_provider=current_scope["provider"],
                    source_host=current_scope["host"],
                    source_username=current_scope["username"],
                    source_connected_email=current_scope["connected_email"],
                    last_uid=last_processed_uid,
                    saved=saved,
                    duplicates=duplicates,
                    attachments_saved=attachments_saved,
                    found=found,
                    status="paused",
                    progress=processed_since_checkpoint,
                    total=found,
                )
            elif final_status == "cancelled":
                _update_sync_checkpoint(
                    sync_state,
                    sync_session,
                    mailbox=mailbox,
                    uidvalidity=uidvalidity,
                    source_provider=current_scope["provider"],
                    source_host=current_scope["host"],
                    source_username=current_scope["username"],
                    source_connected_email=current_scope["connected_email"],
                    last_uid=last_processed_uid,
                    saved=saved,
                    duplicates=duplicates,
                    attachments_saved=attachments_saved,
                    found=found,
                    status="cancelled",
                    progress=processed_since_checkpoint,
                    total=found,
                )
            elif has_more:
                _update_sync_checkpoint(
                    sync_state,
                    sync_session,
                    mailbox=mailbox,
                    uidvalidity=uidvalidity,
                    source_provider=current_scope["provider"],
                    source_host=current_scope["host"],
                    source_username=current_scope["username"],
                    source_connected_email=current_scope["connected_email"],
                    last_uid=last_processed_uid or sync_state.backfill_last_uid,
                    saved=saved,
                    duplicates=duplicates,
                    attachments_saved=attachments_saved,
                    found=found,
                    status="running",
                    progress=processed_since_checkpoint,
                    total=found,
                )
            else:
                _update_sync_checkpoint(
                    sync_state,
                    sync_session,
                    mailbox=mailbox,
                    uidvalidity=uidvalidity,
                    source_provider=current_scope["provider"],
                    source_host=current_scope["host"],
                    source_username=current_scope["username"],
                    source_connected_email=current_scope["connected_email"],
                    last_uid=last_processed_uid or sync_state.backfill_last_uid,
                    saved=saved,
                    duplicates=duplicates,
                    attachments_saved=attachments_saved,
                    found=found,
                    status="idle",
                    progress=processed_since_checkpoint,
                    total=found,
                )
        _update_sync_status(settings, True, saved, duplicates, f"{found} correos encontrados, {downloaded} descargados, {saved} importados, {duplicates} duplicados ignorados, {discarded} descartados, {errors} errores, {attachments_saved} adjuntos guardados.")
        db.commit()
        log_action(db, company_id=company_id, user=None, action="email.fetch_completed", entity_type="email", message=settings.last_sync_message or "")
        logger.info(
            "email.sync.completed",
            extra={
                "event": "email.sync.completed",
                "company_id": company_id,
                "settings_id": getattr(settings, "id", None),
                "mailbox": mailbox,
                "uidvalidity": uidvalidity,
                "last_seen_uid_before": last_seen_uid_before,
                "last_seen_uid_after": last_processed_uid,
                "found": found,
                "downloaded": downloaded,
                "saved": saved,
                "duplicates": duplicates,
                "discarded": discarded,
                "errors": errors,
                "attachments_saved": attachments_saved,
            },
        )
        return {
            "ok": True,
            "found": found,
            "downloaded": downloaded,
            "saved": saved,
            "duplicates": duplicates,
            "discarded": discarded,
            "attachments": attachments_saved,
            "errors": errors,
            "uidvalidity": uidvalidity,
            "last_seen_uid_before": last_seen_uid_before,
            "last_seen_uid_after": last_processed_uid,
            "last_uid": last_processed_uid,
            "has_more": has_more,
            "batch_count": batch_count,
            "message": settings.last_sync_message,
        }
    except (imaplib.IMAP4.error, socket.timeout, OSError) as exc:
        message = f"Error IMAP: {exc}"
        _update_sync_status(settings, False, saved, duplicates, message, message)
        db.commit()
        log_action(db, company_id=company_id, user=None, action="email.fetch_error", entity_type="email", message=message)
        if sync_state and sync_session:
            _update_sync_checkpoint(
                sync_state,
                sync_session,
                mailbox=mailbox,
                uidvalidity=None,
                source_provider=current_scope["provider"],
                source_host=current_scope["host"],
                source_username=current_scope["username"],
                source_connected_email=current_scope["connected_email"],
                last_uid=last_processed_uid,
                saved=saved,
                duplicates=duplicates,
                attachments_saved=attachments_saved,
                found=found,
                status="error",
                error_type=classify_integration_error(exc),
                error_message=message,
                progress=processed_since_checkpoint if "processed_since_checkpoint" in locals() else 0,
                total=found,
            )
        return {"ok": False, "found": found, "downloaded": downloaded, "saved": saved, "duplicates": duplicates, "discarded": discarded, "attachments": attachments_saved, "errors": errors, "message": message}
    finally:
        sync_lock.release()


def read_latest_imap_emails(
    db: Session,
    settings: EmailSettings,
    company_id: int,
    *,
    auto_process: bool | None = None,
    unread_only: bool | None = None,
    limit: int | None = None,
    sync_state: EmailSyncState | MailboxSyncState | None = None,
    sync_session: Session | None = None,
    mailbox_id: int | None = None,
) -> dict:
    effective_limit = limit if limit is not None else IMAP_RECENT_MESSAGES_LIMIT
    return _fetch_imap_emails(
        db,
        settings,
        company_id,
        unread_only=False if unread_only is None else unread_only,
        limit=min(max(int(effective_limit or IMAP_RECENT_MESSAGES_LIMIT), 1), IMAP_MAX_MESSAGES_PER_RUN),
        auto_process=auto_process,
        label="Lectura IMAP",
        sync_state=sync_state,
        sync_session=sync_session,
        mailbox_id=mailbox_id,
    )


def backfill_imap_emails(
    db: Session,
    settings: EmailSettings,
    company_id: int,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int | None = None,
    *,
    from_uid: str | None = None,
    to_uid: str | None = None,
    batch_size: int | None = None,
    resume: bool = False,
    stop_after_batch: bool = False,
    sync_state: EmailSyncState | MailboxSyncState | None = None,
    sync_session: Session | None = None,
    mailbox_id: int | None = None,
) -> dict:
    start_date = _parse_imap_date(from_date or settings.read_from_date)
    if not start_date:
        start_date = datetime.now(timezone.utc).date() - timedelta(days=30)
    end_date = _parse_imap_date(to_date)
    if end_date and start_date and end_date < start_date:
        return {"ok": False, "found": 0, "saved": 0, "message": "La fecha final no puede ser anterior a la inicial."}
    if not start_date:
        return {"ok": False, "found": 0, "saved": 0, "message": "Indica una fecha valida para el backfill (AAAA-MM-DD)."}
    if resume and sync_state and sync_state.backfill_last_uid:
        from_uid = _advance_uid(sync_state.backfill_last_uid)
    return _fetch_imap_emails(
        db,
        settings,
        company_id,
        start_date=start_date,
        end_date=end_date,
        start_uid=from_uid,
        end_uid=to_uid,
        unread_only=False,
        limit=limit,
        auto_process=False,
        label=f"Backfill IMAP desde {start_date.strftime('%d/%m/%Y')}{' hasta ' + end_date.strftime('%d/%m/%Y') if end_date else ''}",
        sync_state=sync_state,
        sync_session=sync_session,
        mailbox_id=mailbox_id,
        batch_size=batch_size,
        stop_after_batch=stop_after_batch,
    )


def _update_sync_status(settings: EmailSettings, ok: bool, saved: int, duplicates: int, message: str, error: str | None = None) -> None:
    settings.last_sync_at = datetime.now(timezone.utc)
    settings.last_sync_ok = ok
    settings.last_sync_message = message
    settings.last_sync_error = error
    settings.last_sync_new = saved
    settings.last_sync_duplicates = duplicates


def _decode_mime_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value or ""))).strip()
    except Exception:
        return value or ""


def _extract_body_parts(msg: EmailMessage) -> tuple[str, str | None]:
    if msg.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
            if content_type == "text/plain":
                plain_parts.append(text)
            else:
                html_parts.append(text)
        body_html = "\n\n".join(html_parts).strip() or None
        return "\n\n".join(plain_parts).strip() or _strip_html(body_html or ""), body_html
    payload = msg.get_payload(decode=True)
    if not payload:
        return "", None
    text = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore").strip()
    if msg.get_content_type() == "text/html":
        return _strip_html(text), text
    return text, None


def _extract_body(msg: EmailMessage) -> str:
    return _extract_body_parts(msg)[0]


def _header_addresses(msg: EmailMessage, header: str) -> list[str]:
    addresses: list[str] = []
    for name, address in getaddresses(msg.get_all(header, [])):
        if address.strip():
            addresses.append(address.strip())
    return addresses


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _safe_filename(filename: str) -> str:
    name = _decode_mime_header(filename or "adjunto")
    name = re.sub(r"[^\w.\- áéíóúàèìòùäëïöüñÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÑ]+", "_", name, flags=re.UNICODE).strip("._ ")
    return name[:160] or "adjunto"


def _is_attachment(part: EmailMessage) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    filename = part.get_filename()
    content_type = part.get_content_type()
    return bool(filename) or disposition in {"attachment", "inline"} and content_type not in {"text/plain", "text/html"}


def _create_inbound_message(
    db: Session,
    company_id: int,
    email: Email,
    settings: EmailSettings,
    msg: EmailMessage,
    body: str,
) -> InboundMessage:
    metadata = {
        "message_id": email.message_id or email.external_id,
        "from": email.sender,
        "subject": email.subject,
        "date": msg.get("Date"),
        "imap_mailbox": email.imap_mailbox or settings.mailbox or settings.inbox_folder,
        "imap_uidvalidity": email.imap_uidvalidity,
        "imap_uid": email.imap_uid,
    }

    normalized = NormalizedMessage(
        company_id=company_id,
        channel_key="email",
        provider=settings.provider or "imap",
        external_id=email.external_id,
        sender=email.sender,
        recipients=[settings.connected_email or settings.imap_username]
        if (settings.connected_email or settings.imap_username)
        else [],
        subject=email.subject,
        text_content=body,
        direction="inbound",
        external_thread_id=msg.get("In-Reply-To") or msg.get("References"),
        received_at=email.received_at,
        metadata=metadata,
    )

    inbound_message, conversation = persist_normalized_message(
        db,
        normalized,
        content_type="email",
        has_attachments=False,
        has_pdf=False,
        has_audio=False,
    )

    email.conversation_id = conversation.id
    inbound_message.conversation_id = conversation.id
    inbound_message.raw_payload_json = (
        inbound_message.raw_payload_json
        or json.dumps(metadata, ensure_ascii=False)
    )
    return inbound_message


def _save_attachments(
    db: Session,
    company_id: int,
    email: Email,
    msg: EmailMessage,
    inbound_message: InboundMessage | None = None,
    communication: Communication | None = None,
    *,
    max_attachments: int = IMAP_MAX_ATTACHMENTS_PER_EMAIL,
    max_attachment_size_mb: int = IMAP_MAX_ATTACHMENT_SIZE_MB,
) -> int:
    count = 0
    for part in msg.walk():
        if not _is_attachment(part):
            continue
        if count >= max_attachments:
            break
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if len(payload) > max_attachment_size_mb * 1024 * 1024:
            log_action(
                db,
                company_id=company_id,
                user=None,
                action="email.attachment_skipped",
                entity_type="email_attachment",
                message=f"Adjunto omitido por tamano: {part.get_filename() or 'adjunto'}",
            )
            continue
        filename = _safe_filename(part.get_filename() or f"adjunto-{count + 1}")
        content_type = part.get_content_type() or "application/octet-stream"
        is_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf")
        storage_path = save_attachment(
            filename=f"email-{email.id}-{filename}",
            payload=payload,
            content_type=content_type,
        )
        attachment = EmailAttachment(
            company_id=company_id,
            email_id=email.id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(payload),
            is_pdf=is_pdf,
            extraction_status="pending" if is_pdf else "not_applicable",
            storage_path=storage_path,
        )
        db.add(attachment)
        db.flush()
        if inbound_message:
            message_attachment = MessageAttachment(
                company_id=company_id,
                inbound_message_id=inbound_message.id,
                filename=filename,
                content_type=content_type,
                size_bytes=len(payload),
                storage_path=storage_path,
                extracted_text=attachment.extracted_text,
                is_pdf=is_pdf,
                is_image=content_type.startswith("image/"),
                is_audio=content_type.startswith("audio/"),
                extraction_status=attachment.extraction_status,
                extraction_error=attachment.extraction_error,
            )
            db.add(message_attachment)
            db.flush()
        log_action(db, company_id=company_id, user=None, action="email.attachment_saved", entity_type="email_attachment", entity_id=attachment.id, message=f"Adjunto guardado: {filename}")
        if is_pdf:
            _extract_pdf_text(db, attachment)
            if inbound_message:
                message_attachment.extracted_text = attachment.extracted_text
                message_attachment.extraction_status = attachment.extraction_status
                message_attachment.extraction_error = attachment.extraction_error
        if communication:
            add_communication_attachment(
                db,
                communication,
                filename=filename,
                mime_type=content_type,
                payload=payload,
                storage_ref=storage_path,
                metadata={"content_disposition": part.get_content_disposition()},
            )
        count += 1
    return count


def _extract_pdf_text(db: Session, attachment: EmailAttachment) -> None:
    try:
        data = read_attachment(attachment.storage_path or "")
        text = _extract_text_from_pdf_bytes(data)
        if text.strip():
            attachment.extracted_text = text.strip()
            attachment.extraction_status = "extracted"
            log_action(db, company_id=attachment.company_id, user=None, action="email.pdf_text_extracted", entity_type="email_attachment", entity_id=attachment.id, message=f"Texto PDF extraido: {attachment.filename}")
        else:
            attachment.extraction_status = "no_text_found"
            attachment.extraction_error = "El PDF no contiene texto legible. Puede requerir OCR."
            log_action(db, company_id=attachment.company_id, user=None, action="email.pdf_text_error", entity_type="email_attachment", entity_id=attachment.id, message=attachment.extraction_error)
    except Exception as exc:
        attachment.extraction_status = "extraction_error"
        attachment.extraction_error = f"No se pudo extraer texto del PDF: {exc}"
        log_action(db, company_id=attachment.company_id, user=None, action="email.pdf_text_error", entity_type="email_attachment", entity_id=attachment.id, message=attachment.extraction_error)


def _extract_text_from_pdf_bytes(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if text.strip():
            return text.strip()
    except Exception:
        logger.exception("Fallo extrayendo PDF con pypdf; usando fallback ligero.")

    chunks: list[str] = []
    raw = data.decode("latin-1", errors="ignore")
    for match in re.finditer(r"\((.*?)\)\s*Tj", raw, re.S):
        chunks.append(_decode_pdf_text(match.group(1)))
    for match in re.finditer(r"\[(.*?)\]\s*TJ", raw, re.S):
        chunks.extend(_decode_pdf_text(item) for item in re.findall(r"\((.*?)\)", match.group(1), re.S))
    text = "\n".join(item for item in chunks if item.strip())
    return re.sub(r"[ \t]+", " ", text)


def _decode_pdf_text(value: str) -> str:
    value = value.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")
    value = re.sub(r"\\([nrtbf])", " ", value)
    value = re.sub(r"\\[0-7]{1,3}", " ", value)
    return value.strip()


def _smtp_client(settings: EmailSettings):
    timeout = 30
    if settings.smtp_security == "ssl_tls":
        return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout)
    client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    if settings.smtp_security == "starttls":
        client.starttls()
    return client


def test_smtp_connection(settings: EmailSettings) -> dict:
    validation = validate_smtp_config(settings)
    if not validation["ok"]:
        return {"ok": False, "error_type": validation["error_type"], "message": validation["message"]}
    password = decrypt_secret(settings.smtp_password_encrypted)
    try:
        client = _smtp_client(settings)
        client.login(settings.smtp_username, password)
        client.quit()
        return {"ok": True, "message": "Conexion SMTP correcta."}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "error_type": "authentication_failed", "message": "Error de autenticacion SMTP."}
    except smtplib.SMTPServerDisconnected:
        return {"ok": False, "error_type": "connection_failed", "message": "Error de servidor SMTP: conexion cerrada."}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "error_type": "timeout", "message": "Timeout conectando con SMTP."}
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "error_type": classify_integration_error(exc), "message": f"Error SMTP: {exc}"}


def send_test_email(settings: EmailSettings, to_email: str, subject: str, message: str) -> dict:
    password = decrypt_secret(settings.smtp_password_encrypted)
    from_email = settings.from_email or settings.smtp_username
    if not settings.smtp_host or not settings.smtp_username or not password or not from_email:
        return {"ok": False, "error_type": "invalid_configuration", "message": "Faltan datos SMTP o remitente."}
    try:
        email = EmailMessage()
        email["From"] = f"{settings.from_name} <{from_email}>" if settings.from_name else from_email
        email["To"] = to_email
        email["Subject"] = subject
        if settings.reply_to:
            email["Reply-To"] = settings.reply_to
        if settings.default_cc:
            email["Cc"] = settings.default_cc
        if settings.default_bcc:
            email["Bcc"] = settings.default_bcc
        email.set_content(message)
        recipients = [to_email]
        recipients += [item.strip() for item in (settings.default_cc or "").split(",") if item.strip()]
        recipients += [item.strip() for item in (settings.default_bcc or "").split(",") if item.strip()]
        client = _smtp_client(settings)
        client.login(settings.smtp_username, password)
        client.send_message(email, from_addr=from_email, to_addrs=recipients)
        client.quit()
        return {"ok": True, "message": "Correo de prueba enviado correctamente."}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "error_type": "authentication_failed", "message": "Error de autenticacion SMTP."}
    except smtplib.SMTPRecipientsRefused:
        return {"ok": False, "error_type": "permission_denied", "message": "El servidor rechazo el destinatario."}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "error_type": "timeout", "message": "Timeout enviando el correo de prueba."}
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "error_type": classify_integration_error(exc), "message": f"Error SMTP: {exc}"}


def call_openai(settings: LLMSettings, messages: list[dict], model: str) -> dict:
    validation = validate_openai_config(settings)
    if not validation["ok"]:
        return {"ok": False, "error_type": validation["error_type"], "message": validation["message"]}
    api_key = decrypt_secret(settings.api_key_encrypted)
    base_url = (settings.base_url or "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    last_error = ""
    for _ in range(max(int(settings.retries or 0), 0) + 1):
        try:
            with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
                data = json.loads(response.read().decode())
            return {
                "ok": True,
                "message": "Conexion OpenAI correcta.",
                "content": data["choices"][0]["message"]["content"],
                "usage": data.get("usage") or {},
            }
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="ignore")
            if exc.code in {401, 403}:
                return {"ok": False, "error_type": "authentication_failed", "message": "API key invalida o sin permisos para el proveedor IA."}
            if exc.code == 404:
                return {"ok": False, "error_type": "invalid_configuration", "message": f"Modelo o endpoint no encontrado: {model}."}
            last_error = f"Error OpenAI HTTP {exc.code}: {detail[:300]}"
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            last_error = f"Timeout o error de conexion con OpenAI: {exc}"
    return {"ok": False, "error_type": classify_integration_error(last_error or "Error desconocido conectando con OpenAI."), "message": last_error or "Error desconocido conectando con OpenAI."}


def classify_sample(db: Session, settings: LLMSettings, company_id: int, text: str, prompt: str | None = None) -> dict:
    result = run_prompt_execution(
        db,
        company_id,
        "classification",
        settings,
        text,
        provider_call=call_openai,
        prompt_override=prompt,
    )
    if not result.get("ok"):
        return result
    if not result.get("validation_ok"):
        result["ok"] = False
        result["message"] = "La clasificacion no paso la validacion estructurada."
        return result
    return result


def extract_sample(db: Session, settings: LLMSettings, company_id: int, text: str, prompt: str | None = None) -> dict:
    result = run_prompt_execution(
        db,
        company_id,
        "extraction",
        settings,
        text,
        provider_call=call_openai,
        prompt_override=prompt,
    )
    if not result.get("ok"):
        return result
    if not result.get("validation_ok"):
        result["ok"] = False
        result["message"] = "La extraccion no paso la validacion estructurada."
        return result
    return result
