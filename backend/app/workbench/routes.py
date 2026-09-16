from __future__ import annotations

import logging
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.agent.extraction.diagnostics import extraction_diagnostics_from_messages, extraction_diagnostics_from_payload
from app.agent.services import AgentProcessingService, ScoringService
from app.auth.dependencies import current_user
from app.core.attachment_storage import read_attachment
from app.core.channel_identity import channel_label, inbound_channel_key, is_whatsapp_provider
from app.core.entry_workflow import close_email, discard_email, mark_email_no_order, queue_email_processing
from app.core.templating import templates
from app.core.timezones import format_local_datetime
from app.dashboard.service import workbench_summary
from app.db.models import Alert, Conversation, Email, EmailAttachment, EmailSettings, ExportFile, FTPSettings, InboundMessage, Order, ScoringSettings, utcnow
from app.jobs.service import enqueue_job, execute_job_inline
from app.logs.service import log_action
from app.master.service import TenantUser
from app.orders.service import _customer_label, _sync_customer_product_knowledge, validate_confirmation
from app.settings.service import get_or_create_settings
from app.tenancy.database import get_tenant_db
from app.agent.platform import LearningService
from app.db.models import Customer, Product, OrderLine
from app.dashboard.service import email_workbench_item, order_workbench_item
from app.workers.jobs_worker import is_job_worker_started

router = APIRouter()
logger = logging.getLogger(__name__)


def _redirect_back(request: Request, fallback: str = "/"):
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _run_job_inline_if_needed(request: Request, db: Session, user: TenantUser, job, *, action: str, message: str, fallback: str = "/") -> dict | None:  # noqa: ANN001
    if os.getenv("VERCEL") != "1" and is_job_worker_started():
        return None
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "workbench.job.inline.start",
        extra={"event": "workbench.job.inline.start", "request_id": request_id, "company_id": user.company_id, "user_id": user.id, "job_id": job.id, "job_type": job.job_type},
    )
    result = execute_job_inline(db, job)
    log_action(db, company_id=user.company_id, user=user, action=action, entity_type="job", entity_id=job.id, message=result.get("message") or message)
    logger.info(
        "workbench.job.inline.end",
        extra={
            "event": "workbench.job.inline.end",
            "request_id": request_id,
            "company_id": user.company_id,
            "user_id": user.id,
            "job_id": job.id,
            "job_type": job.job_type,
            "ok": bool(result.get("ok")),
            "found": result.get("found"),
            "saved": result.get("saved"),
            "duplicates": result.get("duplicates"),
            "errors": result.get("errors"),
        },
    )
    return result


def _parse_selected_items(payload: str) -> list[dict]:
    try:
        data = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)]


def _conversation_preview(source) -> dict | None:
    messages = source
    if hasattr(source, "conversation"):
        conversation = getattr(source, "conversation", None)
        messages = conversation.messages if conversation and conversation.messages else []
    ordered_messages = sorted(messages or [], key=lambda item: item.received_at or item.created_at)
    rendered_messages: list[dict[str, object]] = []
    use_transcript_payload = len(ordered_messages) <= 1
    for message in ordered_messages:
        parsed_payload = {}
        if use_transcript_payload and getattr(message, "raw_payload_json", None):
            try:
                parsed_payload = json.loads(message.raw_payload_json)
            except json.JSONDecodeError:
                parsed_payload = {}
        parsed_messages = []
        if use_transcript_payload and parsed_payload.get("import_type") == "manual_whatsapp":
            parsed = parsed_payload.get("parsed")
            if isinstance(parsed, dict):
                parsed_messages = parsed.get("messages", []) or []
        if parsed_messages:
            for parsed_message in parsed_messages:
                rendered_messages.append(
                    {
                        "sender": parsed_message.get("sender") or message.sender or "Cliente",
                        "direction": parsed_message.get("direction") or "inbound",
                        "role_label": "Empresa" if (parsed_message.get("direction") or "inbound") == "outbound" else "Cliente",
                        "text": parsed_message.get("text") or "",
                        "timestamp_label": parsed_message.get("timestamp_label") or "",
                    }
                )
        else:
            rendered_messages.append(
                {
                    "sender": message.sender or ("Empresa" if getattr(message, "direction", "inbound") == "outbound" else "Cliente"),
                    "direction": getattr(message, "direction", "inbound") or "inbound",
                    "role_label": "Empresa" if (getattr(message, "direction", "inbound") or "inbound") == "outbound" else "Cliente",
                    "text": message.original_content or message.normalized_text or "",
                    "timestamp_label": format_local_datetime(message.received_at, fmt="%d/%m/%Y %H:%M", default=""),
                }
            )
    if not rendered_messages:
        return None
    source_message = ordered_messages[0]
    provider = (getattr(source_message, "provider", "") or "").strip().lower()
    if provider == "manual_import":
        provider_label = "Importación manual"
    elif is_whatsapp_provider(provider):
        provider_label = "WhatsApp"
    elif provider:
        provider_label = provider.title()
    else:
        provider_label = "Conversación"
    return {"provider_label": provider_label, "messages": rendered_messages}


def _inbound_agent_status(message: InboundMessage) -> str:
    status = (message.status or "").strip().lower()
    if status in {"received", "queued", "processing"}:
        return "processing" if status != "received" else "not_processed"
    if status in {"matched", "order_detected"}:
        return "order_detected"
    if status in {"doubtful", "no_order"}:
        return "doubtful" if status == "doubtful" else "no_order"
    if status == "error":
        return "error"
    if message.order_id:
        return "order_detected"
    return "processed" if status == "processed" else "not_processed"


def _inbound_item(message: InboundMessage, *, order: Order | None = None) -> dict:
    status_key = _inbound_agent_status(message)
    channel_key = inbound_channel_key(message)
    provider_label = channel_label(channel_key)
    source_label = "Conversación" if channel_key == "whatsapp" else provider_label
    customer_name = "Cliente no identificado"
    if order and (order.validated_customer or order.customer):
        customer_name = (order.validated_customer or order.customer).fiscal_name
    elif order and order.customer_detected_name:
        customer_name = order.customer_detected_name
    elif message.customer_id:
        customer_name = customer_name
    return {
        "id": f"inbound-{message.id}",
        "kind": "inbound",
        "message_id": message.id,
        "order_id": order.id if order else message.order_id,
        "customer_name": customer_name,
        "provider_label": provider_label,
        "channel_key": channel_key,
        "source_label": source_label,
        "agent_status": status_key,
        "agent_status_label": {
            "not_processed": "Pendiente de analizar",
            "processing": "Procesando",
            "order_detected": "Pedido detectado",
            "doubtful": "Necesita revisión",
            "no_order": "Sin pedido detectado",
            "error": "Error",
            "processed": "Analizado",
        }.get(status_key, status_key),
        "status_label": {
            "received": "Recibido",
            "queued": "En cola",
            "processing": "Procesando",
            "matched": "Coincidente",
            "order_detected": "Pedido detectado",
            "doubtful": "Necesita revisión",
            "no_order": "Sin pedido",
            "error": "Error",
            "processed": "Procesado",
        }.get((message.status or "").strip().lower(), message.status or "Recibido"),
        "score": message.score,
        "message_count": len(message.conversation.messages) if message.conversation and message.conversation.messages else 1,
        "has_attachments": bool(message.attachments),
        "origin": "WhatsApp" if channel_key == "whatsapp" else "Entrada",
    }


def _queued_job_response(request: Request, job_id: int, fallback: str = "/", result: dict | None = None):
    if "application/json" in (request.headers.get("accept") or ""):
        payload = {"ok": True, "job_id": job_id, "status": "queued", "message": "Trabajo encolado correctamente"}
        if result is not None:
            payload["status"] = "success" if result.get("ok") else "failed"
            payload["result"] = result
            payload["message"] = result.get("message") or payload["message"]
        return JSONResponse(payload)
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


@router.get("/workbench")
def workbench_endpoint(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    payload = workbench_summary(db, user.company_id, dict(request.query_params))
    payload.pop("orders", None)
    payload.pop("all_items", None)
    return JSONResponse(jsonable_encoder(payload))


@router.get("/workbench/item/{kind}/{item_id}/detail")
def workbench_item_detail(kind: str, item_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if kind == "order":
        order = db.scalar(
            select(Order)
            .where(Order.id == item_id, Order.company_id == user.company_id)
            .options(
                selectinload(Order.lines).selectinload(OrderLine.product),
                selectinload(Order.lines).selectinload(OrderLine.validated_product),
                selectinload(Order.email).selectinload(Email.attachments),
                selectinload(Order.customer),
                selectinload(Order.validated_customer),
                selectinload(Order.conversation).selectinload(Conversation.messages),
            )
        )
        if not order:
            return PlainTextResponse("No encontrado", status_code=404)
        conversation_preview = _conversation_preview(order)
        customers = db.scalars(select(Customer).where(Customer.company_id == user.company_id).order_by(Customer.fiscal_name)).all()
        products = db.scalars(select(Product).where(Product.company_id == user.company_id).order_by(Product.reference)).all()
        item = order_workbench_item(order, get_or_create_settings(db, ScoringSettings, user.company_id))
        extraction_diagnostics = extraction_diagnostics_from_messages(order.conversation.messages if order.conversation else [])
        return templates.TemplateResponse(
            "workbench/detail.html",
            {"request": request, "user": user, "kind": "order", "order": order, "item": item, "conversation_preview": conversation_preview, "customers": customers, "products": products, "extraction_diagnostics": extraction_diagnostics},
        )
    if kind == "email":
        email = db.scalar(
            select(Email)
            .where(Email.id == item_id, Email.company_id == user.company_id)
            .options(selectinload(Email.attachments))
        )
        if not email:
            return PlainTextResponse("No encontrado", status_code=404)
        item = email_workbench_item(email)
        return templates.TemplateResponse(
            "workbench/detail.html",
            {"request": request, "user": user, "kind": "email", "email": email, "item": item},
        )
    if kind == "inbound":
        inbound = db.scalar(
            select(InboundMessage)
            .where(InboundMessage.id == item_id, InboundMessage.company_id == user.company_id)
            .options(
                selectinload(InboundMessage.attachments),
                selectinload(InboundMessage.conversation).selectinload(Conversation.messages),
            )
        )
        if not inbound:
            return PlainTextResponse("No encontrado", status_code=404)
        order = None
        if inbound.order_id:
            order = db.scalar(
                select(Order)
                .where(Order.id == inbound.order_id, Order.company_id == user.company_id)
                .options(
                    selectinload(Order.lines).selectinload(OrderLine.product),
                    selectinload(Order.lines).selectinload(OrderLine.validated_product),
                    selectinload(Order.customer),
                    selectinload(Order.validated_customer),
                    selectinload(Order.conversation).selectinload(Conversation.messages),
                )
            )
        preview = _conversation_preview(inbound.conversation.messages if inbound.conversation else [inbound])
        item = _inbound_item(inbound, order=order)
        customers = db.scalars(select(Customer).where(Customer.company_id == user.company_id).order_by(Customer.fiscal_name)).all()
        products = db.scalars(select(Product).where(Product.company_id == user.company_id).order_by(Product.reference)).all()
        extraction_diagnostics = extraction_diagnostics_from_payload(inbound.extraction_json)
        return templates.TemplateResponse(
            "workbench/detail.html",
            {
                "request": request,
                "user": user,
                "kind": "inbound",
                "inbound_message": inbound,
                "order": order,
                "conversation_preview": preview,
                "item": item,
                "customers": customers,
                "products": products,
                "extraction_diagnostics": extraction_diagnostics,
            },
        )
    return PlainTextResponse("Tipo no soportado", status_code=400)


@router.post("/workbench/read-email")
def workbench_read_email(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "workbench.email.read.start",
        extra={"event": "workbench.email.read.start", "request_id": request_id, "company_id": user.company_id, "user_id": user.id},
    )
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    safe_limit = max(min(int(settings.read_limit or 10), 50), 1)
    job = enqueue_job(db, company_id=user.company_id, job_type="email_sync", payload={"auto_process": False, "unread_only": False, "limit": safe_limit}, created_by_user_id=user.id)
    logger.info(
        "workbench.email.read.requested",
        extra={"event": "workbench.email.read.requested", "request_id": request_id, "company_id": user.company_id, "job_id": job.id, "job_type": job.job_type},
    )
    result = execute_job_inline(db, job)
    log_action(db, company_id=user.company_id, user=user, action="workbench.email.read.inline", entity_type="job", entity_id=job.id, message=result.get("message") or "Lectura IMAP completada")
    return _queued_job_response(request, job.id, result=result)


@router.post("/workbench/process-pending")
def workbench_process_pending(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    job = enqueue_job(db, company_id=user.company_id, job_type="process_pending_emails", payload={}, created_by_user_id=user.id)
    result = _run_job_inline_if_needed(request, db, user, job, action="workbench.process_pending.inline", message="Procesamiento de pendientes completado")
    if result is not None:
        return _queued_job_response(request, job.id, result=result)
    log_action(db, company_id=user.company_id, user=user, action="workbench.process_pending", entity_type="job", entity_id=job.id, message="Procesamiento de pendientes encolado")
    return _queued_job_response(request, job.id)


@router.post("/workbench/process-recent")
def workbench_process_recent(request: Request, limit: int = Form(3), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    safe_limit = max(min(int(limit or 3), 10), 1)
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "workbench.process_recent.start",
        extra={"event": "workbench.process_recent.start", "request_id": request_id, "company_id": user.company_id, "user_id": user.id, "limit": safe_limit},
    )
    job = enqueue_job(db, company_id=user.company_id, job_type="process_recent_emails", payload={"limit": safe_limit}, created_by_user_id=user.id)
    logger.info(
        "workbench.process_recent.queued",
        extra={"event": "workbench.process_recent.queued", "request_id": request_id, "company_id": user.company_id, "job_id": job.id, "job_type": job.job_type, "limit": safe_limit},
    )
    result = _run_job_inline_if_needed(request, db, user, job, action="workbench.process_recent.inline", message=f"Procesamiento IMAP completado: {safe_limit} correos")
    if result is not None:
        return _queued_job_response(request, job.id, result=result)
    log_action(db, company_id=user.company_id, user=user, action="workbench.process_recent", entity_type="job", entity_id=job.id, message=f"Procesamiento de emergencia IMAP encolado: {safe_limit} correos")
    return _queued_job_response(request, job.id)


@router.post("/workbench/bulk-action")
def workbench_bulk_action(
    request: Request,
    action: str = Form(...),
    selected_items: str = Form("[]"),
    target_state: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    items = _parse_selected_items(selected_items)
    if not items:
        return _redirect_back(request)
    job = enqueue_job(
        db,
        company_id=user.company_id,
        job_type="bulk_order_action",
        payload={"action": action, "selected_items": items, "target_state": target_state},
        created_by_user_id=user.id,
    )
    log_action(db, company_id=user.company_id, user=user, action="workbench.bulk_action", entity_type="job", entity_id=job.id, message=f"Accion masiva encolada: {action}")
    return _queued_job_response(request, job.id)


@router.post("/workbench/email/{email_id}/mark-no-order")
def workbench_mark_email_no_order(email_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    email = db.get(Email, email_id)
    if email and email.company_id == user.company_id:
        mark_email_no_order(db, company_id=user.company_id, user_id=user.id, email_id=email.id)
        log_action(db, company_id=user.company_id, user=user, action="workbench.email.mark_no_order", entity_type="email", entity_id=email.id, message="Correo marcado como no pedido desde Bandeja")
    return RedirectResponse("/?mode=no_order", status_code=303)


@router.post("/workbench/email/{email_id}/process")
def workbench_process_email(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    job = queue_email_processing(db, company_id=user.company_id, user_id=user.id, email_id=email_id)
    log_action(db, company_id=user.company_id, user=user, action="workbench.email.process", entity_type="job", entity_id=job.id, message=f"Correo encolado para procesar: {email_id}")
    return _queued_job_response(request, job.id)


@router.post("/workbench/email/{email_id}/close")
def workbench_close_email(email_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    email = db.get(Email, email_id)
    if email and email.company_id == user.company_id:
        close_email(db, company_id=user.company_id, user_id=user.id, email_id=email.id)
        log_action(db, company_id=user.company_id, user=user, action="workbench.email.close", entity_type="email", entity_id=email.id, message="Correo cerrado desde Bandeja")
    return RedirectResponse("/", status_code=303)


@router.post("/workbench/email/{email_id}/discard")
def workbench_discard_email(email_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    email = db.get(Email, email_id)
    if email and email.company_id == user.company_id:
        discard_email(db, company_id=user.company_id, user_id=user.id, email_id=email.id)
        log_action(db, company_id=user.company_id, user=user, action="workbench.email.discard", entity_type="email", entity_id=email.id, message="Correo descartado desde Bandeja")
    return RedirectResponse("/?mode=no_order", status_code=303)


def _workbench_email_attachment_payload(
    db: Session,
    *,
    email_id: int,
    attachment_id: int,
    company_id: int,
) -> tuple[EmailAttachment, bytes] | PlainTextResponse:
    attachment = db.get(EmailAttachment, attachment_id)
    if (
        not attachment
        or attachment.company_id != company_id
        or attachment.email_id != email_id
    ):
        return PlainTextResponse("No encontrado", status_code=404)

    try:
        content = read_attachment(attachment.storage_path or "", tenant_id=attachment.company_id)
    except Exception:
        return PlainTextResponse("Archivo no disponible", status_code=404)

    return attachment, content


@router.get("/workbench/email/{email_id}/attachments/{attachment_id}")
def workbench_email_attachment(
    email_id: int,
    attachment_id: int,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    payload = _workbench_email_attachment_payload(
        db,
        email_id=email_id,
        attachment_id=attachment_id,
        company_id=user.company_id,
    )
    if isinstance(payload, PlainTextResponse):
        return payload
    attachment, content = payload

    return Response(
        content=content,
        media_type=attachment.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{attachment.filename}"'
        },
    )


@router.get("/workbench/email/{email_id}/attachments/{attachment_id}/preview")
def workbench_email_attachment_preview(
    email_id: int,
    attachment_id: int,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    payload = _workbench_email_attachment_payload(
        db,
        email_id=email_id,
        attachment_id=attachment_id,
        company_id=user.company_id,
    )
    if isinstance(payload, PlainTextResponse):
        return payload
    attachment, content = payload

    media_type = (
        "application/pdf"
        if attachment.is_pdf
        else attachment.content_type or "application/octet-stream"
    )

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{attachment.filename}"'
        },
    )
