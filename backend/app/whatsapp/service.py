from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.encryption import decrypt_secret, encrypt_secret
from app.core.attachment_extraction import extract_attachment_text
from app.core.attachment_storage import read_attachment, save_attachment
from app.db.models import ChannelSetting, Conversation, InboundMessage, InputChannel, MessageAttachment, Order, User
from app.jobs.service import enqueue_job
from app.logs.service import log_action
from app.messages.service import (
    NormalizedMessage,
    normalize_recipients,
    persist_normalized_message,
    upsert_inbound_message,
)
from app.master.models import MasterCompany, MasterTenantDatabase


WHATSAPP_CHANNEL_KEY = "whatsapp"
WHATSAPP_PROVIDER = "meta"
WHATSAPP_ONBOARDING_CLOUD_API = "cloud_api"
WHATSAPP_ONBOARDING_COEXISTENCE = "coexistence"
WHATSAPP_COEXISTENCE_FEATURE_TYPE = "whatsapp_business_app_onboarding"
META_ID_PATTERN = re.compile(r"^\d{5,32}$")
WHATSAPP_SUPPORTED_DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
}
WHATSAPP_SUPPORTED_AUDIO_MIME_TYPES = {
    "audio/ogg",
    "audio/mpeg",
    "audio/mp4",
    "audio/wav",
    "audio/x-m4a",
    "audio/webm",
    "audio/amr",
}
WHATSAPP_SUPPORTED_AUDIO_EXTENSIONS = {".amr", ".m4a", ".mp3", ".ogg", ".wav", ".webm"}
WHATSAPP_SUPPORTED_DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".txt"}
_PENDING_OUTBOUND_EXTERNAL_ID_PREFIX = "wa-pending-"

@dataclass(slots=True)
class WhatsAppTenantConfig:
    enabled: bool = False
    provider: str = "meta"
    phone_number_id: str = ""
    business_account_id: str = ""
    business_id: str = ""
    display_phone_number: str = ""
    verified_name: str = ""
    onboarding_mode: str = WHATSAPP_ONBOARDING_CLOUD_API
    is_on_biz_app: bool = False
    account_mode: str = ""
    phone_status: str = ""
    code_verification_status: str = ""
    access_token: str = ""
    verify_token: str = ""
    webhook_enabled: bool = False
    bot_enabled: bool = True
    connection_status: str = "not_connected"
    connected_at: str = ""
    last_error: str = ""
    default_language: str = "es"
    timezone: str = "Europe/Madrid"
    response_window_minutes: int = 24 * 60
    max_auto_messages: int = 3
    max_attachment_bytes: int = 10 * 1024 * 1024


def get_or_create_whatsapp_channel(db: Session, company_id: int) -> InputChannel:
    channel = db.scalar(select(InputChannel).where(InputChannel.company_id == company_id, InputChannel.key == WHATSAPP_CHANNEL_KEY))
    if channel:
        # Keep persisted channel capabilities aligned with the media policy used
        # by the WhatsApp inbox and provider adapter.
        channel.supports_text = True
        channel.supports_attachments = True
        channel.supports_audio = True
        channel.supports_documents = True
        channel.supports_images = False
        return channel
    channel = InputChannel(
        company_id=company_id,
        key=WHATSAPP_CHANNEL_KEY,
        name="WhatsApp",
        channel_type="message",
        is_active=False,
        is_default=False,
        supports_text=True,
        supports_attachments=True,
        supports_audio=True,
        supports_documents=True,
        supports_images=False,
    )
    db.add(channel)
    db.flush()
    return channel


def whatsapp_settings_map(db: Session, company_id: int) -> dict[str, str | None]:
    channel = get_or_create_whatsapp_channel(db, company_id)
    settings = db.scalars(select(ChannelSetting).where(ChannelSetting.company_id == company_id, ChannelSetting.channel_id == channel.id)).all()
    return {setting.key: setting.value for setting in settings}


def _secret_value(value: str | None) -> str:
    if not value:
        return ""
    return decrypt_secret(value) or value


def whatsapp_config(db: Session, company_id: int) -> WhatsAppTenantConfig:
    settings_map = whatsapp_settings_map(db, company_id)
    return WhatsAppTenantConfig(
        enabled=_as_bool(settings_map.get("enabled")),
        provider=(settings_map.get("provider") or WHATSAPP_PROVIDER).strip().lower() or WHATSAPP_PROVIDER,
        phone_number_id=(settings_map.get("phone_number_id") or "").strip(),
        business_account_id=(settings_map.get("business_account_id") or "").strip(),
        business_id=(settings_map.get("business_id") or "").strip(),
        display_phone_number=(settings_map.get("display_phone_number") or "").strip(),
        verified_name=(settings_map.get("verified_name") or "").strip(),
        onboarding_mode=(settings_map.get("onboarding_mode") or WHATSAPP_ONBOARDING_CLOUD_API).strip().lower(),
        is_on_biz_app=_as_bool(settings_map.get("is_on_biz_app")),
        account_mode=(settings_map.get("account_mode") or "").strip(),
        phone_status=(settings_map.get("phone_status") or "").strip(),
        code_verification_status=(settings_map.get("code_verification_status") or "").strip(),
        access_token=_secret_value(settings_map.get("access_token")).strip(),
        verify_token=_secret_value(settings_map.get("verify_token")).strip(),
        webhook_enabled=_as_bool(settings_map.get("webhook_enabled")),
        bot_enabled=_as_bool(settings_map.get("bot_enabled"), default=True),
        connection_status=(settings_map.get("connection_status") or "not_connected").strip().lower(),
        connected_at=(settings_map.get("connected_at") or "").strip(),
        last_error=(settings_map.get("last_error") or "").strip(),
        default_language=(settings_map.get("default_language") or "es").strip(),
        timezone=(settings_map.get("timezone") or "Europe/Madrid").strip(),
        response_window_minutes=_as_int(settings_map.get("response_window_minutes"), 24 * 60),
        max_auto_messages=_as_int(settings_map.get("max_auto_messages"), 3),
        max_attachment_bytes=_as_int(settings_map.get("max_attachment_bytes"), 10 * 1024 * 1024),
    )


def redact_whatsapp_config(config: WhatsAppTenantConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "provider": config.provider,
        "phone_number_id": config.phone_number_id,
        "business_account_id": config.business_account_id,
        "business_id": config.business_id,
        "display_phone_number": config.display_phone_number,
        "verified_name": config.verified_name,
        "onboarding_mode": config.onboarding_mode,
        "is_on_biz_app": config.is_on_biz_app,
        "account_mode": config.account_mode,
        "phone_status": config.phone_status,
        "code_verification_status": config.code_verification_status,
        "access_token": "••••••••" if config.access_token else "",
        "verify_token": "••••••••" if config.verify_token else "",
        "webhook_enabled": config.webhook_enabled,
        "bot_enabled": config.bot_enabled,
        "connection_status": config.connection_status,
        "connected_at": config.connected_at,
        "last_error": config.last_error,
        "default_language": config.default_language,
        "timezone": config.timezone,
        "response_window_minutes": config.response_window_minutes,
        "max_auto_messages": config.max_auto_messages,
        "max_attachment_bytes": config.max_attachment_bytes,
    }


@dataclass(slots=True)
class WhatsAppEmbeddedSignupResult:
    business_account_id: str
    phone_number_id: str
    business_id: str
    display_phone_number: str
    verified_name: str
    webhook_url: str
    onboarding_mode: str = WHATSAPP_ONBOARDING_CLOUD_API
    is_on_biz_app: bool = False
    connection_status: str = "connected"


class WhatsAppEmbeddedSignupError(RuntimeError):
    def __init__(self, message: str, *, error_type: str = "meta_request_failed") -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = error_type in {"timeout", "connection_failed", "media_download_failed", "meta_api_unavailable", "rate_limited"}


_DELIVERY_STATUS_RANK = {
    "recorded": 0,
    "accepted": 1,
    "sent": 2,
    "delivered": 3,
    "read": 4,
    "failed": 5,
}


def whatsapp_ingress_is_ready(
    db: Session,
    company_id: int,
    *,
    config: WhatsAppTenantConfig | None = None,
) -> bool:
    """Return whether this tenant is allowed to receive live WhatsApp events."""
    channel = db.scalar(
        select(InputChannel).where(
            InputChannel.company_id == company_id,
            InputChannel.key == WHATSAPP_CHANNEL_KEY,
            InputChannel.is_active.is_(True),
        )
    )
    if not channel:
        return False
    config = config or whatsapp_config(db, company_id)
    return bool(
        config.enabled
        and config.provider == WHATSAPP_PROVIDER
        and config.connection_status == "connected"
        and config.webhook_enabled
        and config.phone_number_id
        and config.business_account_id
        and config.access_token
        and config.verify_token
    )


def whatsapp_outbound_is_ready(
    db: Session,
    company_id: int,
    *,
    config: WhatsAppTenantConfig | None = None,
) -> bool:
    """Return whether the tenant is allowed to send through Meta."""
    channel = db.scalar(
        select(InputChannel).where(
            InputChannel.company_id == company_id,
            InputChannel.key == WHATSAPP_CHANNEL_KEY,
            InputChannel.is_active.is_(True),
        )
    )
    if not channel:
        return False
    config = config or whatsapp_config(db, company_id)
    return bool(
        config.enabled
        and config.provider == WHATSAPP_PROVIDER
        and config.connection_status == "connected"
        and config.phone_number_id
        and config.access_token
    )


def whatsapp_event_matches_config(event: dict[str, Any], config: WhatsAppTenantConfig) -> bool:
    """Require Meta identifiers to belong to the configured tenant."""
    event_waba = str(event.get("business_account_id") or "").strip()
    event_phone = str(event.get("phone_number_id") or "").strip()
    if str(event.get("kind") or "").strip().lower() == "account_update":
        if not event_waba or not config.business_account_id or event_waba != config.business_account_id:
            return False
        if event_phone and event_phone != config.phone_number_id:
            return False
        event_phone_number = re.sub(r"\D", "", str(event.get("phone_number") or ""))
        configured_phone_number = re.sub(r"\D", "", config.display_phone_number)
        return not event_phone_number or not configured_phone_number or event_phone_number == configured_phone_number
    return bool(
        event_waba
        and event_phone
        and config.business_account_id
        and config.phone_number_id
        and event_waba == config.business_account_id
        and event_phone == config.phone_number_id
    )


def _should_advance_delivery_status(current: str | None, incoming: str) -> bool:
    current_key = str(current or "").strip().lower()
    incoming_key = str(incoming or "").strip().lower()
    if not incoming_key:
        return False
    if incoming_key == "failed" and current_key in {"delivered", "read"}:
        # A delayed failure callback must not make a successfully delivered
        # or read message appear failed in the inbox.
        return False
    if current_key not in _DELIVERY_STATUS_RANK:
        return True
    return _DELIVERY_STATUS_RANK.get(incoming_key, -1) > _DELIVERY_STATUS_RANK[current_key]


def _whatsapp_status_error(payload: dict[str, Any]) -> str:
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first_error = errors[0]
        if isinstance(first_error, dict):
            parts = [
                str(first_error.get(key) or "").strip()
                for key in ("code", "title", "message", "details")
            ]
            detail = " · ".join(part for part in parts if part)
            if detail:
                return detail[:2000]
    error = payload.get("error")
    if isinstance(error, dict):
        detail = " · ".join(
            str(error.get(key) or "").strip()
            for key in ("code", "type", "message")
            if str(error.get(key) or "").strip()
        )
        if detail:
            return detail[:2000]
    return ""


def _find_existing_whatsapp_message(db: Session, company_id: int, external_id: str) -> InboundMessage | None:
    channel = db.scalar(
        select(InputChannel).where(
            InputChannel.company_id == company_id,
            InputChannel.key == WHATSAPP_CHANNEL_KEY,
        )
    )
    if not channel:
        return None
    return db.scalar(
        select(InboundMessage).where(
            InboundMessage.company_id == company_id,
            InboundMessage.channel_id == channel.id,
            InboundMessage.provider == WHATSAPP_PROVIDER,
            InboundMessage.source_external_id == external_id,
        )
    )


def embedded_signup_public_config(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    return {
        "configured": settings.meta_whatsapp_embedded_signup_ready,
        "app_id": settings.meta_app_id,
        "config_id": settings.meta_embedded_signup_config_id,
        "graph_api_version": settings.meta_graph_api_version,
        "embedded_signup_version": settings.meta_embedded_signup_version,
        "feature_type": WHATSAPP_COEXISTENCE_FEATURE_TYPE,
        "missing": settings.meta_whatsapp_embedded_signup_missing_configuration,
    }


def whatsapp_webhook_url(company_slug: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return f"{settings.app_url.rstrip('/')}/webhooks/whatsapp/{company_slug}"


def _validate_meta_id(value: str, label: str, *, required: bool = True) -> str:
    normalized = str(value or "").strip()
    if not normalized and not required:
        return ""
    if not META_ID_PATTERN.fullmatch(normalized):
        raise WhatsAppEmbeddedSignupError(f"Meta no devolvió un {label} válido.", error_type="invalid_signup_payload")
    return normalized


def _meta_error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:500]
    return fallback


async def _meta_request(
    client: httpx.AsyncClient,
    settings: Settings,
    method: str,
    path: str,
    *,
    access_token: str | None = None,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    data: dict[str, str] | None = None,
    files: Any = None,
) -> dict[str, Any]:
    url = f"https://graph.facebook.com/{settings.meta_graph_api_version}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        request_kwargs: dict[str, Any] = {"headers": headers, "params": params}
        if json_body is not None:
            request_kwargs["json"] = json_body
        if data is not None:
            request_kwargs["data"] = data
        if files is not None:
            request_kwargs["files"] = files
        response = await client.request(method, url, **request_kwargs)
    except httpx.TimeoutException as exc:
        raise WhatsAppEmbeddedSignupError("Meta no respondió a tiempo. Vuelve a intentarlo.", error_type="timeout") from exc
    except httpx.HTTPError as exc:
        raise WhatsAppEmbeddedSignupError("No se pudo conectar con Meta.", error_type="connection_failed") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise WhatsAppEmbeddedSignupError("Meta devolvió una respuesta no válida.", error_type="invalid_response") from exc
    if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
        detail = _meta_error_message(payload, "Meta rechazó la operación.")
        raise WhatsAppEmbeddedSignupError(
            f"Meta rechazó {method.upper()} /{path.lstrip('/')}: {detail}",
            error_type="rate_limited" if response.status_code == 429 else "meta_api_unavailable" if response.status_code >= 500 else "meta_api_error",
        )
    return payload if isinstance(payload, dict) else {}


def _meta_media_id(message: InboundMessage, attachment: MessageAttachment) -> str:
    try:
        raw_payload = json.loads(message.raw_payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    message_payload = raw_payload.get("payload") if isinstance(raw_payload, dict) else None
    if not isinstance(message_payload, dict):
        return ""
    media_payload = message_payload.get(message.content_type or "")
    if isinstance(media_payload, dict):
        return str(media_payload.get("id") or "").strip()
    return ""


def _safe_media_filename(filename: str | None, media_id: str) -> str:
    candidate = Path(str(filename or media_id or "whatsapp-attachment")).name
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate).strip("._")
    return (candidate or media_id or "whatsapp-attachment")[:200]


def _is_supported_media(filename: str | None, content_type: str | None, *, is_audio: bool = False) -> bool:
    normalized_type = str(content_type or "").strip().lower().split(";", 1)[0]
    extension = Path(str(filename or "")).suffix.lower()
    if is_audio:
        return normalized_type in WHATSAPP_SUPPORTED_AUDIO_MIME_TYPES or extension in WHATSAPP_SUPPORTED_AUDIO_EXTENSIONS
    return normalized_type in WHATSAPP_SUPPORTED_DOCUMENT_MIME_TYPES or extension in WHATSAPP_SUPPORTED_DOCUMENT_EXTENSIONS


def _is_allowed_media_host(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and (
        host == "facebook.com"
        or host.endswith(".facebook.com")
        or host == "fbsbx.com"
        or host.endswith(".fbsbx.com")
        or host == "fbcdn.net"
        or host.endswith(".fbcdn.net")
    )


async def _download_meta_binary(
    client: httpx.AsyncClient,
    *,
    url: str,
    access_token: str,
    max_bytes: int,
) -> bytes:
    if not _is_allowed_media_host(url):
        raise WhatsAppEmbeddedSignupError("Meta devolviÃ³ una URL de media no segura.", error_type="invalid_media_url")
    try:
        async with client.stream("GET", url, headers={"Authorization": f"Bearer {access_token}"}) as response:
            if response.status_code >= 400:
                raise WhatsAppEmbeddedSignupError("Meta rechazÃ³ la descarga de la media.", error_type="media_download_failed")
            content_length = response.headers.get("content-length")
            try:
                declared_size = int(content_length or 0)
            except ValueError:
                declared_size = 0
            if declared_size > max_bytes:
                raise WhatsAppEmbeddedSignupError("El adjunto supera el tamaÃ±o mÃ¡ximo permitido.", error_type="media_too_large")

            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > max_bytes:
                    raise WhatsAppEmbeddedSignupError("El adjunto supera el tamaÃ±o mÃ¡ximo permitido.", error_type="media_too_large")
                content.extend(chunk)
            return bytes(content)
    except httpx.TimeoutException as exc:
        raise WhatsAppEmbeddedSignupError("Meta no respondiÃ³ a tiempo al descargar la media.", error_type="timeout") from exc
    except httpx.HTTPError as exc:
        raise WhatsAppEmbeddedSignupError("No se pudo descargar la media desde Meta.", error_type="media_download_failed") from exc


def _persist_media_failure(attachment: MessageAttachment, message: str) -> None:
    attachment.extraction_status = "extraction_error"
    attachment.extraction_error = message[:1000]


def _persist_storage_failure(attachment: MessageAttachment, message: str) -> None:
    attachment.extraction_status = "storage_error"
    attachment.extraction_error = message[:1000]


def _extract_persisted_attachment(attachment: MessageAttachment, content: bytes) -> str:
    result = extract_attachment_text(
        content,
        filename=attachment.filename,
        content_type=attachment.content_type,
    )
    attachment.extracted_text = result.text
    attachment.extraction_status = result.status
    attachment.extraction_error = result.error
    return result.status


def _whatsapp_media_ready_for_processing(message: InboundMessage) -> bool:
    pending_statuses = {
        "pending",
        "downloaded",
        "transcription_pending",
        "extraction_error",
        "storage_error",
        "unsupported",
    }
    return all((attachment.extraction_status or "pending") not in pending_statuses for attachment in message.attachments or [])


async def download_whatsapp_media(
    db: Session,
    *,
    company_id: int,
    inbound_message_id: int,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    message = db.get(InboundMessage, inbound_message_id)
    if not message or message.company_id != company_id:
        raise ValueError("WhatsApp message not found for tenant.")
    config = whatsapp_config(db, company_id)
    if not config.access_token or not config.phone_number_id:
        attachments = list(message.attachments or [])
        for attachment in attachments:
            _persist_media_failure(attachment, "WhatsApp no tiene credenciales configuradas para descargar media.")
        db.commit()
        return {
            "ok": True,
            "downloaded": 0,
            "skipped": 0,
            "failed": len(attachments),
            "ready_for_processing": _whatsapp_media_ready_for_processing(message),
        }
    attachments = list(message.attachments or [])
    if not attachments:
        return {"ok": True, "downloaded": 0, "skipped": 0, "failed": 0, "ready_for_processing": True}
    owns_client = client is None
    graph_client = client or httpx.AsyncClient(timeout=get_settings().meta_request_timeout_seconds)
    downloaded = 0
    skipped = 0
    failed = 0
    retryable_failure = False
    try:
        for attachment in attachments:
            if not _is_supported_media(attachment.filename, attachment.content_type, is_audio=bool(attachment.is_audio)):
                attachment.extraction_status = "unsupported"
                attachment.extraction_error = "Tipo de adjunto no soportado por la integración de WhatsApp."
                skipped += 1
                db.commit()
                continue
            if attachment.storage_path and attachment.extraction_status in {"extracted", "no_text_found", "transcription_pending"}:
                skipped += 1
                db.commit()
                continue

            if attachment.storage_path:
                try:
                    stored_content = read_attachment(attachment.storage_path)
                except Exception:  # noqa: BLE001
                    stored_content = None
                if stored_content is not None:
                    status = _extract_persisted_attachment(attachment, stored_content)
                    skipped += 1
                    if status == "extraction_error":
                        failed += 1
                    db.commit()
                    continue
            media_id = _meta_media_id(message, attachment)
            if not media_id:
                _persist_media_failure(attachment, "Meta no proporcionÃ³ un identificador de media.")
                failed += 1
                db.commit()
                continue
            try:
                media_info = await _meta_request(
                    graph_client,
                    get_settings(),
                    "GET",
                    media_id,
                    access_token=config.access_token,
                    params={"fields": "url,mime_type,file_size"},
                )
                remote_content_type = str(media_info.get("mime_type") or attachment.content_type or "").strip().lower()
                normalized_remote_type = remote_content_type.split(";", 1)[0]
                remote_type_is_generic = normalized_remote_type in {"", "application/octet-stream"}
                if not remote_type_is_generic and not _is_supported_media(
                    None,
                    normalized_remote_type,
                    is_audio=bool(attachment.is_audio),
                ):
                    raise WhatsAppEmbeddedSignupError(
                        "Meta devolvió un tipo de media no soportado.",
                        error_type="unsupported_media",
                    )
                media_url = str(media_info.get("url") or "").strip()
                if not media_url:
                    raise WhatsAppEmbeddedSignupError("Meta no devolviÃ³ la URL de la media.", error_type="media_download_failed")
                declared_size = _as_int(str(media_info.get("file_size") or ""), 0)
                if declared_size > config.max_attachment_bytes:
                    raise WhatsAppEmbeddedSignupError("El adjunto supera el tamaÃ±o mÃ¡ximo permitido.", error_type="media_too_large")
                content = await _download_meta_binary(
                    graph_client,
                    url=media_url,
                    access_token=config.access_token,
                    max_bytes=config.max_attachment_bytes,
                )
            except WhatsAppEmbeddedSignupError as exc:
                if exc.retryable:
                    raise
                _persist_media_failure(attachment, str(exc))
                failed += 1
                db.commit()
                continue
            filename = _safe_media_filename(attachment.filename, media_id)
            content_type = str(media_info.get("mime_type") or attachment.content_type or "application/octet-stream")[:120]
            try:
                storage_path = save_attachment(
                    filename=f"whatsapp-{company_id}-{message.id}-{filename}",
                    payload=content,
                    content_type=content_type,
                )
            except Exception as exc:  # noqa: BLE001
                _persist_storage_failure(attachment, f"No se pudo guardar el adjunto de WhatsApp: {exc}")
                failed += 1
                retryable_failure = True
                db.commit()
                continue
            attachment.filename = filename
            attachment.content_type = content_type
            attachment.size_bytes = len(content)
            attachment.storage_path = storage_path
            status = _extract_persisted_attachment(attachment, content)
            if status == "extraction_error":
                failed += 1
            downloaded += 1
            # Keep completed attachments durable before the next Meta request so a retry
            # resumes from the first incomplete attachment instead of repeating work.
            db.commit()
    finally:
        if owns_client:
            await graph_client.aclose()
    return {
        "ok": not retryable_failure,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "retryable": retryable_failure,
        "ready_for_processing": _whatsapp_media_ready_for_processing(message),
    }


def _prepare_whatsapp_text_send(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    body: str,
    template_name: str | None = None,
) -> tuple[WhatsAppTenantConfig, str, str, str]:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.company_id != company_id:
        raise ValueError("Conversation not found for tenant.")
    clean_body = str(body or "").strip()
    clean_template_name = str(template_name or "").strip()
    if clean_template_name and not re.fullmatch(r"[a-z0-9_]+", clean_template_name):
        raise WhatsAppEmbeddedSignupError("El nombre de la plantilla de WhatsApp no es valido.", error_type="invalid_message")
    if not clean_template_name and (not clean_body or len(clean_body) > 4096):
        raise WhatsAppEmbeddedSignupError("El mensaje debe tener entre 1 y 4096 caracteres.", error_type="invalid_message")
    config = whatsapp_config(db, company_id)
    if not whatsapp_outbound_is_ready(db, company_id, config=config):
        raise WhatsAppEmbeddedSignupError("WhatsApp no está configurado para enviar mensajes.", error_type="server_not_configured")
    recipient = _whatsapp_reply_recipient(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        response_window_minutes=config.response_window_minutes,
        enforce_window=not bool(clean_template_name),
    )
    return config, recipient, clean_body, clean_template_name


async def send_whatsapp_text(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    body: str,
    client: httpx.AsyncClient | None = None,
    template_name: str | None = None,
    template_language: str | None = None,
    template_components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    config, recipient, body, clean_template_name = _prepare_whatsapp_text_send(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        body=body,
        template_name=template_name,
    )
    owns_client = client is None
    graph_client = client or httpx.AsyncClient(timeout=get_settings().meta_request_timeout_seconds)
    try:
        response = await _meta_request(
            graph_client,
            get_settings(),
            "POST",
            f"{config.phone_number_id}/messages",
            access_token=config.access_token,
            json_body={
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "template" if clean_template_name else "text",
                **(
                    {
                        "template": {
                            "name": clean_template_name,
                            "language": {"code": template_language or config.default_language or "es"},
                            **({"components": template_components} if template_components else {}),
                        }
                    }
                    if clean_template_name
                    else {"text": {"preview_url": False, "body": body}}
                ),
            },
        )
    finally:
        if owns_client:
            await graph_client.aclose()
    messages = response.get("messages") if isinstance(response.get("messages"), list) else []
    provider_message_id = str(messages[0].get("id") or "").strip() if messages and isinstance(messages[0], dict) else ""
    if not provider_message_id:
        raise WhatsAppEmbeddedSignupError("Meta aceptó la petición pero no devolvió el identificador del mensaje.", error_type="invalid_response")
    return {"provider_message_id": provider_message_id, "recipient": recipient}


def _whatsapp_reply_recipient(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    response_window_minutes: int,
    enforce_window: bool = True,
) -> str:
    latest_inbound = db.scalar(
        select(InboundMessage)
        .where(
            InboundMessage.company_id == company_id,
            InboundMessage.conversation_id == conversation_id,
            InboundMessage.direction == "inbound",
        )
        .order_by(InboundMessage.received_at.desc(), InboundMessage.id.desc())
    )
    recipient = str(latest_inbound.sender or "").strip() if latest_inbound else ""
    if not recipient:
        raise WhatsAppEmbeddedSignupError("No se encontró el número del cliente para responder.", error_type="recipient_not_found")
    if enforce_window and latest_inbound and latest_inbound.received_at:
        received_at = latest_inbound.received_at
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - received_at).total_seconds()
        if age_seconds > max(response_window_minutes, 1) * 60:
            raise WhatsAppEmbeddedSignupError("La ventana de respuesta de WhatsApp ha caducado; usa una plantilla aprobada.", error_type="response_window_expired")
    return recipient


def _prepare_whatsapp_media_send(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    content: bytes,
    filename: str,
    content_type: str,
    is_audio: bool = False,
) -> tuple[WhatsAppTenantConfig, str]:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.company_id != company_id:
        raise ValueError("Conversation not found for tenant.")
    config = whatsapp_config(db, company_id)
    if not whatsapp_outbound_is_ready(db, company_id, config=config):
        raise WhatsAppEmbeddedSignupError("WhatsApp no está configurado para enviar archivos.", error_type="server_not_configured")
    if not content or len(content) > config.max_attachment_bytes:
        raise WhatsAppEmbeddedSignupError("El adjunto supera el tamaño máximo permitido.", error_type="media_too_large")
    if not _is_supported_media(filename, content_type, is_audio=is_audio):
        raise WhatsAppEmbeddedSignupError("Tipo de adjunto no soportado por WhatsApp.", error_type="unsupported_media")
    recipient = _whatsapp_reply_recipient(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        response_window_minutes=config.response_window_minutes,
    )
    return config, recipient


async def send_whatsapp_media(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    content: bytes,
    filename: str,
    content_type: str,
    is_audio: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    config, recipient = _prepare_whatsapp_media_send(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        content=content,
        filename=filename,
        content_type=content_type,
        is_audio=is_audio,
    )
    owns_client = client is None
    graph_client = client or httpx.AsyncClient(timeout=get_settings().meta_request_timeout_seconds)
    try:
        media_response = await _meta_request(
            graph_client,
            get_settings(),
            "POST",
            f"{config.phone_number_id}/media",
            access_token=config.access_token,
            data={"messaging_product": "whatsapp"},
            files={"file": (filename, content, content_type)},
        )
        media_id = str(media_response.get("id") or "").strip()
        if not media_id:
            raise WhatsAppEmbeddedSignupError("Meta no devolvió el identificador del archivo.", error_type="invalid_response")
        message_type = "audio" if is_audio else "document"
        media_body: dict[str, str] = {"id": media_id}
        if not is_audio:
            media_body["filename"] = filename
        response = await _meta_request(
            graph_client,
            get_settings(),
            "POST",
            f"{config.phone_number_id}/messages",
            access_token=config.access_token,
            json_body={
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": message_type,
                message_type: media_body,
            },
        )
    finally:
        if owns_client:
            await graph_client.aclose()
    messages = response.get("messages") if isinstance(response.get("messages"), list) else []
    provider_message_id = str(messages[0].get("id") or "").strip() if messages and isinstance(messages[0], dict) else ""
    if not provider_message_id:
        raise WhatsAppEmbeddedSignupError("Meta no devolvió el identificador del mensaje.", error_type="invalid_response")
    return {"provider_message_id": provider_message_id, "recipient": recipient}


def _upsert_whatsapp_setting(
    db: Session,
    *,
    company_id: int,
    channel_id: int,
    key: str,
    value: str,
    is_secret: bool = False,
) -> None:
    setting = db.scalar(
        select(ChannelSetting).where(
            ChannelSetting.company_id == company_id,
            ChannelSetting.channel_id == channel_id,
            ChannelSetting.key == key,
        )
    )
    if not setting:
        setting = ChannelSetting(company_id=company_id, channel_id=channel_id, key=key)
        db.add(setting)
    setting.value = encrypt_secret(value) if is_secret else value
    setting.value_type = "secret" if is_secret else "string"
    setting.is_secret = is_secret
    setting.updated_at = datetime.now(timezone.utc)


def _store_embedded_signup_state(
    db: Session,
    *,
    company_id: int,
    business_account_id: str,
    phone_number_id: str,
    business_id: str,
    access_token: str,
    verify_token: str,
    display_phone_number: str,
    verified_name: str,
    waba_name: str,
    onboarding_mode: str,
    is_on_biz_app: bool,
    account_mode: str,
    phone_status: str,
    code_verification_status: str,
    connection_status: str,
    webhook_enabled: bool,
    last_error: str = "",
) -> InputChannel:
    channel = get_or_create_whatsapp_channel(db, company_id)
    channel.is_active = True
    channel.updated_at = datetime.now(timezone.utc)
    values = {
        "enabled": "true",
        "provider": WHATSAPP_PROVIDER,
        "business_account_id": business_account_id,
        "phone_number_id": phone_number_id,
        "business_id": business_id,
        "display_phone_number": display_phone_number,
        "verified_name": verified_name,
        "waba_name": waba_name,
        "onboarding_mode": onboarding_mode,
        "is_on_biz_app": "true" if is_on_biz_app else "false",
        "account_mode": account_mode,
        "phone_status": phone_status,
        "code_verification_status": code_verification_status,
        "connection_status": connection_status,
        "connected_at": datetime.now(timezone.utc).isoformat() if connection_status == "connected" else "",
        "webhook_enabled": "true" if webhook_enabled else "false",
        "bot_enabled": "true",
        "last_error": last_error[:500],
    }
    for key, value in values.items():
        _upsert_whatsapp_setting(db, company_id=company_id, channel_id=channel.id, key=key, value=value)
    _upsert_whatsapp_setting(db, company_id=company_id, channel_id=channel.id, key="access_token", value=access_token, is_secret=True)
    _upsert_whatsapp_setting(db, company_id=company_id, channel_id=channel.id, key="verify_token", value=verify_token, is_secret=True)
    db.commit()
    return channel


async def complete_embedded_signup(
    db: Session,
    *,
    company_id: int,
    company_slug: str,
    code: str,
    business_account_id: str,
    phone_number_id: str = "",
    business_id: str = "",
    onboarding_mode: str = WHATSAPP_ONBOARDING_CLOUD_API,
    client: httpx.AsyncClient | None = None,
) -> WhatsAppEmbeddedSignupResult:
    settings = get_settings()
    if not settings.meta_whatsapp_embedded_signup_ready:
        raise WhatsAppEmbeddedSignupError("Embedded Signup no está configurado en el servidor.", error_type="server_not_configured")
    code = str(code or "").strip()
    if not code or len(code) > 2048:
        raise WhatsAppEmbeddedSignupError("Meta no devolvió un código de autorización válido.", error_type="invalid_signup_payload")
    onboarding_mode = str(onboarding_mode or "").strip().lower()
    if onboarding_mode not in {WHATSAPP_ONBOARDING_CLOUD_API, WHATSAPP_ONBOARDING_COEXISTENCE}:
        raise WhatsAppEmbeddedSignupError("El tipo de alta de WhatsApp no es válido.", error_type="invalid_signup_payload")
    business_account_id = _validate_meta_id(business_account_id, "WABA ID")
    phone_number_id = _validate_meta_id(
        phone_number_id,
        "Phone Number ID",
        required=onboarding_mode != WHATSAPP_ONBOARDING_COEXISTENCE,
    )
    business_id = _validate_meta_id(business_id, "Business ID", required=False)
    callback_url = whatsapp_webhook_url(company_slug, settings)
    verify_token = secrets.token_urlsafe(32)
    owns_client = client is None
    graph_client = client or httpx.AsyncClient(timeout=settings.meta_request_timeout_seconds)
    try:
        token_params = {
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "code": code,
        }
        if settings.meta_oauth_redirect_uri:
            token_params["redirect_uri"] = settings.meta_oauth_redirect_uri
        token_payload = await _meta_request(
            graph_client,
            settings,
            "GET",
            "oauth/access_token",
            params=token_params,
        )
        access_token = str(token_payload.get("access_token") or "").strip()
        if not access_token:
            raise WhatsAppEmbeddedSignupError("Meta no devolvió un token de acceso.", error_type="token_exchange_failed")

        waba = await _meta_request(
            graph_client,
            settings,
            "GET",
            business_account_id,
            access_token=access_token,
            params={"fields": "id,name,business_verification_status"},
        )
        if str(waba.get("id") or "") != business_account_id:
            raise WhatsAppEmbeddedSignupError("El WABA devuelto por Meta no coincide con el autorizado.", error_type="asset_mismatch")
        phone_payload = await _meta_request(
            graph_client,
            settings,
            "GET",
            f"{business_account_id}/phone_numbers",
            access_token=access_token,
            params={"fields": "id,display_phone_number,verified_name,quality_rating,code_verification_status"},
        )
        phones = phone_payload.get("data") if isinstance(phone_payload.get("data"), list) else []
        phone = next((item for item in phones if str(item.get("id") or "") == phone_number_id), None) if phone_number_id else None
        if phone_number_id and not phone:
            raise WhatsAppEmbeddedSignupError(
                "El teléfono seleccionado no pertenece al WABA autorizado.",
                error_type="asset_mismatch",
            )
        phone_details: dict[str, Any] = {}
        if onboarding_mode == WHATSAPP_ONBOARDING_COEXISTENCE:
            candidates = [phone] if phone else [item for item in phones if isinstance(item, dict)]
            coexistence_candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for candidate in candidates:
                candidate_id = str(candidate.get("id") or "")
                if not META_ID_PATTERN.fullmatch(candidate_id):
                    continue
                details = await _meta_request(
                    graph_client,
                    settings,
                    "GET",
                    candidate_id,
                    access_token=access_token,
                    params={
                        "fields": "status,account_mode,is_on_biz_app,display_phone_number,verified_name,code_verification_status"
                    },
                )
                if details.get("is_on_biz_app") is True:
                    coexistence_candidates.append((candidate, details))
            if len(coexistence_candidates) != 1:
                raise WhatsAppEmbeddedSignupError(
                    "No se pudo identificar de forma segura el número de WhatsApp Business App autorizado.",
                    error_type="asset_mismatch",
                )
            phone, phone_details = coexistence_candidates[0]
            phone_number_id = str(phone.get("id") or "")
        elif phone:
            phone_details = await _meta_request(
                graph_client,
                settings,
                "GET",
                phone_number_id,
                access_token=access_token,
                params={
                    "fields": "status,platform_type"
                },
            )
        if not phone:
            raise WhatsAppEmbeddedSignupError("El teléfono seleccionado no pertenece al WABA autorizado.", error_type="asset_mismatch")

        phone = {**phone, **phone_details}
        is_on_biz_app = onboarding_mode == WHATSAPP_ONBOARDING_COEXISTENCE or phone.get("is_on_biz_app") is True

        state_payload = {
            "company_id": company_id,
            "business_account_id": business_account_id,
            "phone_number_id": phone_number_id,
            "business_id": business_id,
            "access_token": access_token,
            "verify_token": verify_token,
            "display_phone_number": str(phone.get("display_phone_number") or ""),
            "verified_name": str(phone.get("verified_name") or ""),
            "waba_name": str(waba.get("name") or ""),
            "onboarding_mode": onboarding_mode,
            "is_on_biz_app": is_on_biz_app,
            "account_mode": str(phone.get("account_mode") or ""),
            "phone_status": str(phone.get("status") or ""),
            "code_verification_status": str(phone.get("code_verification_status") or ""),
        }
        _store_embedded_signup_state(db, **state_payload, connection_status="provisioning", webhook_enabled=False)
        try:
            phone_status = str(phone.get("status") or "").strip().upper()
            already_registered = phone_status in {"CONNECTED", "ACTIVE"}
            if onboarding_mode == WHATSAPP_ONBOARDING_CLOUD_API and not already_registered:
                if not settings.meta_whatsapp_registration_pin:
                    raise WhatsAppEmbeddedSignupError(
                        "Falta META_WHATSAPP_REGISTRATION_PIN para registrar un número nuevo de Cloud API.",
                        error_type="server_not_configured",
                    )
                await _meta_request(
                    graph_client,
                    settings,
                    "POST",
                    f"{phone_number_id}/register",
                    access_token=access_token,
                    json_body={"messaging_product": "whatsapp", "pin": settings.meta_whatsapp_registration_pin},
                )
            await _meta_request(
                graph_client,
                settings,
                "POST",
                f"{business_account_id}/subscribed_apps",
                access_token=access_token,
            )
            await _meta_request(
                graph_client,
                settings,
                "POST",
                f"{business_account_id}/subscribed_apps",
                access_token=access_token,
                json_body={"override_callback_uri": callback_url, "verify_token": verify_token},
            )
        except WhatsAppEmbeddedSignupError as exc:
            _store_embedded_signup_state(
                db,
                **state_payload,
                connection_status="error",
                webhook_enabled=False,
                last_error=str(exc),
            )
            raise

        _store_embedded_signup_state(db, **state_payload, connection_status="connected", webhook_enabled=True)
        return WhatsAppEmbeddedSignupResult(
            business_account_id=business_account_id,
            phone_number_id=phone_number_id,
            business_id=business_id,
            display_phone_number=str(phone.get("display_phone_number") or ""),
            verified_name=str(phone.get("verified_name") or ""),
            webhook_url=callback_url,
            onboarding_mode=onboarding_mode,
            is_on_biz_app=is_on_biz_app,
        )
    finally:
        if owns_client:
            await graph_client.aclose()


def resolve_company_from_slug(master_db: Session, company_slug: str) -> tuple[MasterCompany | None, MasterTenantDatabase | None]:
    company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == company_slug))
    if not company:
        return None, None
    tenant_db = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id, MasterTenantDatabase.is_active.is_(True)))
    return company, tenant_db


def resolve_company_from_whatsapp_identifiers(
    master_db: Session,
    *,
    business_account_id: str | None,
    phone_number_id: str | None,
) -> tuple[MasterCompany | None, MasterTenantDatabase | None]:
    from app.tenancy.database import tenant_db_session

    wanted_waba = str(business_account_id or "").strip()
    wanted_phone = str(phone_number_id or "").strip()
    if not wanted_waba and not wanted_phone:
        return None, None
    tenants = master_db.scalars(select(MasterTenantDatabase).where(MasterTenantDatabase.is_active.is_(True))).all()
    matches: list[tuple[MasterCompany, MasterTenantDatabase]] = []
    for tenant in tenants:
        tenant_db = tenant_db_session(tenant.get_database_url())()
        try:
            channel = tenant_db.scalar(
                select(InputChannel).where(
                    InputChannel.company_id == tenant.company_id,
                    InputChannel.key == WHATSAPP_CHANNEL_KEY,
                )
            )
            if not channel:
                continue
            rows = tenant_db.scalars(
                select(ChannelSetting).where(
                    ChannelSetting.company_id == tenant.company_id,
                    ChannelSetting.channel_id == channel.id,
                    ChannelSetting.key.in_(("business_account_id", "phone_number_id")),
                )
            ).all()
            values = {row.key: str(row.value or "").strip() for row in rows}
            phone_matches = bool(wanted_phone and values.get("phone_number_id") == wanted_phone)
            waba_matches = bool(wanted_waba and values.get("business_account_id") == wanted_waba)
            identifiers_match = (
                phone_matches and waba_matches
                if wanted_phone and wanted_waba
                else phone_matches or waba_matches
            )
            if identifiers_match:
                company = master_db.get(MasterCompany, tenant.company_id)
                if company:
                    matches.append((company, tenant))
        finally:
            tenant_db.close()
    return matches[0] if len(matches) == 1 else (None, None)


def verify_webhook_token(config: WhatsAppTenantConfig, verify_token: str | None) -> bool:
    return bool(config.verify_token and verify_token and hmac.compare_digest(config.verify_token, verify_token))


def verify_signature(app_secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)


def _event_timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(str(value)), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _normalize_whatsapp_address(value: Any) -> str:
    """Canonicalize Meta phone addresses without changing other identifiers."""
    normalized = str(value or "").strip()
    if re.fullmatch(r"\d{6,15}", normalized):
        return f"+{normalized}"
    return normalized


def _message_event(
    *,
    kind: str,
    webhook_field: str,
    message: dict[str, Any],
    metadata: dict[str, Any],
    business_account_id: str | None,
    phone_number_id: str | None,
    display_phone_number: str | None,
    contacts: list[dict[str, Any]],
    direction: str,
    thread_id: str | None = None,
    sync_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fallback_contact = _normalize_whatsapp_address(contacts[0].get("wa_id")) if contacts else None
    sender = _normalize_whatsapp_address(message.get("from") or (display_phone_number if direction == "outbound" else fallback_contact))
    remote_recipient = _normalize_whatsapp_address(message.get("to") or thread_id or fallback_contact)
    recipients = (
        [remote_recipient]
        if direction == "outbound" and remote_recipient
        else [_normalize_whatsapp_address(display_phone_number)]
        if display_phone_number
        else []
    )
    external_thread_id = remote_recipient if direction == "outbound" else (thread_id or sender)
    history_context = message.get("history_context") if isinstance(message.get("history_context"), dict) else {}
    event_metadata: dict[str, Any] = {
        "payload": message,
        "metadata": metadata,
        "webhook_field": webhook_field,
    }
    if sync_metadata:
        event_metadata["sync"] = sync_metadata
    return {
        "kind": kind,
        "direction": direction,
        "external_id": message.get("id"),
        "external_thread_id": external_thread_id or message.get("id"),
        "sender": sender,
        "recipients": recipients,
        "phone_number_id": phone_number_id,
        "business_account_id": business_account_id,
        "text_content": _extract_message_text(message),
        "message_type": message.get("type"),
        "message_status": str(history_context.get("status") or "").strip().lower(),
        "occurred_at": _event_timestamp(message.get("timestamp")),
        "metadata": event_metadata,
        "attachments": _message_attachments(message),
    }


def parse_payload_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) if isinstance(payload.get("entry"), list) else []:
        entry_waba_id = entry.get("id") if isinstance(entry, dict) else None
        changes = entry.get("changes", []) if isinstance(entry, dict) else []
        for change in changes:
            value = change.get("value", {}) if isinstance(change, dict) else {}
            if not isinstance(value, dict):
                continue
            webhook_field = str(change.get("field") or "messages").strip().lower()
            metadata = value.get("metadata", {}) if isinstance(value, dict) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            phone_number_id = metadata.get("phone_number_id")
            business_account_id = entry_waba_id or metadata.get("business_account_id")
            display_phone_number = metadata.get("display_phone_number")
            contacts = [item for item in value.get("contacts", []) if isinstance(item, dict)] if isinstance(value.get("contacts"), list) else []
            statuses = value.get("statuses", []) if isinstance(value.get("statuses"), list) else []
            messages = value.get("messages", []) if isinstance(value.get("messages"), list) else []
            for message in messages:
                if isinstance(message, dict) and str(message.get("id") or "").strip():
                    events.append(
                        _message_event(
                            kind="message",
                            webhook_field=webhook_field,
                            message=message,
                            metadata=metadata,
                            business_account_id=business_account_id,
                            phone_number_id=phone_number_id,
                            display_phone_number=display_phone_number,
                            contacts=contacts,
                            direction="inbound",
                        )
                    )
            for status in statuses:
                if not isinstance(status, dict) or not str(status.get("id") or "").strip():
                    continue
                events.append(
                    {
                        "kind": "status",
                        "external_id": status.get("id"),
                        "external_thread_id": status.get("conversation", {}).get("id") if isinstance(status.get("conversation"), dict) else status.get("id"),
                        "sender": None,
                        "recipients": [_normalize_whatsapp_address(status.get("recipient_id"))]
                        if status.get("recipient_id")
                        else ([_normalize_whatsapp_address(display_phone_number)] if display_phone_number else []),
                        "phone_number_id": phone_number_id,
                        "business_account_id": business_account_id,
                        "text_content": None,
                        "message_type": status.get("status"),
                        "occurred_at": _event_timestamp(status.get("timestamp")),
                        "metadata": {"payload": status, "metadata": metadata, "webhook_field": webhook_field},
                        "attachments": [],
                    }
                )
            message_echoes = value.get("message_echoes", []) if isinstance(value.get("message_echoes"), list) else []
            for message in message_echoes:
                if isinstance(message, dict) and str(message.get("id") or "").strip():
                    events.append(
                        _message_event(
                            kind="message_echo",
                            webhook_field=webhook_field,
                            message=message,
                            metadata=metadata,
                            business_account_id=business_account_id,
                            phone_number_id=phone_number_id,
                            display_phone_number=display_phone_number,
                            contacts=contacts,
                            direction="outbound",
                        )
                    )
            history_chunks = value.get("history", []) if isinstance(value.get("history"), list) else []
            for history_chunk in history_chunks:
                if not isinstance(history_chunk, dict):
                    continue
                sync_metadata = history_chunk.get("metadata") if isinstance(history_chunk.get("metadata"), dict) else {}
                errors = history_chunk.get("errors") if isinstance(history_chunk.get("errors"), list) else []
                events.append(
                    {
                        "kind": "history_sync",
                        "phone_number_id": phone_number_id,
                        "business_account_id": business_account_id,
                        "metadata": {
                            "payload": {"errors": errors},
                            "metadata": metadata,
                            "sync": sync_metadata,
                            "webhook_field": webhook_field,
                        },
                    }
                )
                threads = history_chunk.get("threads") if isinstance(history_chunk.get("threads"), list) else []
                for thread in threads:
                    if not isinstance(thread, dict):
                        continue
                    thread_id = str(thread.get("id") or "").strip() or None
                    history_messages = thread.get("messages") if isinstance(thread.get("messages"), list) else []
                    for message in history_messages:
                        if not isinstance(message, dict) or not str(message.get("id") or "").strip():
                            continue
                        events.append(
                            _message_event(
                                kind="history_message",
                                webhook_field=webhook_field,
                                message=message,
                                metadata=metadata,
                                business_account_id=business_account_id,
                                phone_number_id=phone_number_id,
                                display_phone_number=display_phone_number,
                                contacts=contacts,
                                direction="outbound" if message.get("to") else "inbound",
                                thread_id=thread_id,
                                sync_metadata=sync_metadata,
                            )
                        )
            state_sync = value.get("state_sync", []) if isinstance(value.get("state_sync"), list) else []
            for state_item in state_sync:
                if not isinstance(state_item, dict):
                    continue
                item_metadata = state_item.get("metadata") if isinstance(state_item.get("metadata"), dict) else {}
                events.append(
                    {
                        "kind": "contact_sync",
                        "phone_number_id": phone_number_id,
                        "business_account_id": business_account_id,
                        "occurred_at": _event_timestamp(item_metadata.get("timestamp")),
                        "metadata": {"payload": state_item, "metadata": metadata, "webhook_field": webhook_field},
                    }
                )
            if webhook_field == "account_update" and value.get("event"):
                events.append(
                    {
                        "kind": "account_update",
                        "phone_number_id": phone_number_id,
                        "business_account_id": business_account_id,
                        "phone_number": value.get("phone_number") or value.get("display_phone_number"),
                        "metadata": {"payload": value, "metadata": metadata, "webhook_field": webhook_field},
                    }
                )
    return events


def persist_event(db: Session, company_id: int, event: dict[str, Any], user=None) -> InboundMessage | None:
    channel = get_or_create_whatsapp_channel(db, company_id)
    metadata = dict(event.get("metadata") or {})
    metadata.setdefault("whatsapp", True)
    event_kind = str(event.get("kind") or "")
    external_id = str(event.get("external_id") or "").strip()
    if event_kind in {"message", "message_echo", "history_message", "status"} and not str(event.get("external_id") or "").strip():
        # Meta IDs are the only stable deduplication key available for these
        # events. Persisting an event without one would create an orphan entry
        # that a repeated webhook could never match safely.
        return None
    if event_kind in {"contact_sync", "history_sync", "account_update"}:
        now = datetime.now(timezone.utc)
        payload = metadata.get("payload") if isinstance(metadata.get("payload"), dict) else {}
        setting_values: dict[str, str] = {}
        if event_kind == "contact_sync":
            setting_values = {
                "last_contact_sync_at": (event.get("occurred_at") or now).isoformat(),
                "last_contact_sync_action": str(payload.get("action") or "sync")[:80],
            }
        elif event_kind == "history_sync":
            errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
            first_error = errors[0] if errors and isinstance(errors[0], dict) else {}
            sync = metadata.get("sync") if isinstance(metadata.get("sync"), dict) else {}
            setting_values = {
                "last_history_sync_at": now.isoformat(),
                "last_history_sync_progress": str(sync.get("progress") or ""),
                "last_history_sync_error": str(first_error.get("message") or first_error.get("title") or "")[:500],
            }
        else:
            account_event = str(payload.get("event") or "").strip().upper()
            setting_values = {
                "last_account_update_at": now.isoformat(),
                "last_account_update_event": account_event[:120],
            }
            if account_event in {"ACCOUNT_OFFBOARDED", "PARTNER_REMOVED"}:
                channel.is_active = False
                setting_values.update({"connection_status": "disconnected", "webhook_enabled": "false"})
            elif account_event == "ACCOUNT_RECONNECTED":
                channel.is_active = True
                setting_values.update({"connection_status": "connected", "webhook_enabled": "true"})
        for key, value in setting_values.items():
            _upsert_whatsapp_setting(db, company_id=company_id, channel_id=channel.id, key=key, value=value)
        db.commit()
        log_action(
            db,
            company_id=company_id,
            user=user,
            action=f"whatsapp.{event_kind}",
            entity_type="input_channel",
            entity_id=channel.id,
            message=f"Evento de coexistencia procesado: {event_kind}",
        )
        return None
    if event_kind == "status":
        message = db.scalar(
            select(InboundMessage).where(
                InboundMessage.company_id == company_id,
                InboundMessage.channel_id == channel.id,
                InboundMessage.provider == WHATSAPP_PROVIDER,
                InboundMessage.source_external_id == external_id,
            )
        ) if external_id else None
        try:
            if not message:
                message = upsert_inbound_message(
                    db,
                    company_id=company_id,
                    channel_key=WHATSAPP_CHANNEL_KEY,
                    provider=WHATSAPP_PROVIDER,
                    external_id=external_id or None,
                    sender=event.get("sender"),
                    recipients=event.get("recipients"),
                    subject="Estado WhatsApp",
                    text_content=event.get("text_content"),
                    external_thread_id=event.get("external_thread_id"),
                    metadata=metadata,
                    content_type="whatsapp_status",
                    direction="outbound",
                    received_at=event.get("occurred_at"),
                )[0]
        except IntegrityError:
            db.rollback()
            existing = _find_existing_whatsapp_message(db, company_id, external_id) if external_id else None
            if existing:
                return existing
            raise
        status_payload = metadata.get("payload") if isinstance(metadata.get("payload"), dict) else {}
        status_value = str(status_payload.get("status") or "sent").lower()
        previous_status = str(message.status or "").strip().lower()
        status_advanced = _should_advance_delivery_status(previous_status, status_value)
        if status_advanced:
            message.status = status_value
            message.processing_step = f"delivery_{status_value}"
            message.last_processed_at = datetime.now(timezone.utc)
        if status_value == "failed" and (status_advanced or previous_status == "failed"):
            status_error = _whatsapp_status_error(status_payload)
            if status_error:
                message.processing_error = status_error
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = _find_existing_whatsapp_message(db, company_id, external_id) if external_id else None
            if existing:
                return existing
            raise
        log_action(db, company_id=company_id, user=user, action="whatsapp.status_received", entity_type="inbound_message", entity_id=message.id, message=f"Estado WhatsApp recibido: {status_value}")
        return message

    existing_message = None
    if external_id:
        existing_message = db.scalar(
            select(InboundMessage).where(
                InboundMessage.company_id == company_id,
                InboundMessage.channel_id == channel.id,
                InboundMessage.provider == WHATSAPP_PROVIDER,
                InboundMessage.source_external_id == external_id,
            )
        )
    try:
        if event_kind in {"message_echo", "history_message"}:
            message, conversation = upsert_inbound_message(
                db,
                company_id=company_id,
                channel_key=WHATSAPP_CHANNEL_KEY,
                provider=WHATSAPP_PROVIDER,
                external_id=event.get("external_id"),
                sender=event.get("sender"),
                recipients=normalize_recipients(event.get("recipients")),
                subject="WhatsApp",
                text_content=event.get("text_content"),
                external_thread_id=event.get("external_thread_id"),
                metadata=metadata,
                content_type=event.get("message_type") or "whatsapp",
                direction=str(event.get("direction") or "inbound"),
                received_at=event.get("occurred_at"),
                sent_at=event.get("occurred_at") if event.get("direction") == "outbound" else None,
                has_attachments=bool(event.get("attachments")),
                has_pdf=any(attachment.get("is_pdf") for attachment in event.get("attachments", [])),
                has_audio=any(attachment.get("is_audio") for attachment in event.get("attachments", [])),
            )
        else:
            normalized = NormalizedMessage(
                company_id=company_id,
                channel_key=WHATSAPP_CHANNEL_KEY,
                provider=WHATSAPP_PROVIDER,
                external_id=event.get("external_id"),
                sender=event.get("sender"),
                recipients=normalize_recipients(event.get("recipients")),
                subject="WhatsApp",
                text_content=event.get("text_content"),
                external_thread_id=event.get("external_thread_id"),
                metadata=metadata,
            )
            message, conversation = persist_normalized_message(
                db,
                normalized,
                content_type=event.get("message_type") or "whatsapp",
                has_attachments=bool(event.get("attachments")),
                has_pdf=any(attachment.get("is_pdf") for attachment in event.get("attachments", [])),
                has_audio=any(attachment.get("is_audio") for attachment in event.get("attachments", [])),
            )
    except IntegrityError:
        db.rollback()
        existing = _find_existing_whatsapp_message(db, company_id, external_id) if external_id else None
        if existing:
            return existing
        raise

    message.channel_id = channel.id
    is_new_message = existing_message is None
    if is_new_message:
        if event_kind == "message_echo":
            message.processing_step = "echoed_from_business_app"
            message.status = event.get("message_status") or "sent"
        elif event_kind == "history_message":
            message.processing_step = "history_synced"
            message.status = event.get("message_status") or "received"
        else:
            message.processing_step = "received_whatsapp"
            message.status = "received"
        message.last_processed_at = datetime.now(timezone.utc)
    elif event_kind in {"message_echo", "history_message"} and not message.order_id:
        desired_status = str(event.get("message_status") or ("sent" if event_kind == "message_echo" else "received")).lower()
        if _should_advance_delivery_status(message.status, desired_status):
            message.status = desired_status
            message.processing_step = "echoed_from_business_app" if event_kind == "message_echo" else "history_synced"
            message.last_processed_at = datetime.now(timezone.utc)
    if existing_message is None:
        for attachment in event.get("attachments", []):
            db.add(
                MessageAttachment(
                    company_id=company_id,
                    inbound_message_id=message.id,
                    filename=attachment.get("filename") or attachment.get("media_id") or "whatsapp-attachment",
                    content_type=attachment.get("content_type"),
                    size_bytes=_as_int(attachment.get("size_bytes"), 0),
                    storage_path=None,
                    extracted_text=None,
                    ocr_text=None,
                    transcription_text=None,
                    is_pdf=bool(attachment.get("is_pdf")),
                    is_image=bool(attachment.get("is_image")),
                    is_audio=bool(attachment.get("is_audio")),
                    extraction_status="pending",
                )
            )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _find_existing_whatsapp_message(db, company_id, external_id) if external_id else None
        if existing:
            return existing
        raise
    action = {
        "message_echo": "whatsapp.message_echoed",
        "history_message": "whatsapp.history_message_synced",
    }.get(event_kind, "whatsapp.message_received")
    log_action(
        db,
        company_id=company_id,
        user=user,
        action=action,
        entity_type="inbound_message",
        entity_id=message.id,
        message=f"Evento WhatsApp procesado: {event.get('external_id')}",
    )
    return message


def enqueue_whatsapp_processing(db: Session, company_id: int, inbound_message_id: int, user_id: int | None = None) -> object:
    return enqueue_job(
        db,
        company_id=company_id,
        job_type="process_inbound_message",
        payload={"inbound_message_id": inbound_message_id, "channel": WHATSAPP_CHANNEL_KEY},
        created_by_user_id=user_id,
    )


def enqueue_whatsapp_media_download(db: Session, company_id: int, inbound_message_id: int, user_id: int | None = None) -> object:
    return enqueue_job(
        db,
        company_id=company_id,
        job_type="download_whatsapp_media",
        payload={"inbound_message_id": inbound_message_id, "channel": WHATSAPP_CHANNEL_KEY},
        created_by_user_id=user_id,
    )


def _whatsapp_message_metadata(message: InboundMessage) -> dict[str, Any]:
    try:
        payload = json.loads(message.raw_payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_idempotency_key(value: str | None) -> str | None:
    key = str(value or "").strip()
    if not key:
        return None
    if len(key) > 200 or any(ord(character) < 32 for character in key):
        raise WhatsAppEmbeddedSignupError("La clave de idempotencia no es valida.", error_type="invalid_message")
    return key


def _find_idempotent_outbound(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    idempotency_key: str | None,
) -> InboundMessage | None:
    if not idempotency_key:
        return None
    return db.scalar(
        select(InboundMessage)
        .where(
            InboundMessage.company_id == company_id,
            InboundMessage.conversation_id == conversation_id,
            InboundMessage.direction == "outbound",
            InboundMessage.source_message_id == idempotency_key,
        )
        .order_by(InboundMessage.id.desc())
    )


def _pending_outbound_external_id(company_id: int, conversation_id: int, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{company_id}:{conversation_id}:{idempotency_key}".encode("utf-8")).hexdigest()[:32]
    return f"{_PENDING_OUTBOUND_EXTERNAL_ID_PREFIX}{digest}"


def _reuse_or_reject_outbound(existing: InboundMessage) -> InboundMessage:
    status = str(existing.status or "").strip().lower()
    if status == "send_unknown":
        raise WhatsAppEmbeddedSignupError(
            "El envío anterior de WhatsApp quedó sin confirmar; no se repetirá automáticamente para evitar duplicados.",
            error_type="send_unknown",
        )
    if status == "sending" or str(existing.source_external_id or "").startswith(_PENDING_OUTBOUND_EXTERNAL_ID_PREFIX):
        raise WhatsAppEmbeddedSignupError(
            "Ya hay un envío de WhatsApp en curso para esta clave de idempotencia.",
            error_type="send_in_progress",
        )
    return existing


def _reserve_manual_response(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    body: str,
    user_id: int | None,
    template_name: str | None,
    idempotency_key: str,
) -> tuple[InboundMessage, bool]:
    existing = _find_idempotent_outbound(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        idempotency_key=idempotency_key,
    )
    if existing:
        return _reuse_or_reject_outbound(existing), False
    try:
        message = record_manual_response(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
            body=body,
            user_id=user_id,
            template_name=template_name,
            external_id=_pending_outbound_external_id(company_id, conversation_id, idempotency_key),
            status="sending",
            processing_step="outbound_sending",
            idempotency_key=idempotency_key,
        )
    except IntegrityError:
        db.rollback()
        existing = _find_idempotent_outbound(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )
        if existing:
            return _reuse_or_reject_outbound(existing), False
        raise
    return message, True


def _mark_outbound_send_unknown(db: Session, message: InboundMessage, *, user_id: int | None, error: Exception) -> None:
    message.status = "send_unknown"
    message.processing_step = "outbound_send_unknown"
    message.processing_error = str(error)[:2000]
    try:
        db.commit()
        audit_user = db.get(User, user_id) if user_id else None
        log_action(
            db,
            company_id=message.company_id,
            user=audit_user,
            action="whatsapp.outbound_send_unknown",
            entity_type="inbound_message",
            entity_id=message.id,
            message="El envío de WhatsApp quedó sin confirmar; se bloqueó el reintento automático.",
        )
    except Exception:  # noqa: BLE001
        db.rollback()


def _last_conversation_order_created_at(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
) -> datetime | None:
    return db.scalar(
        select(Order.created_at)
        .where(
            Order.company_id == company_id,
            Order.conversation_id == conversation_id,
        )
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(1)
    )


def automatic_response_status(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    trigger_message_id: int,
) -> dict[str, Any]:
    config = whatsapp_config(db, company_id)

    if not config.bot_enabled:
        return {
            "allowed": False,
            "reason": "bot_disabled",
            "count": 0,
            "limit": config.max_auto_messages,
        }

    boundary = _last_conversation_order_created_at(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
    )

    query = select(InboundMessage).where(
        InboundMessage.company_id == company_id,
        InboundMessage.conversation_id == conversation_id,
        InboundMessage.direction == "outbound",
    )
    if boundary is not None:
        query = query.where(InboundMessage.received_at > boundary)

    outbound_messages = db.scalars(
        query.order_by(InboundMessage.received_at, InboundMessage.id)
    ).all()

    automatic_messages: list[InboundMessage] = []
    for message in outbound_messages:
        metadata = _whatsapp_message_metadata(message)
        if not metadata.get("auto_response"):
            continue

        automatic_messages.append(message)

        if int(metadata.get("trigger_message_id") or 0) == int(trigger_message_id):
            return {
                "allowed": False,
                "reason": "already_replied",
                "count": len(automatic_messages),
                "limit": config.max_auto_messages,
                "message_id": message.id,
            }

    limit = max(int(config.max_auto_messages or 0), 0)
    if len(automatic_messages) >= limit:
        return {
            "allowed": False,
            "reason": "auto_message_limit",
            "count": len(automatic_messages),
            "limit": limit,
        }

    return {
        "allowed": True,
        "reason": "allowed",
        "count": len(automatic_messages),
        "limit": limit,
    }


def record_automatic_response(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    body: str,
    external_id: str,
    trigger_message_id: int,
    semantic_state: str,
    prompt_execution_id: int | None = None,
    idempotency_key: str | None = None,
    status: str = "accepted",
    processing_step: str = "outbound_auto_accepted",
) -> InboundMessage:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.company_id != company_id:
        raise ValueError("Conversation not found for tenant.")
    idempotency_key = _normalize_idempotency_key(idempotency_key)
    existing = _find_idempotent_outbound(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        idempotency_key=idempotency_key,
    )
    is_pending_existing = bool(
        existing
        and str(existing.source_external_id or "").startswith(_PENDING_OUTBOUND_EXTERNAL_ID_PREFIX)
    )
    if existing and not (is_pending_existing and external_id and external_id != existing.source_external_id):
        return existing
    metadata = {
        "auto_response": True,
        "trigger_message_id": trigger_message_id,
        "semantic_state": semantic_state,
        "prompt_execution_id": prompt_execution_id,
        "idempotency_key": idempotency_key,
    }
    if is_pending_existing:
        message = existing
        message.source_external_id = external_id
        message.original_content = body or message.original_content
        message.raw_payload_json = json.dumps(metadata, ensure_ascii=False)
    else:
        message, _ = upsert_inbound_message(
            db,
            company_id=company_id,
            channel_key=WHATSAPP_CHANNEL_KEY,
            provider=WHATSAPP_PROVIDER,
            external_id=external_id,
            sender=None,
            recipients=[],
            subject="WhatsApp automatic response",
            text_content=body,
            external_thread_id=conversation.external_thread_id or external_id,
            metadata=metadata,
            content_type="whatsapp_text",
            direction="outbound",
            sent_at=datetime.now(timezone.utc),
        )
    message.source_message_id = idempotency_key
    message.status = status
    message.processing_step = processing_step
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()

    log_action(
        db,
        company_id=company_id,
        user=None,
        action="whatsapp.auto_response_accepted" if status == "accepted" else "whatsapp.auto_response_started",
        entity_type="inbound_message",
        entity_id=message.id,
        message=(
            f"Respuesta automática de WhatsApp aceptada por Meta ({semantic_state})"
            if status == "accepted"
            else f"Envío automático de WhatsApp iniciado ({semantic_state})"
        ),
    )
    return message


def _reserve_automatic_response(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    body: str,
    trigger_message_id: int,
    semantic_state: str,
    prompt_execution_id: int | None,
    idempotency_key: str,
) -> tuple[InboundMessage, bool]:
    existing = _find_idempotent_outbound(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        idempotency_key=idempotency_key,
    )
    if existing:
        return _reuse_or_reject_outbound(existing), False
    try:
        message = record_automatic_response(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
            body=body,
            external_id=_pending_outbound_external_id(company_id, conversation_id, idempotency_key),
            trigger_message_id=trigger_message_id,
            semantic_state=semantic_state,
            prompt_execution_id=prompt_execution_id,
            idempotency_key=idempotency_key,
            status="sending",
            processing_step="outbound_auto_sending",
        )
    except IntegrityError:
        db.rollback()
        existing = _find_idempotent_outbound(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )
        if existing:
            return _reuse_or_reject_outbound(existing), False
        raise
    return message, True


async def send_automatic_response(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    trigger_message_id: int,
    body: str,
    semantic_state: str,
    prompt_execution_id: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    status = automatic_response_status(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        trigger_message_id=trigger_message_id,
    )
    if not status["allowed"]:
        return {
            "sent": False,
            "skipped": True,
            **status,
        }
    _prepare_whatsapp_text_send(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        body=body,
        template_name=None,
    )
    idempotency_key = f"auto:{trigger_message_id}"
    try:
        reservation, owns_send = _reserve_automatic_response(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
            body=body,
            trigger_message_id=trigger_message_id,
            semantic_state=semantic_state,
            prompt_execution_id=prompt_execution_id,
            idempotency_key=idempotency_key,
        )
    except WhatsAppEmbeddedSignupError as exc:
        return {
            "sent": False,
            "skipped": True,
            "reason": exc.error_type,
            "count": int(status["count"]),
            "limit": status["limit"],
        }
    if not owns_send:
        return {
            "sent": False,
            "skipped": True,
            "reason": "already_replied",
            "message_id": reservation.id,
            "count": int(status["count"]),
            "limit": status["limit"],
        }
    try:
        result = await send_whatsapp_text(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
            body=body,
            client=client,
        )
        message = record_automatic_response(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
            body=body,
            external_id=result["provider_message_id"],
            trigger_message_id=trigger_message_id,
            semantic_state=semantic_state,
            prompt_execution_id=prompt_execution_id,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:  # noqa: BLE001
        _mark_outbound_send_unknown(db, reservation, user_id=None, error=exc)
        raise

    return {
        "sent": True,
        "skipped": False,
        "reason": "sent",
        "message_id": message.id,
        "provider_message_id": result["provider_message_id"],
        "count": int(status["count"]) + 1,
        "limit": status["limit"],
    }


def record_manual_response(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    body: str,
    user_id: int | None = None,
    template_name: str | None = None,
    external_id: str | None = None,
    status: str = "recorded",
    processing_step: str = "outbound_recorded",
    attachments: list[dict[str, Any]] | None = None,
    idempotency_key: str | None = None,
) -> InboundMessage:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.company_id != company_id:
        raise ValueError("Conversation not found for tenant.")
    idempotency_key = _normalize_idempotency_key(idempotency_key)
    existing = _find_idempotent_outbound(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        idempotency_key=idempotency_key,
    )
    is_pending_existing = bool(
        existing
        and str(existing.source_external_id or "").startswith(_PENDING_OUTBOUND_EXTERNAL_ID_PREFIX)
    )
    if existing and not (is_pending_existing and external_id and external_id != existing.source_external_id):
        return existing
    attachment_payloads = attachments or []
    content_type = "whatsapp_media" if attachment_payloads and not body else "whatsapp_text"
    has_pdf = any(str(item.get("filename") or "").lower().endswith(".pdf") for item in attachment_payloads)
    has_audio = any(bool(item.get("is_audio")) for item in attachment_payloads)
    metadata = {"manual_response": True, "template_name": template_name, "idempotency_key": idempotency_key}
    if is_pending_existing:
        message = existing
        message.source_external_id = external_id
        message.original_content = body or message.original_content
        message.raw_payload_json = json.dumps(metadata, ensure_ascii=False)
        message.content_type = content_type
        message.has_attachments = bool(attachment_payloads) or message.has_attachments
        message.has_pdf = has_pdf or message.has_pdf
        message.has_audio = has_audio or message.has_audio
    else:
        external_id = external_id or f"wa-out-{uuid4().hex}"
        message, _ = upsert_inbound_message(
            db,
            company_id=company_id,
            channel_key=WHATSAPP_CHANNEL_KEY,
            provider=WHATSAPP_PROVIDER,
            external_id=external_id,
            sender=None,
            recipients=[],
            subject="WhatsApp outbound",
            text_content=body or None,
            external_thread_id=conversation.external_thread_id or external_id,
            metadata=metadata,
            content_type=content_type,
            direction="outbound",
            sent_at=datetime.now(timezone.utc),
            has_attachments=bool(attachment_payloads),
            has_pdf=has_pdf,
            has_audio=has_audio,
        )
    message.source_message_id = idempotency_key
    message.status = status
    message.processing_step = processing_step
    for item in attachment_payloads:
        filename = _safe_media_filename(str(item.get("filename") or "whatsapp-attachment"), str(message.id))
        storage_path = None
        extraction_status = "storage_error"
        extraction_error = "No se pudo guardar una copia local del adjunto enviado."
        try:
            storage_path = save_attachment(
                filename=filename,
                payload=bytes(item.get("content") or b""),
                content_type=str(item.get("content_type") or "application/octet-stream"),
            )
            extraction_status = "downloaded"
            extraction_error = None
        except Exception:  # noqa: BLE001
            pass
        content_type = str(item.get("content_type") or "application/octet-stream")
        db.add(
            MessageAttachment(
                company_id=company_id,
                inbound_message_id=message.id,
                filename=filename,
                content_type=content_type[:120],
                size_bytes=len(bytes(item.get("content") or b"")),
                storage_path=storage_path,
                is_pdf=filename.lower().endswith(".pdf") or content_type.lower() == "application/pdf",
                is_audio=bool(item.get("is_audio")) or content_type.lower().startswith("audio/"),
                extraction_status=extraction_status,
                extraction_error=extraction_error,
            )
        )
    conversation.status = "human_owned"
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()
    audit_user = db.get(User, user_id) if user_id else None
    log_action(
        db,
        company_id=company_id,
        user=audit_user,
        action=(
            "whatsapp.outbound_accepted"
            if status == "accepted"
            else "whatsapp.outbound_started"
            if status == "sending"
            else "whatsapp.outbound_recorded"
        ),
        entity_type="inbound_message",
        entity_id=message.id,
        message=(
            "Respuesta manual de WhatsApp aceptada por Meta"
            if status == "accepted"
            else "Envío manual de WhatsApp iniciado"
            if status == "sending"
            else "Respuesta manual de WhatsApp registrada"
        ),
    )
    return message


async def send_manual_response(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    body: str,
    user_id: int | None = None,
    client: httpx.AsyncClient | None = None,
    attachments: list[dict[str, Any]] | None = None,
    idempotency_key: str | None = None,
    template_name: str | None = None,
    template_language: str | None = None,
    template_components: list[dict[str, Any]] | None = None,
) -> InboundMessage:
    clean_body = str(body or "").strip()
    attachment_payloads = attachments or []
    if not clean_body and not attachment_payloads and not template_name:
        raise WhatsAppEmbeddedSignupError("El mensaje debe incluir texto o un archivo.", error_type="invalid_message")
    idempotency_key = _normalize_idempotency_key(idempotency_key)
    text_key = f"{idempotency_key}:text" if idempotency_key and attachment_payloads else idempotency_key
    text_existing = _find_idempotent_outbound(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        idempotency_key=text_key,
    ) if text_key else None
    if (clean_body or template_name) and not text_existing:
        _prepare_whatsapp_text_send(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
            body=clean_body,
            template_name=template_name,
        )
    for index, attachment in enumerate(attachment_payloads):
        media_key = f"{idempotency_key}:media:{index}" if idempotency_key else None
        existing = _find_idempotent_outbound(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
            idempotency_key=media_key,
        ) if media_key else None
        if existing:
            continue
        _prepare_whatsapp_media_send(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
            content=bytes(attachment.get("content") or b""),
            filename=str(attachment.get("filename") or "whatsapp-attachment"),
            content_type=str(attachment.get("content_type") or "application/octet-stream"),
            is_audio=bool(attachment.get("is_audio")),
        )
    sent_messages: list[InboundMessage] = []
    if clean_body or template_name:
        reservation = None
        owns_send = True
        if text_key:
            reservation, owns_send = _reserve_manual_response(
                db,
                company_id=company_id,
                conversation_id=conversation_id,
                body=clean_body or f"[Plantilla WhatsApp: {template_name}]",
                user_id=user_id,
                template_name=template_name,
                idempotency_key=text_key,
            )
        if not owns_send:
            sent_messages.append(reservation)
        else:
            try:
                result = await send_whatsapp_text(
                    db,
                    company_id=company_id,
                    conversation_id=conversation_id,
                    body=clean_body,
                    client=client,
                    template_name=template_name,
                    template_language=template_language,
                    template_components=template_components,
                )
            except Exception as exc:  # noqa: BLE001
                if reservation:
                    _mark_outbound_send_unknown(db, reservation, user_id=user_id, error=exc)
                raise
            try:
                sent_message = record_manual_response(
                    db,
                    company_id=company_id,
                    conversation_id=conversation_id,
                    body=clean_body or f"[Plantilla WhatsApp: {template_name}]",
                    user_id=user_id,
                    external_id=result["provider_message_id"],
                    status="accepted",
                    processing_step="outbound_accepted",
                    idempotency_key=text_key,
                    template_name=template_name,
                )
            except Exception as exc:  # noqa: BLE001
                if reservation:
                    _mark_outbound_send_unknown(db, reservation, user_id=user_id, error=exc)
                raise
            sent_messages.append(sent_message)
    for index, attachment in enumerate(attachment_payloads):
        media_key = f"{idempotency_key}:media:{index}" if idempotency_key else None
        reservation = None
        owns_send = True
        if media_key:
            reservation, owns_send = _reserve_manual_response(
                db,
                company_id=company_id,
                conversation_id=conversation_id,
                body="",
                user_id=user_id,
                template_name=None,
                idempotency_key=media_key,
            )
        if not owns_send:
            sent_messages.append(reservation)
            continue
        try:
            result = await send_whatsapp_media(
                db,
                company_id=company_id,
                conversation_id=conversation_id,
                content=bytes(attachment.get("content") or b""),
                filename=str(attachment.get("filename") or "whatsapp-attachment"),
                content_type=str(attachment.get("content_type") or "application/octet-stream"),
                is_audio=bool(attachment.get("is_audio")),
                client=client,
            )
        except Exception as exc:  # noqa: BLE001
            if reservation:
                _mark_outbound_send_unknown(db, reservation, user_id=user_id, error=exc)
            raise
        try:
            sent_message = record_manual_response(
                db,
                company_id=company_id,
                conversation_id=conversation_id,
                body="",
                user_id=user_id,
                external_id=result["provider_message_id"],
                status="accepted",
                processing_step="outbound_accepted",
                attachments=[attachment],
                idempotency_key=media_key,
            )
        except Exception as exc:  # noqa: BLE001
            if reservation:
                _mark_outbound_send_unknown(db, reservation, user_id=user_id, error=exc)
            raise
        sent_messages.append(sent_message)
    return sent_messages[-1]


def _extract_message_text(message: dict[str, Any]) -> str | None:
    if message.get("type") == "text":
        return (message.get("text") or {}).get("body")
    if message.get("type") == "document":
        document = message.get("document") or {}
        return document.get("caption")
    if message.get("type") == "image":
        image = message.get("image") or {}
        return image.get("caption")
    if message.get("type") == "audio":
        return (message.get("audio") or {}).get("caption")
    if message.get("type") == "video":
        video = message.get("video") or {}
        return video.get("caption")
    if message.get("type") == "revoke":
        return "Mensaje eliminado desde WhatsApp Business App"
    if message.get("type") == "edit":
        edit = message.get("edit") if isinstance(message.get("edit"), dict) else {}
        edited_message = edit.get("message") if isinstance(edit.get("message"), dict) else {}
        return _extract_message_text(edited_message) or "Mensaje editado desde WhatsApp Business App"
    return message.get("text", {}).get("body") if isinstance(message.get("text"), dict) else None


def _message_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    if message.get("type") == "document":
        document = message.get("document") or {}
        filename = document.get("filename")
        content_type = document.get("mime_type")
        attachments.append(
            {
                "media_id": document.get("id"),
                "filename": filename,
                "content_type": content_type,
                "size_bytes": document.get("file_size"),
                "is_pdf": str(content_type or "").lower() == "application/pdf" or str(filename or "").lower().endswith(".pdf"),
                "downloadable": _is_supported_media(filename, content_type),
            }
        )
    elif message.get("type") == "image":
        image = message.get("image") or {}
        attachments.append(
            {
                "media_id": image.get("id"),
                "filename": image.get("filename") or "image.jpg",
                "content_type": image.get("mime_type"),
                "size_bytes": image.get("file_size"),
                "is_image": True,
                "downloadable": False,
            }
        )
    elif message.get("type") == "audio":
        audio = message.get("audio") or {}
        attachments.append(
            {
                "media_id": audio.get("id"),
                "filename": audio.get("filename") or "audio.ogg",
                "content_type": audio.get("mime_type"),
                "size_bytes": audio.get("file_size"),
                "is_audio": True,
                "downloadable": _is_supported_media(audio.get("filename") or "audio.ogg", audio.get("mime_type"), is_audio=True),
            }
        )
    elif message.get("type") == "video":
        video = message.get("video") or {}
        attachments.append(
            {
                "media_id": video.get("id"),
                "filename": video.get("filename") or "video.mp4",
                "content_type": video.get("mime_type"),
                "size_bytes": video.get("file_size"),
                "downloadable": False,
            }
        )
    return attachments


def whatsapp_event_requires_media_download(event: dict[str, Any]) -> bool:
    attachments = event.get("attachments") if isinstance(event, dict) else None
    return any(
        isinstance(attachment, dict) and bool(attachment.get("downloadable"))
        for attachment in (attachments if isinstance(attachments, list) else [])
    )


def whatsapp_event_has_processable_content(event: dict[str, Any]) -> bool:
    if not isinstance(event, dict) or whatsapp_event_requires_media_download(event):
        return False
    return bool(str(event.get("text_content") or "").strip())


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled", "active"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default
