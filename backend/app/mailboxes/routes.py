from __future__ import annotations

from argparse import Namespace
import hmac
from datetime import datetime, timezone
from email.utils import parseaddr
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.config import effective_email_batch_limit, get_settings
from app.core.templating import templates
from app.db.models import Company, Email, InboundMessage, Mailbox
from app.jobs.service import enqueue_job
from app.logs.service import log_action
from app.mailboxes.service import get_mailbox, get_or_create_mailbox_sync_state, list_mailboxes, serialize_mailbox
from app.mailboxes.pilot_sync import run_pilot_sync
from app.mailboxes.google_oauth import (
    GOOGLE_OAUTH_STATE_SESSION_KEY,
    GoogleOAuthError,
    build_google_authorization_url,
    exchange_google_authorization_code,
    fetch_google_account_email,
    google_oauth_configured,
    google_oauth_redirect_uri,
    mailbox_oauth_provider,
    new_oauth_state,
    encrypt_refresh_token,
)
from app.mailboxes.microsoft_oauth import (
    MICROSOFT_OAUTH_STATE_SESSION_KEY,
    MicrosoftOAuthError,
    build_microsoft_authorization_url,
    exchange_microsoft_authorization_code,
    microsoft_oauth_configured,
    microsoft_oauth_redirect_uri,
    new_oauth_state as new_microsoft_oauth_state,
    encrypt_refresh_token as encrypt_microsoft_refresh_token,
)
from app.master.database import get_master_db
from app.master.models import MailboxSyncState
from app.master.service import TenantUser
from app.settings.integrations import test_imap_connection, test_smtp_connection
from app.settings.service import resolve_updated_by_id, update_with_form
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/settings/mailboxes", tags=["mailboxes"])

EDIT_FIELDS = [
    "name", "provider", "connection_method", "connected_email", "imap_host", "imap_port", "imap_security",
    "imap_use_ssl", "imap_username", "imap_password_encrypted", "inbox_folder", "mailbox",
    "read_limit", "polling_frequency_minutes", "auto_sync_enabled", "auto_process_on_fetch", "read_unread_only",
    "smtp_provider", "smtp_enabled", "smtp_host", "smtp_port", "smtp_security", "smtp_username",
    "smtp_password_encrypted", "from_email", "from_name", "reply_to",
]
SECRET_FIELDS = {"imap_password_encrypted", "smtp_password_encrypted"}
BOOL_FIELDS = {"imap_use_ssl", "auto_sync_enabled", "auto_process_on_fetch", "read_unread_only", "smtp_enabled"}
BACKFILL_PRODUCTION_CONFIRM = "BACKFILL_PRODUCTION_CONFIRM"


def _safe_database_name(database_url: str | None) -> str | None:
    """Keep the administrative runner out of normal mailbox route startup."""
    from scripts.run_mailbox_backfill_once import _safe_database_name as safe_database_name

    return safe_database_name(database_url)


def run_backfill_in_context(*args, **kwargs):  # noqa: ANN002, ANN003
    """Load the one-shot runner only when the guarded action is invoked."""
    from scripts.run_mailbox_backfill_once import run_backfill_in_context as runner

    return runner(*args, **kwargs)


def _can_edit(user: TenantUser) -> bool:
    return user.role.name in {"Administrador", "Superadmin"}


def _can_test(user: TenantUser) -> bool:
    return user.role.name in {"Administrador", "Supervisor", "Superadmin"}


async def _form_data(request: Request) -> dict[str, str]:
    if "application/json" in (request.headers.get("content-type") or ""):
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    form = await request.form()
    return {key: str(value) for key, value in form.multi_items() if not hasattr(value, "filename")}


def _response(
    request: Request,
    payload: dict,
    *,
    redirect: str = "/settings/mailboxes",
    status_code: int = 303,
    save_feedback: bool = False,
):
    if "application/json" in (request.headers.get("accept") or "") or "application/json" in (request.headers.get("content-type") or ""):
        return JSONResponse(payload, status_code=status_code if status_code >= 400 else 200)
    if save_feedback:
        message = str(payload.get("message") or "").strip()
        if payload.get("ok"):
            feedback = {"mailbox_message": message or "Configuración IMAP guardada correctamente."}
        else:
            detail = f" {message}" if message else ""
            feedback = {"mailbox_error": f"No se ha podido guardar la configuración IMAP.{detail}"}
        redirect = f"{redirect}{'&' if '?' in redirect else '?'}{urlencode(feedback)}"
    return RedirectResponse(redirect, status_code=303 if save_feedback else status_code)


def _valid_email(value: str) -> bool:
    address = parseaddr(value)[1].strip()
    return bool(address and "@" in address and "." in address.rsplit("@", 1)[-1])


def _oauth_feedback(message: str, *, ok: bool = False) -> RedirectResponse:
    key = "mailbox_message" if ok else "mailbox_error"
    return RedirectResponse(f"/settings/mailboxes?{urlencode({key: message})}", status_code=303)


def _normalize_new_mailbox_data(data: dict[str, str]) -> tuple[dict[str, str], str]:
    """Validate the supported creation profiles before applying form fields."""
    normalized = dict(data)
    provider = (normalized.get("provider") or "imap").strip().lower()
    connection_method = (normalized.get("connection_method") or "password").strip().lower()
    if provider not in {"imap", "gmail", "microsoft365"}:
        raise ValueError("El proveedor de correo no es válido.")
    if provider == "microsoft365":
        if connection_method != "oauth2":
            raise ValueError("Microsoft 365 requiere OAuth del proveedor.")
        normalized.update(provider="microsoft365", connection_method="oauth2")
        normalized.pop("imap_username", None)
        normalized.pop("imap_password_encrypted", None)
        normalized.pop("imap_host", None)
        normalized.pop("imap_port", None)
        normalized.pop("imap_security", None)
        return normalized, "microsoft365"
    if provider == "imap" and connection_method != "password":
        raise ValueError("IMAP manual requiere usuario y contraseña.")
    if provider == "gmail" and connection_method not in {"password", "oauth2"}:
        raise ValueError("El método de autenticación de Gmail no es válido.")
    normalized["provider"] = provider
    normalized["connection_method"] = connection_method
    return normalized, "manual"


def _save_mailbox(db: Session, mailbox: Mailbox, data: dict[str, str], user: TenantUser) -> None:
    normalized = {key: data[key] for key in EDIT_FIELDS if key in data}
    update_with_form(mailbox, normalized, SECRET_FIELDS)
    mailbox.name = (mailbox.name or mailbox.email_address or "Buzón de correo").strip()[:150]
    mailbox.email_address = (data.get("email_address") or mailbox.email_address or mailbox.connected_email or mailbox.imap_username or "").strip().lower()
    mailbox.connected_email = (data.get("connected_email") or mailbox.email_address or "").strip() or None
    mailbox.inbox_folder = (mailbox.inbox_folder or mailbox.mailbox or "INBOX").strip() or "INBOX"
    if mailbox.connection_method == "oauth2" and mailbox.provider in {"gmail", "microsoft365"}:
        mailbox.imap_host = mailbox.imap_host or ("imap.gmail.com" if mailbox.provider == "gmail" else "outlook.office365.com")
        mailbox.imap_port = mailbox.imap_port or 993
        mailbox.imap_security = mailbox.imap_security or "ssl_tls"
        mailbox.imap_use_ssl = True
        mailbox.imap_username = mailbox.imap_username or mailbox.email_address
        mailbox.imap_password_encrypted = None
    mailbox.updated_by = resolve_updated_by_id(db, user)
    mailbox.updated_at = datetime.now(timezone.utc)


@router.get("")
def mailboxes_page(
    request: Request,
    db: Session = Depends(get_tenant_db),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(current_user),
):
    mailboxes = list_mailboxes(db, user.company_id)
    states = {
        state.mailbox_id: state
        for state in master_db.scalars(
            select(MailboxSyncState).where(MailboxSyncState.company_id == user.company_id)
        ).all()
    }
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(
            {
                "ok": True,
                "items": [
                    {**serialize_mailbox(mailbox), "sync_state": _serialize_state(states.get(mailbox.id))}
                    for mailbox in mailboxes
                ],
            }
        )
    return templates.TemplateResponse(
        "settings/mailboxes.html",
        {
            "request": request,
            "user": user,
            "mailboxes": mailboxes,
            "mailbox_states": states,
            "can_edit": _can_edit(user),
            "can_test": _can_test(user),
            "message": request.query_params.get("mailbox_message"),
            "error": request.query_params.get("mailbox_error"),
            "pilot_sync_result": {
                "imported": request.query_params.get("pilot_imported"),
                "duplicates": request.query_params.get("pilot_duplicates"),
                "skipped_attachment": request.query_params.get("pilot_skipped_attachment"),
                "candidates_reviewed": request.query_params.get("pilot_candidates_reviewed"),
            }
            if request.query_params.get("pilot_imported") is not None
            else None,
        },
    )


def _serialize_state(state: MailboxSyncState | None) -> dict | None:
    if not state:
        return None
    return {
        "mailbox_id": state.mailbox_id,
        "status": state.status,
        "sync_status": state.sync_status,
        "last_seen_uid": state.last_seen_uid,
        "uidvalidity": state.uidvalidity,
        "last_sync_at": state.last_sync_at.isoformat() if state.last_sync_at else None,
        "last_error_message": state.last_error_message,
        "backfill_status": state.backfill_status,
        "backfill_last_uid": state.backfill_last_uid,
    }


@router.get("/oauth/google/callback", name="google_mailbox_oauth_callback")
def google_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_edit(user):
        return _oauth_feedback("No tienes permisos para conectar Google.")
    stored = request.session.pop(GOOGLE_OAUTH_STATE_SESSION_KEY, None)
    received_state = (state or "").strip()
    if not isinstance(stored, dict) or not received_state or not hmac.compare_digest(str(stored.get("state") or ""), received_state):
        return _oauth_feedback("La sesión de conexión con Google ha caducado. Recarga la pantalla.")
    try:
        expires_at = int(stored.get("expires_at") or 0)
        company_id = int(stored.get("company_id"))
        mailbox_id = int(stored.get("mailbox_id"))
        user_id = int(stored.get("user_id"))
    except (TypeError, ValueError):
        return _oauth_feedback("La sesión de conexión con Google no es válida.")
    if expires_at <= int(datetime.now(timezone.utc).timestamp()):
        return _oauth_feedback("La sesión de conexión con Google ha caducado. Recarga la pantalla.")
    if company_id != user.company_id or user_id != user.id:
        return _oauth_feedback("La sesión de conexión no coincide con el tenant actual.")
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _oauth_feedback("No se encontró el buzón solicitado.")
    if mailbox.enabled or mailbox.auto_sync_enabled:
        return _oauth_feedback("Desactiva el buzón y la sincronización antes de conectar Google.")
    if error or not code:
        return _oauth_feedback("La autorización de Google no se ha completado.")
    if mailbox.provider != "gmail" or mailbox.connection_method != "oauth2":
        return _oauth_feedback("Selecciona Gmail y Google OAuth en la configuración del buzón.")
    try:
        redirect_uri = google_oauth_redirect_uri(request)
        tokens = exchange_google_authorization_code(code, redirect_uri)
        connected_email = fetch_google_account_email(tokens.access_token)
    except GoogleOAuthError as exc:
        log_action(db, company_id=user.company_id, user=user, action="settings.mailbox.oauth.google.failed", entity_type="mailbox", entity_id=mailbox.id, message=f"Conexión Google OAuth fallida: {exc.error_type}")
        return _oauth_feedback(str(exc))
    expected_email = (mailbox.email_address or "").strip().lower()
    if not expected_email or connected_email != expected_email:
        log_action(db, company_id=user.company_id, user=user, action="settings.mailbox.oauth.google.failed", entity_type="mailbox", entity_id=mailbox.id, message="La cuenta Google autorizada no coincide con el buzón.")
        return _oauth_feedback("La cuenta Google autorizada no coincide con la dirección del buzón.")
    mailbox.provider = "gmail"
    mailbox.connection_method = "oauth2"
    mailbox.connected_email = connected_email
    mailbox.refresh_token_encrypted = encrypt_refresh_token(tokens.refresh_token or "")
    mailbox.access_token_encrypted = None
    mailbox.imap_password_encrypted = None
    mailbox.updated_by = resolve_updated_by_id(db, user)
    mailbox.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="settings.mailbox.oauth.google.connected", entity_type="mailbox", entity_id=mailbox.id, message="Buzón conectado con Google OAuth")
    return _oauth_feedback("Google se ha conectado correctamente. El buzón sigue desactivado.", ok=True)


@router.get("/{mailbox_id}/oauth/google/start", name="google_mailbox_oauth_start")
def google_oauth_start(
    mailbox_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_edit(user):
        return _oauth_feedback("No tienes permisos para conectar Google.")
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _oauth_feedback("No se encontró el buzón solicitado.")
    if mailbox.provider != "gmail" or mailbox.connection_method != "oauth2":
        return _oauth_feedback("Selecciona Gmail y Google OAuth en la configuración del buzón.")
    if mailbox.enabled or mailbox.auto_sync_enabled:
        return _oauth_feedback("Desactiva el buzón y la sincronización antes de conectar Google.")
    if not google_oauth_configured():
        return _oauth_feedback("La conexión Google OAuth todavía no está configurada en KIBAK.")
    oauth_state = new_oauth_state(company_id=user.company_id, mailbox_id=mailbox.id, user_id=user.id)
    request.session[GOOGLE_OAUTH_STATE_SESSION_KEY] = oauth_state
    try:
        redirect_uri = google_oauth_redirect_uri(request)
        authorization_url = build_google_authorization_url(
            state=oauth_state["state"],
            redirect_uri=redirect_uri,
            login_hint=mailbox.email_address,
        )
    except GoogleOAuthError as exc:
        request.session.pop(GOOGLE_OAUTH_STATE_SESSION_KEY, None)
        return _oauth_feedback(str(exc))
    return RedirectResponse(authorization_url, status_code=307)


@router.get("/oauth/microsoft/callback", name="microsoft_mailbox_oauth_callback")
def microsoft_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_edit(user):
        return _oauth_feedback("No tienes permisos para conectar Microsoft.")
    stored = request.session.pop(MICROSOFT_OAUTH_STATE_SESSION_KEY, None)
    received_state = (state or "").strip()
    if not isinstance(stored, dict) or not received_state or not hmac.compare_digest(str(stored.get("state") or ""), received_state):
        return _oauth_feedback("La sesión de conexión con Microsoft ha caducado. Recarga la pantalla.")
    try:
        expires_at = int(stored.get("expires_at") or 0)
        company_id = int(stored.get("company_id"))
        mailbox_id = int(stored.get("mailbox_id"))
        user_id = int(stored.get("user_id"))
    except (TypeError, ValueError):
        return _oauth_feedback("La sesión de conexión con Microsoft no es válida.")
    if expires_at <= int(datetime.now(timezone.utc).timestamp()):
        return _oauth_feedback("La sesión de conexión con Microsoft ha caducado. Recarga la pantalla.")
    if company_id != user.company_id or user_id != user.id:
        return _oauth_feedback("La sesión de conexión no coincide con el tenant actual.")
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _oauth_feedback("No se encontró el buzón solicitado.")
    if mailbox.enabled or mailbox.auto_sync_enabled:
        return _oauth_feedback("Desactiva el buzón y la sincronización antes de conectar Microsoft.")
    if error or not code:
        return _oauth_feedback("La autorización de Microsoft no se ha completado.")
    if mailbox.provider != "microsoft365" or mailbox.connection_method != "oauth2":
        return _oauth_feedback("Selecciona Microsoft 365 y OAuth del proveedor en la configuración del buzón.")
    try:
        redirect_uri = microsoft_oauth_redirect_uri(request)
        tokens = exchange_microsoft_authorization_code(code, redirect_uri)
    except MicrosoftOAuthError as exc:
        log_action(db, company_id=user.company_id, user=user, action="settings.mailbox.oauth.microsoft.failed", entity_type="mailbox", entity_id=mailbox.id, message=f"Conexión Microsoft OAuth fallida: {exc.error_type}")
        return _oauth_feedback(str(exc))
    mailbox.provider = "microsoft365"
    mailbox.connection_method = "oauth2"
    mailbox.connected_email = mailbox.email_address
    mailbox.imap_host = mailbox.imap_host or "outlook.office365.com"
    mailbox.imap_port = mailbox.imap_port or 993
    mailbox.imap_security = mailbox.imap_security or "ssl_tls"
    mailbox.imap_use_ssl = True
    mailbox.imap_username = mailbox.imap_username or mailbox.email_address
    mailbox.refresh_token_encrypted = encrypt_microsoft_refresh_token(tokens.refresh_token or "")
    mailbox.access_token_encrypted = None
    mailbox.imap_password_encrypted = None
    mailbox.updated_by = resolve_updated_by_id(db, user)
    mailbox.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="settings.mailbox.oauth.microsoft.connected", entity_type="mailbox", entity_id=mailbox.id, message="Buzón conectado con Microsoft OAuth")
    return _oauth_feedback("Microsoft se ha conectado correctamente. El buzón sigue desactivado.", ok=True)


@router.get("/{mailbox_id}/oauth/microsoft/start", name="microsoft_mailbox_oauth_start")
def microsoft_oauth_start(
    mailbox_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_edit(user):
        return _oauth_feedback("No tienes permisos para conectar Microsoft.")
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _oauth_feedback("No se encontró el buzón solicitado.")
    if mailbox.provider != "microsoft365" or mailbox.connection_method != "oauth2":
        return _oauth_feedback("Selecciona Microsoft 365 y OAuth del proveedor en la configuración del buzón.")
    if mailbox.enabled or mailbox.auto_sync_enabled:
        return _oauth_feedback("Desactiva el buzón y la sincronización antes de conectar Microsoft.")
    if not microsoft_oauth_configured():
        return _oauth_feedback("La conexión Microsoft OAuth todavía no está configurada en KIBAK.")
    oauth_state = new_microsoft_oauth_state(company_id=user.company_id, mailbox_id=mailbox.id, user_id=user.id)
    request.session[MICROSOFT_OAUTH_STATE_SESSION_KEY] = oauth_state
    try:
        redirect_uri = microsoft_oauth_redirect_uri(request)
        authorization_url = build_microsoft_authorization_url(
            state=oauth_state["state"],
            redirect_uri=redirect_uri,
            login_hint=mailbox.email_address,
        )
    except MicrosoftOAuthError as exc:
        request.session.pop(MICROSOFT_OAUTH_STATE_SESSION_KEY, None)
        return _oauth_feedback(str(exc))
    return RedirectResponse(authorization_url, status_code=307)


@router.post("")
async def create_mailbox(
    request: Request,
    db: Session = Depends(get_tenant_db),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar buzones."}, status_code=403)
    data = await _form_data(request)
    try:
        data, creation_profile = _normalize_new_mailbox_data(data)
    except ValueError as exc:
        return _response(request, {"ok": False, "message": str(exc)}, status_code=400, save_feedback=True)
    email_address = (data.get("email_address") or data.get("connected_email") or data.get("imap_username") or "").strip().lower()
    if not _valid_email(email_address):
        return _response(request, {"ok": False, "message": "Indica una dirección de correo válida."}, status_code=400, save_feedback=True)
    mailbox = Mailbox(
        company_id=user.company_id,
        name=(data.get("name") or email_address).strip(),
        email_address=email_address,
        enabled=False,
        auto_sync_enabled=False,
        smtp_enabled=False,
    )
    db.add(mailbox)
    try:
        _save_mailbox(db, mailbox, data, user)
        if creation_profile == "microsoft365":
            mailbox.provider = "microsoft365"
            mailbox.connection_method = "oauth2"
            mailbox.imap_host = "outlook.office365.com"
            mailbox.imap_port = 993
            mailbox.imap_security = "ssl_tls"
            mailbox.imap_use_ssl = True
            mailbox.imap_username = mailbox.email_address
            mailbox.imap_password_encrypted = None
            mailbox.access_token_encrypted = None
            mailbox.refresh_token_encrypted = None
            mailbox.enabled = False
            mailbox.auto_sync_enabled = False
            mailbox.mark_as_read_after_import = False
            mailbox.smtp_enabled = False
        db.commit()
        db.refresh(mailbox)
    except IntegrityError:
        db.rollback()
        return _response(request, {"ok": False, "message": "Ya existe un buzón con esa dirección en este tenant."}, status_code=409, save_feedback=True)
    except Exception:
        db.rollback()
        return _response(request, {"ok": False, "message": "Revisa los datos introducidos e inténtalo de nuevo."}, status_code=400, save_feedback=True)
    get_or_create_mailbox_sync_state(master_db, mailbox)
    log_action(db, company_id=user.company_id, user=user, action="settings.mailbox.create", entity_type="mailbox", entity_id=mailbox.id, message="Buzón creado")
    return _response(
        request,
        {
            "ok": True,
            "message": "Buzón Microsoft 365 preparado para conectar." if creation_profile == "microsoft365" else "Configuración IMAP guardada correctamente.",
            "mailbox": serialize_mailbox(mailbox),
        },
        redirect="/settings/mailboxes",
        save_feedback=True,
    )


@router.post("/{mailbox_id}")
async def update_mailbox(
    mailbox_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar buzones."}, status_code=403)
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _response(request, {"ok": False, "message": "No se encontró el buzón solicitado."}, status_code=404)
    data = await _form_data(request)
    email_address = (data.get("email_address") or mailbox.email_address or "").strip().lower()
    if not _valid_email(email_address):
        return _response(request, {"ok": False, "message": "Indica una dirección de correo válida."}, status_code=400, save_feedback=True)
    mailbox.email_address = email_address
    _save_mailbox(db, mailbox, data, user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _response(request, {"ok": False, "message": "Ya existe un buzón con esa dirección en este tenant."}, status_code=409, save_feedback=True)
    except Exception:
        db.rollback()
        return _response(request, {"ok": False, "message": "Revisa los datos introducidos e inténtalo de nuevo."}, status_code=400, save_feedback=True)
    state = get_or_create_mailbox_sync_state(master_db, mailbox)
    log_action(
        db,
        company_id=user.company_id,
        user=user,
        action="settings.mailbox.update",
        entity_type="mailbox",
        entity_id=mailbox.id,
        message="Configuración del buzón guardada",
    )
    return _response(
        request,
        {
            "ok": True,
            "message": "Configuración IMAP guardada correctamente.",
            "mailbox": serialize_mailbox(mailbox),
            "sync_state": _serialize_state(state),
        },
        save_feedback=True,
    )


@router.post("/{mailbox_id}/toggle")
def toggle_mailbox(
    mailbox_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar buzones."}, status_code=403)
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _response(request, {"ok": False, "message": "No se encontró el buzón solicitado."}, status_code=404)
    mailbox.enabled = not mailbox.enabled
    mailbox.updated_at = datetime.now(timezone.utc)
    db.commit()
    state = get_or_create_mailbox_sync_state(master_db, mailbox)
    return _response(request, {"ok": True, "enabled": mailbox.enabled, "sync_state": _serialize_state(state)})


@router.post("/{mailbox_id}/test")
def test_mailbox(
    mailbox_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_test(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para probar buzones."}, status_code=403)
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _response(request, {"ok": False, "message": "No se encontró el buzón solicitado."}, status_code=404)
    result = test_imap_connection(mailbox, request_id=getattr(request.state, "request_id", None))
    mailbox.last_imap_test_at = datetime.now(timezone.utc)
    mailbox.last_imap_test_ok = bool(result.get("ok"))
    mailbox.last_imap_test_message = result.get("message")
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="settings.mailbox.test", entity_type="mailbox", entity_id=mailbox.id, message=result.get("message") or "Prueba IMAP ejecutada")
    return _response(request, {"ok": bool(result.get("ok")), "result": result}, redirect="/settings/mailboxes")


@router.post("/{mailbox_id}/smtp-test")
def test_mailbox_smtp(
    mailbox_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_test(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para probar buzones."}, status_code=403)
    if get_settings().is_pilot_runtime:
        return _response(request, {"ok": False, "message": "SMTP está desactivado en el piloto gratuito."}, status_code=409)
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _response(request, {"ok": False, "message": "No se encontró el buzón solicitado."}, status_code=404)
    result = test_smtp_connection(mailbox)
    mailbox.last_smtp_test_at = datetime.now(timezone.utc)
    mailbox.last_smtp_test_ok = bool(result.get("ok"))
    mailbox.last_smtp_test_message = result.get("message")
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="settings.mailbox.smtp_test", entity_type="mailbox", entity_id=mailbox.id, message=result.get("message") or "Prueba SMTP ejecutada")
    return _response(request, {"ok": bool(result.get("ok")), "result": result}, redirect="/settings/mailboxes")


@router.post("/{mailbox_id}/sync")
def sync_mailbox(
    mailbox_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_test(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para sincronizar buzones."}, status_code=403)
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _response(request, {"ok": False, "message": "No se encontró el buzón solicitado."}, status_code=404)
    limit = effective_email_batch_limit(mailbox.read_limit, standard_default=10, standard_max=50)
    job = enqueue_job(db, company_id=user.company_id, job_type="email_sync", payload={"mailbox_id": mailbox.id, "auto_process": False, "unread_only": mailbox.read_unread_only, "limit": limit}, created_by_user_id=user.id)
    return _response(request, {"ok": True, "job_id": job.id, "status": job.status, "mailbox_id": mailbox.id})


@router.post("/{mailbox_id}/pilot-sync")
def pilot_sync_mailbox(
    mailbox_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_test(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para ejecutar el piloto."}, status_code=403)
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _response(request, {"ok": False, "message": "No se encontró el buzón solicitado."}, status_code=404)
    result = run_pilot_sync(db, mailbox, user.company_id)
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)
    if not result.get("ok"):
        return _response(
            request,
            {"ok": False, "message": result.get("message") or "No se pudo ejecutar el piloto."},
            redirect=f"/settings/mailboxes?{urlencode({'mailbox_error': result.get('message') or 'No se pudo ejecutar el piloto.'})}",
            status_code=303,
        )
    query = urlencode(
        {
            "mailbox_message": "Piloto completado.",
            "pilot_imported": result.get("imported", 0),
            "pilot_duplicates": result.get("duplicates", 0),
            "pilot_skipped_attachment": result.get("skipped_attachment", 0),
            "pilot_candidates_reviewed": result.get("candidates_reviewed", 0),
        }
    )
    return _response(request, {"ok": True, "result": result}, redirect=f"/settings/mailboxes?{query}", status_code=303)


@router.post("/{mailbox_id}/backfill")
async def backfill_mailbox(
    mailbox_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_test(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para lanzar backfill."}, status_code=403)
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _response(request, {"ok": False, "message": "No se encontró el buzón solicitado."}, status_code=404)
    data = await _form_data(request)
    payload = {
        "mailbox_id": mailbox.id,
        "from_date": data.get("from_date") or mailbox.read_from_date,
        "to_date": data.get("to_date") or None,
        "limit": None,
        "unbounded": True,
    }
    job = enqueue_job(db, company_id=user.company_id, job_type="backfill_imap", payload=payload, created_by_user_id=user.id)
    return _response(request, {"ok": True, "job_id": job.id, "status": job.status, "mailbox_id": mailbox.id})


@router.post("/{mailbox_id}/backfill-once")
async def administrative_backfill_once(
    mailbox_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(current_user),
):
    """Run the one-shot Production backfill inside the authenticated app context."""
    settings = get_settings()
    if (
        settings.environment != "production"
        or settings.app_slug.strip().lower() != "kibak"
        or not settings.enable_production_backfill_admin
    ):
        return JSONResponse({"ok": False, "message": "Acción administrativa no disponible."}, status_code=404)
    if not _can_edit(user):
        return JSONResponse({"ok": False, "message": "No autorizado."}, status_code=403)
    if user.company_slug != "kibak-pilot":
        return JSONResponse({"ok": False, "message": "Tenant no autorizado para esta acción."}, status_code=403)

    data = await _form_data(request)
    if data.get("confirmation") != BACKFILL_PRODUCTION_CONFIRM:
        return JSONResponse({"ok": False, "message": "Confirmación explícita requerida."}, status_code=400)

    mailboxes = list(
        db.scalars(
            select(Mailbox).where(
                Mailbox.company_id == user.company_id,
                Mailbox.provider == "microsoft365",
                Mailbox.connection_method == "oauth2",
            )
        ).all()
    )
    if len(mailboxes) != 1 or mailboxes[0].id != mailbox_id:
        return JSONResponse({"ok": False, "message": "El buzón no coincide con el único buzón piloto."}, status_code=409)
    mailbox = mailboxes[0]
    company = db.scalar(select(Company).where(Company.id == user.company_id))
    if company is None:
        return JSONResponse({"ok": False, "message": "Tenant no disponible."}, status_code=503)

    log_action(
        db,
        company_id=user.company_id,
        user=user,
        action="settings.mailbox.backfill_once.start",
        entity_type="mailbox",
        entity_id=mailbox.id,
        message="Backfill administrativo iniciado",
        metadata={"since": "2026-09-14", "mode": "one-shot", "tenant": user.company_slug},
    )
    args = Namespace(company_slug="kibak-pilot", since="2026-09-14", to=None)
    try:
        result = run_backfill_in_context(
            args,
            settings=settings,
            master_db=master_db,
            tenant_db=db,
            company=company,
            mailbox=mailbox,
            database_name=_safe_database_name(user.database_url),
        )
    except Exception as exc:  # noqa: BLE001
        log_action(
            db,
            company_id=user.company_id,
            user=user,
            action="settings.mailbox.backfill_once.finish",
            entity_type="mailbox",
            entity_id=mailbox.id,
            message="Backfill administrativo detenido",
            metadata={"ok": False, "error_type": type(exc).__name__},
        )
        return JSONResponse({"ok": False, "message": "Backfill detenido durante la validación o ejecución."}, status_code=409)

    metrics = {
        "range": result.get("range"),
        "backfill": result.get("backfill"),
        "counts_before": result.get("counts_before"),
        "counts_after": result.get("counts_after"),
        "count_deltas": result.get("count_deltas"),
        "received_range": result.get("received_range"),
        "storage": result.get("storage"),
        "safety": {
            "auto_process": False,
            "simulation_mode": result.get("policy", {}).get("simulation_mode"),
            "auto_forwarding": result.get("policy", {}).get("auto_forwarding_enabled"),
            "background_worker_started": result.get("execution", {}).get("background_worker_started"),
            "openai_called": result.get("execution", {}).get("openai_called"),
            "routing_jobs_enqueued": result.get("backfill", {}).get("routing_jobs_enqueued", 0),
        },
    }
    log_action(
        db,
        company_id=user.company_id,
        user=user,
        action="settings.mailbox.backfill_once.finish",
        entity_type="mailbox",
        entity_id=mailbox.id,
        message="Backfill administrativo finalizado",
        metadata={"ok": bool(result.get("ok")), "metrics": metrics},
    )
    return JSONResponse({"ok": bool(result.get("ok")), "metrics": metrics}, status_code=200 if result.get("ok") else 409)


@router.post("/{mailbox_id}/delete")
def delete_mailbox(
    mailbox_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    master_db: Session = Depends(get_master_db),
    user: TenantUser = Depends(current_user),
):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para eliminar buzones."}, status_code=403)
    mailbox = get_mailbox(db, user.company_id, mailbox_id)
    if not mailbox:
        return _response(request, {"ok": False, "message": "No se encontró el buzón solicitado."}, status_code=404)
    referenced = db.scalar(select(Email.id).where(Email.company_id == user.company_id, Email.mailbox_id == mailbox.id).limit(1))
    inbound_referenced = db.scalar(select(InboundMessage.id).where(InboundMessage.company_id == user.company_id, InboundMessage.mailbox_id == mailbox.id).limit(1))
    if referenced or inbound_referenced:
        return _response(request, {"ok": False, "message": "No se puede eliminar un buzón que ya tiene mensajes sincronizados."}, status_code=409)
    state = master_db.scalar(select(MailboxSyncState).where(MailboxSyncState.company_id == user.company_id, MailboxSyncState.mailbox_id == mailbox.id))
    if state:
        master_db.delete(state)
        master_db.commit()
    db.delete(mailbox)
    db.commit()
    return _response(request, {"ok": True, "mailbox_id": mailbox_id})
