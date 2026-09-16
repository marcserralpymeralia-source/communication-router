from __future__ import annotations

from dataclasses import dataclass
from html import escape
from io import BytesIO

import pandas as pd
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.attachment_storage import read_attachment
from app.master.service import TenantUser
from app.db.models import Email, EmailAttachment, EmailSettings, InboundMessage, MessageAttachment, Order
from app.jobs.service import enqueue_job, execute_job_inline
from app.logs.service import log_action
from app.settings.service import get_or_create_settings
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/channels", tags=["channels"])
entries_router = APIRouter(tags=["entries"])


@dataclass(slots=True)
class ResolutionDestination:
    state: str
    source_kind: str
    source_id: int
    order_id: int | None
    message_id: int | None
    conversation_id: int | None
    redirect_url: str
    source_label: str











def _attachment_kind(filename: str, content_type: str | None, *, is_pdf: bool = False, is_image: bool = False, is_audio: bool = False) -> str:
    lowered = (filename or "").lower()
    content_type = (content_type or "").lower()
    if is_pdf or content_type == "application/pdf" or lowered.endswith(".pdf"):
        return "pdf"
    if is_image or content_type.startswith("image/"):
        return "image"
    if is_audio or content_type.startswith("audio/"):
        return "audio"
    if lowered.endswith(".csv") or content_type in {"text/csv", "application/csv"}:
        return "csv"
    if lowered.endswith((".xls", ".xlsx", ".ods")) or content_type in {
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.spreadsheet",
    }:
        return "sheet"
    if content_type.startswith("text/") or lowered.endswith((".txt", ".log", ".md", ".json", ".eml")):
        return "text"
    if lowered.endswith((".doc", ".docx")):
        return "doc"
    return "unsupported"


















def _resolution_destination_for_source(db: Session, user: TenantUser, source_kind: str, source_id: int) -> ResolutionDestination | None:
    if source_kind == "email":
        source = db.get(Email, source_id)
        if not source or source.company_id != user.company_id:
            return None
        order = db.scalar(select(Order).where(Order.company_id == user.company_id, Order.email_id == source.id))
        if order:
            return ResolutionDestination("order", source_kind, source.id, order.id, None, None, f"/orders/{order.id}", "Pedido")
        if source.agent_status == "processing_error" or (source.status or "").startswith("error"):
            return ResolutionDestination("error", source_kind, source.id, None, None, None, f"/workbench/item/email/{source.id}/detail", "Error")
        if source.agent_status in {"processed_order_detected", "processed_doubtful", "processed_no_order", "processed"}:
            return ResolutionDestination("proposal", source_kind, source.id, None, None, source.conversation_id, f"/workbench/item/email/{source.id}/detail", "Propuesta")
        if source.agent_status in {"processing", "pending_reprocess"} or source.status == "pending":
            return ResolutionDestination("processing", source_kind, source.id, None, None, source.conversation_id, f"/workbench/item/email/{source.id}/detail", "En proceso")
        return ResolutionDestination("unprocessed", source_kind, source.id, None, None, source.conversation_id, f"/workbench/item/email/{source.id}/detail", "Pendiente")

    source = db.get(InboundMessage, source_id)
    if not source or source.company_id != user.company_id:
        return None
    order = db.get(Order, source.order_id) if source.order_id else None
    if order:
        return ResolutionDestination("order", source_kind, source.id, order.id, source.id, source.conversation_id, f"/orders/{order.id}", "Pedido")
    inbound_focus = f"/?focus=inbound-{source.id}"
    if source.status == "error":
        return ResolutionDestination("error", source_kind, source.id, None, source.id, source.conversation_id, inbound_focus, "Error")
    if source.status in {"processing", "queued"}:
        return ResolutionDestination("processing", source_kind, source.id, None, source.id, source.conversation_id, inbound_focus, "En proceso")
    if source.status in {"doubtful", "no_order", "processed", "matched", "order_detected"}:
        return ResolutionDestination("proposal", source_kind, source.id, None, source.id, source.conversation_id, inbound_focus, "Propuesta")
    return ResolutionDestination("unprocessed", source_kind, source.id, None, source.id, source.conversation_id, inbound_focus, "Pendiente")














def _parse_entry_id(entry_id: str) -> tuple[str, int] | None:
    if "-" not in entry_id:
        if entry_id.isdigit():
            return "email", int(entry_id)
        return None
    source_kind, raw_id = entry_id.split("-", 1)
    if source_kind not in {"email", "inbound"} or not raw_id.isdigit():
        return None
    return source_kind, int(raw_id)


def _resolve_channel_entry_response(db: Session, user: TenantUser, source_kind: str, source_id: int) -> RedirectResponse | PlainTextResponse:
    destination = _resolution_destination_for_source(db, user, source_kind, source_id)
    if not destination:
        return PlainTextResponse("No encontrado", status_code=404)
    log_action(
        db,
        company_id=user.company_id,
        user=user,
        action=f"channel.resolve.{destination.source_kind}",
        entity_type="channel_entry",
        entity_id=destination.source_id,
        message=f"Abrir resolucion: {destination.state}",
    )
    db.commit()
    return RedirectResponse(destination.redirect_url, status_code=303)


def _resolve_entry_review_response(request: Request, db: Session, user: TenantUser, source_kind: str, source_id: int):
    destination = _resolution_destination_for_source(db, user, source_kind, source_id)
    if not destination:
        return PlainTextResponse("No encontrado", status_code=404)
    if destination.order_id:
        from app.orders.routes import order_detail

        log_action(
            db,
            company_id=user.company_id,
            user=user,
            action=f"entry.resolve.{destination.source_kind}",
            entity_type="channel_entry",
            entity_id=destination.source_id,
            message="Revision de pedido abierta desde Entradas",
        )
        db.commit()
        return order_detail(destination.order_id, request, db=db, user=user)
    return channels_page(request)


def _process_channel_entry_response(db: Session, user: TenantUser, source_kind: str, source_id: int) -> RedirectResponse | PlainTextResponse:
    destination = _resolution_destination_for_source(db, user, source_kind, source_id)
    if not destination:
        return PlainTextResponse("No encontrado", status_code=404)
    if source_kind == "email":
        source = db.get(Email, source_id)
        assert source is not None
        order = db.scalar(select(Order).where(Order.company_id == user.company_id, Order.email_id == source.id))
        if order:
            return RedirectResponse(f"/orders/{order.id}", status_code=303)
        job = enqueue_job(db, company_id=user.company_id, job_type="process_email", payload={"email_id": source.id}, created_by_user_id=user.id)
        result = execute_job_inline(db, job)
        log_action(
            db,
            company_id=user.company_id,
            user=user,
            action="channel.process.email",
            entity_type="job",
            entity_id=job.id,
            message=result.get("message") or f"Procesamiento completado para email {source.id}",
        )
        return RedirectResponse(destination.redirect_url, status_code=303)

    source = db.get(InboundMessage, source_id)
    assert source is not None
    order = db.get(Order, source.order_id) if source.order_id else None
    if order:
        return RedirectResponse(f"/orders/{order.id}", status_code=303)
    if source.status in {"received", "queued", "processing", "doubtful", "error", "no_order"}:
        job = enqueue_job(
            db,
            company_id=user.company_id,
            job_type="process_inbound_message",
            payload={"inbound_message_id": source.id, "channel": source_kind, "source": source.provider or "manual_import"},
            created_by_user_id=user.id,
        )
        log_action(db, company_id=user.company_id, user=user, action="channel.process.inbound", entity_type="job", entity_id=job.id, message=f"Procesamiento encolado para entrada {source.id}")
        db.commit()
    return RedirectResponse(destination.redirect_url, status_code=303)


@router.get("/{source_kind}/{source_id}/resolve")
def resolve_channel_entry(
    source_kind: str,
    source_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    return _resolve_channel_entry_response(db, user, source_kind, source_id)


@router.post("/{source_kind}/{source_id}/process")
def process_channel_entry(
    source_kind: str,
    source_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    return _process_channel_entry_response(db, user, source_kind, source_id)


@router.post("/{source_kind}/{source_id}/resolve")
def legacy_resolve_channel_entry(
    source_kind: str,
    source_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    return _resolve_channel_entry_response(db, user, source_kind, source_id)


@entries_router.get("/entries/{entry_id}")
def entry_detail(entry_id: str, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    parsed = _parse_entry_id(entry_id)
    if not parsed:
        return PlainTextResponse("No encontrado", status_code=404)
    source_kind, source_id = parsed
    return _resolve_entry_review_response(request, db, user, source_kind, source_id)


@entries_router.get("/entries/{entry_id}/resolve")
def resolve_entry(entry_id: str, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    parsed = _parse_entry_id(entry_id)
    if not parsed:
        return PlainTextResponse("No encontrado", status_code=404)
    source_kind, source_id = parsed
    return _resolve_entry_review_response(request, db, user, source_kind, source_id)


@entries_router.post("/entries/{entry_id}/process")
def process_entry(entry_id: str, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    parsed = _parse_entry_id(entry_id)
    if not parsed:
        return PlainTextResponse("No encontrado", status_code=404)
    source_kind, source_id = parsed
    return _process_channel_entry_response(db, user, source_kind, source_id)


@entries_router.post("/entries/sync")
def sync_entries(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    safe_limit = max(min(int(settings.read_limit or 10), 50), 1)
    job = enqueue_job(
        db,
        company_id=user.company_id,
        job_type="email_sync",
        payload={"auto_process": False, "unread_only": False, "limit": safe_limit},
        created_by_user_id=user.id,
    )
    result = execute_job_inline(db, job)
    log_action(
        db,
        company_id=user.company_id,
        user=user,
        action="entries.sync.inline",
        entity_type="job",
        entity_id=job.id,
        message=result.get("message") or "Sincronizacion IMAP ejecutada desde Entradas",
    )
    status = "success" if result.get("ok") else "error"
    return RedirectResponse(f"/?sync={status}", status_code=303)




@router.get("")
def channels_legacy_redirect(request: Request):
    target = "/" if not request.url.query else f"/?{request.url.query}"
    return RedirectResponse(target, status_code=303)


@entries_router.get("/entries")
def channels_page(
    request: Request,
    user: TenantUser = Depends(current_user),
):
    target = "/" if not request.url.query else f"/?{request.url.query}"
    return RedirectResponse(target, status_code=303)



def _preview_html(title: str, body: str, download_href: str) -> str:
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:12px;background:#f7f9f8;color:#172026;font-size:12px;line-height:1.35}}
.wrap{{display:grid;gap:10px}}
.head{{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:10px 11px;border:1px solid #dbe2e6;border-radius:8px;background:#fff}}
.head strong{{font-size:14px}}
.muted{{color:#63717b}}
.table-wrap{{overflow:auto;border:1px solid #dbe2e6;border-radius:8px;background:#fff}}
table{{border-collapse:collapse;width:100%;font-size:11px}}
th,td{{padding:6px 8px;border-bottom:1px solid #dbe2e6;text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:#eef3f4}}
pre{{margin:0;white-space:pre-wrap;word-break:break-word;padding:10px 11px;border:1px solid #dbe2e6;border-radius:8px;background:#fff}}
a.button{{display:inline-flex;align-items:center;justify-content:center;padding:6px 10px;border-radius:6px;background:#0f766e;color:#fff;text-decoration:none;font-weight:700}}
</style></head>
<body><div class="wrap">
<div class="head"><div><strong>{escape(title)}</strong><div class="muted">Vista rápida integrada</div></div><a class="button" href="{escape(download_href)}" target="_blank">Descargar</a></div>
{body}
</div></body></html>"""


@router.get("/{source_kind}/{source_id}/attachments/{attachment_id}/preview")
def preview_attachment(
    source_kind: str,
    source_id: int,
    attachment_id: int,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    attachment = None
    title = "Adjunto"
    download_href = f"/channels/{source_kind}/{source_id}/attachments/{attachment_id}"

    if source_kind == "email":
        source = db.get(Email, source_id)
        attachment = db.get(EmailAttachment, attachment_id)
        title = attachment.filename if attachment else title
        if (
            not source
            or not attachment
            or source.company_id != user.company_id
            or attachment.email_id != source.id
        ):
            return PlainTextResponse("No encontrado", status_code=404)
    else:
        source = db.get(InboundMessage, source_id)
        attachment = db.get(MessageAttachment, attachment_id)
        title = attachment.filename if attachment else title
        if (
            not source
            or not attachment
            or source.company_id != user.company_id
            or attachment.inbound_message_id != source.id
        ):
            return PlainTextResponse("No encontrado", status_code=404)

    try:
        content = read_attachment(attachment.storage_path or "", tenant_id=attachment.company_id)
    except Exception:
        return HTMLResponse(
            _preview_html(
                title,
                "<p class='muted'>No se puede previsualizar este archivo.</p>",
                download_href,
            ),
            status_code=404,
        )

    kind = _attachment_kind(
        attachment.filename,
        attachment.content_type,
        is_pdf=getattr(attachment, "is_pdf", False),
        is_image=getattr(attachment, "is_image", False),
        is_audio=getattr(attachment, "is_audio", False),
    )
    media_type = attachment.content_type or "application/octet-stream"

    if kind in {"pdf", "image", "audio"}:
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'inline; filename="{attachment.filename}"'
            },
        )

    if kind == "csv":
        try:
            frame = pd.read_csv(BytesIO(content))
            table_html = frame.to_html(index=False, escape=True)
            return HTMLResponse(
                _preview_html(
                    title,
                    f"<div class='table-wrap'>{table_html}</div>",
                    download_href,
                )
            )
        except Exception:
            pass

    if kind == "sheet":
        try:
            frame = pd.read_excel(BytesIO(content))
            table_html = frame.to_html(index=False, escape=True)
            return HTMLResponse(
                _preview_html(
                    title,
                    f"<div class='table-wrap'>{table_html}</div>",
                    download_href,
                )
            )
        except Exception:
            pass

    preview_text = (
        attachment.extracted_text
        or attachment.ocr_text
        or attachment.transcription_text
    )

    if preview_text is None and kind == "text":
        try:
            preview_text = content.decode("utf-8")
        except UnicodeDecodeError:
            preview_text = content.decode("latin-1", errors="replace")

    if preview_text:
        return HTMLResponse(
            _preview_html(
                title,
                f"<pre>{escape(preview_text[:12000])}</pre>",
                download_href,
            )
        )

    return HTMLResponse(
        _preview_html(
            title,
            "<p class='muted'>No se puede previsualizar este archivo.</p>",
            download_href,
        )
    )


@router.get("/{source_kind}/{source_id}/attachments/{attachment_id}")
def download_attachment(
    source_kind: str,
    source_id: int,
    attachment_id: int,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    attachment = None

    if source_kind == "email":
        source = db.get(Email, source_id)
        attachment = db.get(EmailAttachment, attachment_id)
        if (
            not source
            or not attachment
            or source.company_id != user.company_id
            or attachment.email_id != source.id
        ):
            return PlainTextResponse("No encontrado", status_code=404)
    else:
        source = db.get(InboundMessage, source_id)
        attachment = db.get(MessageAttachment, attachment_id)
        if (
            not source
            or not attachment
            or source.company_id != user.company_id
            or attachment.inbound_message_id != source.id
        ):
            return PlainTextResponse("No encontrado", status_code=404)

    try:
        content = read_attachment(attachment.storage_path or "", tenant_id=attachment.company_id)
    except Exception:
        return PlainTextResponse("Archivo no disponible", status_code=404)

    return Response(
        content=content,
        media_type=attachment.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{attachment.filename}"'
        },
    )
