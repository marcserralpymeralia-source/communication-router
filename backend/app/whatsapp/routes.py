from __future__ import annotations

import hmac
import json
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.config import get_settings
from app.master.database import get_master_db
from app.master.service import TenantUser
from app.tenancy.database import tenant_db_session
from app.tenancy.database import get_tenant_db
from app.whatsapp.service import (
    enqueue_whatsapp_media_download,
    enqueue_whatsapp_processing,
    parse_payload_events,
    persist_event,
    resolve_company_from_slug,
    resolve_company_from_whatsapp_identifiers,
    whatsapp_event_matches_config,
    whatsapp_event_has_processable_content,
    whatsapp_event_requires_media_download,
    whatsapp_ingress_is_ready,
    verify_signature,
    verify_webhook_token,
    whatsapp_config,
    send_manual_response,
)

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])


@router.get("")
def verify_default_webhook(request: Request):
    settings = get_settings()
    challenge = request.query_params.get("hub.challenge")
    verify_token = request.query_params.get("hub.verify_token")
    mode = request.query_params.get("hub.mode")
    if mode != "subscribe":
        return PlainTextResponse("forbidden", status_code=403)
    if not settings.meta_whatsapp_verify_token or not verify_token:
        return PlainTextResponse("forbidden", status_code=403)
    if not hmac.compare_digest(settings.meta_whatsapp_verify_token, verify_token):
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(challenge or "ok")


@router.post("")
async def receive_default_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    master_db: Session = Depends(get_master_db),
):
    raw_body = await request.body()
    if not verify_signature(get_settings().meta_app_secret, raw_body, x_hub_signature_256):
        return JSONResponse({"ok": False, "message": "invalid signature"}, status_code=403)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "message": "invalid json"}, status_code=400)
    events = parse_payload_events(payload if isinstance(payload, dict) else {})
    stored: list[int] = []
    ignored = 0
    for event in events:
        company, tenant_db = resolve_company_from_whatsapp_identifiers(
            master_db,
            business_account_id=event.get("business_account_id"),
            phone_number_id=event.get("phone_number_id"),
        )
        if not company or not tenant_db:
            ignored += 1
            continue
        config_db = tenant_db_session(tenant_db.get_database_url())()
        try:
            config = whatsapp_config(config_db, company.id)
            if event.get("kind") != "account_update" and not whatsapp_ingress_is_ready(config_db, company.id, config=config):
                ignored += 1
                continue
            if not whatsapp_event_matches_config(event, config):
                ignored += 1
                continue
            message = persist_event(config_db, company.id, event)
            if message:
                stored.append(message.id)
                if event.get("kind") == "message":
                    if whatsapp_event_requires_media_download(event):
                        enqueue_whatsapp_media_download(config_db, company.id, message.id)
                    elif whatsapp_event_has_processable_content(event):
                        enqueue_whatsapp_processing(config_db, company.id, message.id)
                    else:
                        message.processing_step = "received_without_processable_content"
                        message.processing_error = "El mensaje no contiene texto procesable ni un adjunto compatible."
            config_db.commit()
        finally:
            config_db.close()
    return {"ok": True, "events": len(events), "stored": stored, "ignored": ignored}


@router.get("/{company_slug}")
def verify_webhook(company_slug: str, request: Request, master_db: Session = Depends(get_master_db)):
    company, tenant_db = resolve_company_from_slug(master_db, company_slug)
    if not company or not tenant_db:
        return PlainTextResponse("unknown tenant", status_code=404)
    config_db = tenant_db_session(tenant_db.get_database_url())()
    try:
        config = whatsapp_config(config_db, company.id)
    finally:
        config_db.close()
    challenge = request.query_params.get("hub.challenge")
    verify_token = request.query_params.get("hub.verify_token") or request.query_params.get("verify_token")
    mode = request.query_params.get("hub.mode") or request.query_params.get("mode")
    if mode and mode not in {"subscribe", "whatsapp"}:
        return PlainTextResponse("forbidden", status_code=403)
    if not verify_webhook_token(config, verify_token):
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(challenge or "ok")


@router.post("/{company_slug}")
async def receive_webhook(
    company_slug: str,
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    master_db: Session = Depends(get_master_db),
):
    company, tenant_db = resolve_company_from_slug(master_db, company_slug)
    if not company or not tenant_db:
        return JSONResponse({"ok": False, "message": "tenant not found"}, status_code=404)
    raw_body = await request.body()
    config_db = tenant_db_session(tenant_db.get_database_url())()
    try:
        config = whatsapp_config(config_db, company.id)
        if not verify_signature(get_settings().meta_app_secret, raw_body, x_hub_signature_256):
            return JSONResponse({"ok": False, "message": "invalid signature"}, status_code=403)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "message": "invalid json"}, status_code=400)
        events = parse_payload_events(payload if isinstance(payload, dict) else {})
        if not events:
            return JSONResponse({"ok": True, "message": "no events"})
        stored = []
        ignored = 0
        ingress_ready = whatsapp_ingress_is_ready(config_db, company.id, config=config)
        if not any(event.get("kind") == "account_update" for event in events) and not ingress_ready:
            return {"ok": True, "company_id": company.id, "events": len(events), "stored": [], "ignored": len(events)}
        for event in events:
            if event.get("kind") != "account_update" and not ingress_ready:
                ignored += 1
                continue
            if not whatsapp_event_matches_config(event, config):
                ignored += 1
                continue
            message = persist_event(config_db, company.id, event)
            stored.append(message.id if message else None)
            if message and event.get("kind") == "message":
                if whatsapp_event_requires_media_download(event):
                    enqueue_whatsapp_media_download(config_db, company.id, message.id)
                elif whatsapp_event_has_processable_content(event):
                    enqueue_whatsapp_processing(config_db, company.id, message.id)
                else:
                    message.processing_step = "received_without_processable_content"
                    message.processing_error = "El mensaje no contiene texto procesable ni un adjunto compatible."
        config_db.commit()
        return {"ok": True, "company_id": company.id, "events": len(events), "stored": [item for item in stored if item is not None], "ignored": ignored}
    finally:
        config_db.close()


@router.post("/{company_slug}/respond")
async def manual_response(
    company_slug: str,
    conversation_id: int,
    body: str = "",
    template_name: str | None = None,
    template_language: str | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
    master_db: Session = Depends(get_master_db),
):
    company, tenant_db = resolve_company_from_slug(master_db, company_slug)
    if not company or not tenant_db or company.id != user.company_id:
        return JSONResponse({"ok": False, "message": "tenant not found"}, status_code=404)
    try:
        message = await send_manual_response(db, company_id=company.id, conversation_id=conversation_id, body=body, user_id=user.id, idempotency_key=idempotency_key, template_name=template_name, template_language=template_language)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=404)
    except Exception as exc:  # noqa: BLE001
        error_type = getattr(exc, "error_type", "whatsapp_send_failed")
        status_code = 400 if error_type in {"invalid_message", "recipient_not_found", "response_window_expired", "server_not_configured"} else 409 if error_type in {"send_in_progress", "send_unknown"} else 502
        return JSONResponse({"ok": False, "message": str(exc), "error_type": error_type}, status_code=status_code)
    return {"ok": True, "message_id": message.id, "status": message.status}
