from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parseaddr
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.templating import templates
from app.db.models import Email, InboundMessage, Mailbox
from app.jobs.service import enqueue_job
from app.logs.service import log_action
from app.mailboxes.service import get_mailbox, get_or_create_mailbox_sync_state, list_mailboxes, serialize_mailbox
from app.master.database import get_master_db
from app.master.models import MailboxSyncState
from app.master.service import TenantUser
from app.settings.integrations import test_imap_connection, test_smtp_connection
from app.settings.service import resolve_updated_by_id, update_with_form
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/settings/mailboxes", tags=["mailboxes"])

EDIT_FIELDS = [
    "name", "provider", "connected_email", "imap_host", "imap_port", "imap_security",
    "imap_use_ssl", "imap_username", "imap_password_encrypted", "inbox_folder", "mailbox",
    "read_limit", "polling_frequency_minutes", "auto_sync_enabled", "read_unread_only",
    "smtp_provider", "smtp_enabled", "smtp_host", "smtp_port", "smtp_security", "smtp_username",
    "smtp_password_encrypted", "from_email", "from_name", "reply_to",
]
SECRET_FIELDS = {"imap_password_encrypted", "smtp_password_encrypted"}
BOOL_FIELDS = {"imap_use_ssl", "auto_sync_enabled", "read_unread_only", "smtp_enabled"}


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


def _save_mailbox(db: Session, mailbox: Mailbox, data: dict[str, str], user: TenantUser) -> None:
    normalized = {key: data[key] for key in EDIT_FIELDS if key in data}
    update_with_form(mailbox, normalized, SECRET_FIELDS)
    mailbox.name = (mailbox.name or mailbox.email_address or "Buzón de correo").strip()[:150]
    mailbox.email_address = (data.get("email_address") or mailbox.email_address or mailbox.connected_email or mailbox.imap_username or "").strip().lower()
    mailbox.connected_email = (data.get("connected_email") or mailbox.email_address or "").strip() or None
    mailbox.inbox_folder = (mailbox.inbox_folder or mailbox.mailbox or "INBOX").strip() or "INBOX"
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
        {"ok": True, "message": "Configuración IMAP guardada correctamente.", "mailbox": serialize_mailbox(mailbox)},
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
    job = enqueue_job(db, company_id=user.company_id, job_type="email_sync", payload={"mailbox_id": mailbox.id, "auto_process": False, "unread_only": mailbox.read_unread_only, "limit": mailbox.read_limit}, created_by_user_id=user.id)
    return _response(request, {"ok": True, "job_id": job.id, "status": job.status, "mailbox_id": mailbox.id})


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
    try:
        requested_limit = max(min(int(data.get("limit") or 100), 100), 1)
    except (TypeError, ValueError):
        return _response(request, {"ok": False, "message": "El límite de backfill no es válido."}, status_code=400)
    payload = {
        "mailbox_id": mailbox.id,
        "from_date": data.get("from_date") or mailbox.read_from_date,
        "to_date": data.get("to_date") or None,
        "limit": requested_limit,
    }
    job = enqueue_job(db, company_id=user.company_id, job_type="backfill_imap", payload=payload, created_by_user_id=user.id)
    return _response(request, {"ok": True, "job_id": job.id, "status": job.status, "mailbox_id": mailbox.id})


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
