from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timezone
from urllib.error import URLError

import json
from socket import gethostname
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.channels.service import is_channel_enabled
from app.agent.platform import LearningService
from app.agent.services import AgentProcessingService, ScoringService
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.metrics import record_job
from app.core.observability import observability_scope
from app.db.models import BackgroundJob, Email, EmailSettings, ExportFile, ExportSettings, FTPSettings, ImportJob, InboundMessage, Mailbox, Order, ScoringSettings
from app.exports.service import ExportService, FTPService
from app.logs.service import log_action
from app.orders.state import ORDER_STATE
import app.master.database as master_database
from app.master.database import MasterSessionLocal
from app.master.models import EmailSyncState, MailboxSyncState, MasterTenantDatabase
from app.imports.service import confirm_import, guess_mapping, read_preview
from app.knowledge.service import index_knowledge_entries
from app.orders.service import _customer_label, _sync_customer_product_knowledge, validate_confirmation
from app.routing.auto import AUTO_ROUTING_JOB_TYPE, process_automatic_routing
from app.routing.forwarding import AUTO_FORWARD_JOB_TYPE, process_forwarding_job
from app.semantic_retrieval.products import index_products
from app.settings.integrations import backfill_imap_emails, read_latest_imap_emails
from app.mailboxes.service import get_or_create_mailbox_sync_state
from app.settings.service import get_or_create_settings
from app.jobs.service import claim_next_job, enqueue_job, fail_job, finish_job, job_payload, job_trace, recover_stale_jobs, update_job_progress
from app.migrations.kibak_baseline import KIBAK_TENANT_BASELINE_VERSION
from app.tenancy.migrations import tenant_migration_report
from app.tenancy.database import tenant_db_session
from app.whatsapp.conversation_orders import evaluate_conversation_order
from app.whatsapp.conversation_semantics import evaluate_whatsapp_conversation_semantics
from app.whatsapp.service import send_automatic_response, whatsapp_config

logger = logging.getLogger(__name__)
_worker_started = False
_worker_identity: str | None = None

JOB_TYPES = {
    "email_sync",
    "process_recent_emails",
    "backfill_imap",
    "process_pending_emails",
    "process_email",
    "process_order",
    "process_inbound_message",
    "download_whatsapp_media",
    "import_confirm",
    "import_file",
    "export_order",
    "export_order_ftp",
    "bulk_order_action",
    "index_product_embeddings",
    "index_knowledge_entries",
    AUTO_ROUTING_JOB_TYPE,
    AUTO_FORWARD_JOB_TYPE,
}


def _now():
    return datetime.now(timezone.utc)


def _identity() -> str:
    global _worker_identity
    if _worker_identity:
        return _worker_identity
    _worker_identity = f"{gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    return _worker_identity


def _is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError, URLError)):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "timeout",
            "tempor",
            "connection reset",
            "connection refused",
            "service unavailable",
            "imap",
            "smtp",
            "network",
            "socket",
            "temporarily",
        )
    )


def _email_job_context(db, master_db, company_id: int, payload: dict):  # noqa: ANN001
    raw_mailbox_id = payload.get("mailbox_id")
    if raw_mailbox_id not in (None, ""):
        try:
            mailbox_id = int(raw_mailbox_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("El buzón solicitado no es válido.") from exc
        mailbox = db.get(Mailbox, mailbox_id)
        if not mailbox or mailbox.company_id != company_id:
            raise RuntimeError("No se encontró el buzón solicitado.")
        if not mailbox.enabled:
            raise RuntimeError("El buzón solicitado está desactivado.")
        state = get_or_create_mailbox_sync_state(master_db, mailbox)
        return mailbox, state, mailbox_id

    settings = get_or_create_settings(db, EmailSettings, company_id)
    sync_state = master_db.scalar(
        select(EmailSyncState).where(
            EmailSyncState.company_id == company_id,
            EmailSyncState.channel_key == "email",
        )
    )
    if not sync_state:
        sync_state = EmailSyncState(
            company_id=company_id,
            channel_key="email",
            enabled=True,
            frequency_seconds=60,
            status="idle",
            next_run_at=_now(),
        )
        master_db.add(sync_state)
        master_db.commit()
    return settings, sync_state, None


def _process_job(db, job: BackgroundJob) -> dict:
    payload = job_payload(job)
    if job.job_type == AUTO_ROUTING_JOB_TYPE:
        return process_automatic_routing(db, job)
    if job.job_type == AUTO_FORWARD_JOB_TYPE:
        return process_forwarding_job(db, job)
    if job.job_type in {"import_confirm", "import_file"}:
        return _process_import_job(db, job, payload)
    if job.job_type in {"export_order", "export_order_ftp"}:
        return _process_export_job(db, job, payload)
    if job.job_type == "index_product_embeddings":
        return _process_product_embeddings_job(db, job, payload)
    if job.job_type == "index_knowledge_entries":
        return _process_knowledge_entries_job(db, job, payload)
    if job.job_type == "bulk_order_action":
        return _process_bulk_action(db, job, payload)
    if job.job_type == "email_sync":
        if not is_channel_enabled(db, job.company_id, "email"):
            return {
                "ok": True,
                "skipped": True,
                "message": "Canal Email desactivado para este tenant",
            }
        master_db = MasterSessionLocal()
        try:
            settings, sync_state, mailbox_id = _email_job_context(db, master_db, job.company_id, payload)
            return read_latest_imap_emails(
                db,
                settings,
                job.company_id,
                auto_process=bool(payload.get("auto_process", False)),
                unread_only=payload.get("unread_only"),
                limit=payload.get("limit"),
                sync_state=sync_state,
                sync_session=master_db,
                mailbox_id=mailbox_id,
            )
        finally:
            master_db.close()
    if job.job_type == "process_recent_emails":
        if not is_channel_enabled(db, job.company_id, "email"):
            return {
                "ok": True,
                "skipped": True,
                "message": "Canal Email desactivado para este tenant",
            }
        limit = max(min(int(payload.get("limit", 3) or 3), 10), 1)
        master_db = MasterSessionLocal()
        try:
            settings, sync_state, mailbox_id = _email_job_context(db, master_db, job.company_id, payload)
            return read_latest_imap_emails(
                db,
                settings,
                job.company_id,
                auto_process=True,
                unread_only=False,
                limit=limit,
                sync_state=sync_state,
                sync_session=master_db,
                mailbox_id=mailbox_id,
            )
        finally:
            master_db.close()
    if job.job_type == "backfill_imap":
        if not is_channel_enabled(db, job.company_id, "email"):
            return {
                "ok": True,
                "skipped": True,
                "message": "Canal Email desactivado para este tenant",
            }
        master_db = MasterSessionLocal()
        try:
            settings, sync_state, mailbox_id = _email_job_context(db, master_db, job.company_id, payload)
            requested_limit = max(int(payload.get("limit") or 1), 1)

            result = backfill_imap_emails(
                db,
                settings,
                job.company_id,
                payload.get("from_date"),
                payload.get("to_date"),
                requested_limit,
                from_uid=payload.get("from_uid"),
                to_uid=payload.get("to_uid"),
                batch_size=5,
                resume=bool(payload.get("resume", False)),
                stop_after_batch=True,
                sync_state=sync_state,
                sync_session=master_db,
                mailbox_id=mailbox_id,
            )

            consumed = max(int(result.get("batch_count") or 0), 0)
            processed_before = max(int(payload.get("processed_count") or 0), 0)
            processed_count = processed_before + consumed
            remaining_limit = max(requested_limit - consumed, 0)
            total_found = max(int(payload.get("total_found") or result.get("total_found") or result.get("found") or 0), 0)
            remaining_messages = max(total_found - processed_count, 0)
            result["batch_count"] = consumed
            result["total_found"] = total_found
            result["remaining_limit"] = remaining_limit
            result["remaining_messages"] = remaining_messages
            result["remaining"] = remaining_messages

            if result.get("ok") and result.get("has_more") and remaining_limit > 0 and remaining_messages > 0:
                continuation_payload = {
                    "from_date": payload.get("from_date"),
                    "to_date": payload.get("to_date"),
                    "limit": remaining_limit,
                    "resume": True,
                    "total_found": total_found,
                    "processed_count": processed_count,
                }
                if payload.get("mailbox_id") is not None:
                    continuation_payload["mailbox_id"] = payload["mailbox_id"]
                if payload.get("to_uid"):
                    continuation_payload["to_uid"] = payload.get("to_uid")

                continuation = enqueue_job(
                    db,
                    company_id=job.company_id,
                    job_type="backfill_imap",
                    payload=continuation_payload,
                    created_by_user_id=job.created_by_user_id,
                )
                result["continuation_job_id"] = continuation.id

            return result
        finally:
            master_db.close()
    if job.job_type == "process_pending_emails":
        processor = AgentProcessingService()
        emails = db.scalars(
            select(Email).where(
                Email.company_id == job.company_id,
                Email.agent_status.in_(["not_processed", "pending_reprocess"]),
            )
        ).all()
        processed = 0
        total = len(emails) or 1
        for index, email in enumerate(emails, start=1):
            processor.process_email_fast(db, email)
            processed += 1
            update_job_progress(db, job, int((index / total) * 100))
        return {"ok": True, "processed": processed, "message": f"{processed} correos procesados"}
    if job.job_type == "process_email":
        email_id = int(payload.get("email_id") or 0)
        email = db.get(Email, email_id)
        if not email or email.company_id != job.company_id:
            raise RuntimeError("No se encontró el correo a procesar.")
        force_order = bool(payload.get("force"))
        result = AgentProcessingService().process_email_fast(
            db,
            email,
            force_order=force_order,
        )
        return {"ok": True, **result}
    if job.job_type == "process_order":
        order_id = int(payload.get("order_id") or 0)
        order = db.get(Order, order_id)
        if not order or order.company_id != job.company_id:
            raise RuntimeError("No se encontró el pedido a procesar.")
        if order.email:
            result = AgentProcessingService().process_email_fast(
                db,
                order.email,
                force_order=True,
            )
            return {"ok": True, **result}
        return {"ok": False, "message": "El pedido no tiene correo asociado."}
    if job.job_type == "process_inbound_message":
        inbound_message_id = int(payload.get("inbound_message_id") or 0)
        inbound_message = db.get(InboundMessage, inbound_message_id)
        if not inbound_message or inbound_message.company_id != job.company_id:
            raise RuntimeError("No se encontró el mensaje a procesar.")

        pipeline = AgentProcessingService().pipeline

        is_live_meta_whatsapp = (
            str(payload.get("channel") or "").strip().lower() == "whatsapp"
            and str(inbound_message.provider or "").strip().lower() == "meta"
        )

        if is_live_meta_whatsapp:
            config = whatsapp_config(db, job.company_id)
            semantic_evaluator = (
                evaluate_whatsapp_conversation_semantics
                if config.bot_enabled
                else None
            )

            context = evaluate_conversation_order(
                db,
                message=inbound_message,
                semantic_evaluator=semantic_evaluator,
            )

            if context.state == "collecting":
                inbound_message.status = "received"
                inbound_message.processing_step = "whatsapp_order_collecting"
                db.commit()

                auto_response = None
                if (
                    config.bot_enabled
                    and context.semantic_state
                    in {"needs_clarification", "ready_for_confirmation"}
                    and context.reply_needed
                    and context.suggested_reply
                ):
                    auto_response = asyncio.run(
                        send_automatic_response(
                            db,
                            company_id=job.company_id,
                            conversation_id=context.conversation_id,
                            trigger_message_id=inbound_message.id,
                            body=context.suggested_reply,
                            semantic_state=context.semantic_state,
                            prompt_execution_id=context.prompt_execution_id,
                        )
                    )

                return {
                    "ok": True,
                    "status": context.semantic_state or "collecting",
                    "message": (
                        "Conversación WhatsApp evaluada."
                        if context.semantic_state
                        else "Conversación WhatsApp pendiente de completar."
                    ),
                    "conversation_id": context.conversation_id,
                    "semantic_state": context.semantic_state,
                    "semantic_intent": context.semantic_intent,
                    "missing_or_uncertain": context.missing_or_uncertain or [],
                    "reply_needed": context.reply_needed,
                    "auto_response": auto_response,
                }

            if context.state == "ready":
                result = pipeline.process_inbound_message(
                    db,
                    inbound_message,
                    source_text_override=context.transcript,
                )

                order_id = result.get("order_id") if isinstance(result, dict) else None
                if order_id:
                    for source_message in context.messages:
                        if source_message.company_id != job.company_id:
                            continue
                        if source_message.order_id is None:
                            source_message.order_id = int(order_id)
                    db.commit()

                return {
                    "ok": True,
                    "conversation_id": context.conversation_id,
                    **result,
                }

        result = pipeline.process_inbound_message(db, inbound_message)
        return {"ok": True, **result}
    if job.job_type == "download_whatsapp_media":
        inbound_message_id = int(payload.get("inbound_message_id") or 0)
        from app.whatsapp.service import download_whatsapp_media, enqueue_whatsapp_processing

        result = asyncio.run(
            download_whatsapp_media(
                db,
                company_id=job.company_id,
                inbound_message_id=inbound_message_id,
            )
        )
        processing_job_id = None
        if result.get("ready_for_processing"):
            processing_job = enqueue_whatsapp_processing(db, job.company_id, inbound_message_id, user_id=job.created_by_user_id)
            processing_job_id = processing_job.id
        message = (
            "Media WhatsApp descargada y entrada encolada"
            if processing_job_id
            else "Media WhatsApp guardada; la entrada queda pendiente de completar la extraccion"
        )
        return {**result, "processing_job_id": processing_job_id, "message": message}
    raise RuntimeError(f"Tipo de job no soportado: {job.job_type}")


def _process_product_embeddings_job(db, job: BackgroundJob, payload: dict) -> dict:
    batch_size = max(1, min(int(payload.get("batch_size") or 64), 256))
    model = str(payload.get("model") or "").strip() or None
    stats = index_products(db, company_id=job.company_id, model=model, batch_size=batch_size)
    update_job_progress(db, job, 100)
    return {
        "ok": stats.failed == 0,
        "message": "Embeddings de productos indexados" if stats.failed == 0 else "Indexacion de embeddings completada con errores",
        "scanned": stats.scanned,
        "indexed": stats.indexed,
        "skipped": stats.skipped,
        "failed": stats.failed,
    }


def _process_knowledge_entries_job(db, job: BackgroundJob, payload: dict) -> dict:
    batch_size = max(1, min(int(payload.get("batch_size") or 64), 256))
    model = str(payload.get("model") or "").strip() or None
    stats = index_knowledge_entries(db, company_id=job.company_id, model=model, batch_size=batch_size)
    update_job_progress(db, job, 100)
    return {
        "ok": stats.failed == 0,
        "message": "Conocimiento empresarial indexado" if stats.failed == 0 else "Indexacion de conocimiento completada con errores",
        "scanned": stats.scanned,
        "indexed": stats.indexed,
        "skipped": stats.skipped,
        "failed": stats.failed,
    }


def _process_import_job(db, job: BackgroundJob, payload: dict) -> dict:
    token = str(payload.get("token") or "")
    filename = str(payload.get("filename") or "import")
    entity_type = str(payload.get("entity_type") or "customers")
    encoding = str(payload.get("encoding") or "utf-8")
    mapping = payload.get("mapping") or {}
    mode = str(payload.get("mode") or "create_update")
    save_template = bool(payload.get("save_template"))
    template_name = str(payload.get("template_name") or "")
    df = read_preview(token, filename, encoding=encoding)
    if not mapping:
        mapping = guess_mapping(entity_type, [str(column) for column in df.columns])
    dummy_user = type("TenantLikeUser", (), {"id": job.created_by_user_id or 0, "company_id": job.company_id})()
    import_job = confirm_import(
        db,
        company_id=job.company_id,
        user=dummy_user,
        entity_type=entity_type,
        filename=filename,
        df=df,
        mapping=mapping,
        mode=mode,
        save_template=save_template,
        template_name=template_name,
    )
    return {
        "ok": True,
        "message": f"Importacion {entity_type} completada",
        "import_job_id": import_job.id,
        "rows_total": import_job.rows_total,
        "rows_created": import_job.rows_created,
        "rows_updated": import_job.rows_updated,
        "rows_ignored": import_job.rows_ignored,
    }


def _serialize_export(export: ExportFile) -> dict:
    return {
        "id": export.id,
        "filename": export.filename,
        "status": export.status,
        "created_at": export.created_at.isoformat() if export.created_at else None,
    }


def _process_export_job(db, job: BackgroundJob, payload: dict) -> dict:
    order_id = int(payload.get("order_id") or 0)
    order = db.scalar(
        select(Order)
        .where(Order.id == order_id, Order.company_id == job.company_id)
        .options(selectinload(Order.lines), selectinload(Order.validated_customer), selectinload(Order.customer), selectinload(Order.email).selectinload(Email.attachments))
    )
    if not order:
        raise RuntimeError("No se encontró el pedido a exportar.")
    export = db.scalar(select(ExportFile).where(ExportFile.order_id == order.id, ExportFile.company_id == job.company_id).order_by(ExportFile.created_at.desc()))
    if not export:
        export = ExportService().generate(db, order)
    ftp_settings = get_or_create_settings(db, FTPSettings, job.company_id)
    send_via_ftp = job.job_type == "export_order_ftp"

    if not send_via_ftp:
        export.status = "generated"
        return {
            "ok": True,
            "message": "Archivo de exportacion generado",
            "export": _serialize_export(export),
            "order_id": order.id,
        }

    if export.status == "sent":
        return {
            "ok": True,
            "message": "La exportacion ya estaba completada; no se reenvia el archivo.",
            "export": _serialize_export(export),
            "order_id": order.id,
        }

    if order.status not in {"pedido_confirmado", "pedido_validado", "error_exportacion"}:
        raise RuntimeError("El pedido debe estar confirmado antes de enviarse.")

    validation_errors = validate_confirmation(
        order,
        get_or_create_settings(db, ScoringSettings, job.company_id),
    )
    if validation_errors:
        raise RuntimeError("No se puede exportar el pedido: " + " | ".join(validation_errors))

    if not ftp_settings.host:
        raise RuntimeError("La conexion de exportacion no esta configurada.")

    export_settings = get_or_create_settings(
        db,
        ExportSettings,
        job.company_id,
    )
    ok = FTPService().send(
        export,
        ftp_settings,
        encoding=export_settings.encoding or "utf-8",
    )

    ORDER_STATE.export(order, ok=ok, when=_now())
    export.status = "sent" if ok else "error"

    if not ok:
        return {
            "ok": False,
            "message": "Exportacion fallida",
            "export": _serialize_export(export),
            "order_id": order.id,
        }

    if ok:
        order.exported_at = order.exported_at or _now()
        for line in order.lines or []:
            _sync_customer_product_knowledge(
                db,
                company_id=job.company_id,
                order=order,
                line=line,
                user=type("TenantLikeUser", (), {"id": job.created_by_user_id or 0, "company_id": job.company_id})(),
                source_context="pedido_exportado_ftp" if send_via_ftp else "pedido_exportado",
                exported_at=order.exported_at,
            )
        LearningService().record_case(
            db,
            company_id=job.company_id,
            summary=f"{_customer_label(order)} exportado {'por FTP/SFTP' if send_via_ftp else 'a salida'}.",
            resolved_action="pedido_exportado_ftp" if send_via_ftp else "pedido_exportado",
            resolution_json=json.dumps({"order_id": order.id, "customer": _customer_label(order), "export": export.filename}, ensure_ascii=False),
            customer_id=order.validated_customer_id or order.customer_id,
            order_id=order.id,
        )
    return {
        "ok": ok,
        "message": "Exportacion completada" if ok else "Exportacion fallida",
        "export": _serialize_export(export),
        "order_id": order.id,
    }


def _process_bulk_action(db, job: BackgroundJob, payload: dict) -> dict:
    action = str(payload.get("action") or "")
    target_state = str(payload.get("target_state") or "")
    items = payload.get("selected_items") or []
    if not isinstance(items, list):
        items = []
    processor = AgentProcessingService()
    scoring = get_or_create_settings(db, ScoringSettings, job.company_id)
    processed = 0
    skipped = 0
    total = len(items) or 1
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
            continue
        kind = str(item.get("kind") or "")
        try:
            item_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
            continue
        if not item_id:
            skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
            continue
        if kind == "order":
            order = db.scalar(
                select(Order)
                .where(Order.id == item_id, Order.company_id == job.company_id)
                .options(selectinload(Order.lines), selectinload(Order.customer), selectinload(Order.validated_customer), selectinload(Order.email).selectinload(Email.attachments))
            )
            if not order:
                skipped += 1
                update_job_progress(db, job, int(((processed + skipped) / total) * 100))
                continue
            if action in {"process", "reprocess"}:
                if order.email:
                    processor.process_email(db, order.email)
                else:
                    order.score = ScoringService().score_order(db, order)
                    ORDER_STATE.apply_score(db, order, job.company_id, order.score)
                db.commit()
                processed += 1
            elif action == "confirm":
                errors = validate_confirmation(order, scoring)
                if errors:
                    skipped += 1
                    update_job_progress(db, job, int(((processed + skipped) / total) * 100))
                    continue
                ORDER_STATE.confirm(order, when=_now())
                for line in order.lines or []:
                    _sync_customer_product_knowledge(
                        db,
                        company_id=job.company_id,
                        order=order,
                        line=line,
                        user=type("TenantLikeUser", (), {"id": job.created_by_user_id or 0, "company_id": job.company_id})(),
                        source_context="pedido_confirmado",
                        force_habitual=False,
                    )
                LearningService().record_case(
                    db,
                    company_id=job.company_id,
                    summary=f"{_customer_label(order)} confirmado con {len(order.lines or [])} lineas.",
                    resolved_action="pedido_confirmado",
                    resolution_json=json.dumps({"order_id": order.id, "customer": _customer_label(order), "lines": len(order.lines or [])}, ensure_ascii=False),
                    customer_id=order.validated_customer_id or order.customer_id,
                    order_id=order.id,
                )
                db.commit()
                processed += 1
            elif action == "export":
                export = db.scalar(
                    select(ExportFile)
                    .where(
                        ExportFile.order_id == order.id,
                        ExportFile.company_id == job.company_id,
                    )
                    .order_by(ExportFile.created_at.desc())
                )
                if export and export.status == "sent":
                    processed += 1
                    update_job_progress(db, job, int(((processed + skipped) / total) * 100))
                    continue

                if order.status not in {"pedido_confirmado", "pedido_validado", "error_exportacion"}:
                    skipped += 1
                    update_job_progress(db, job, int(((processed + skipped) / total) * 100))
                    continue

                validation_errors = validate_confirmation(order, scoring)
                if validation_errors:
                    skipped += 1
                    update_job_progress(db, job, int(((processed + skipped) / total) * 100))
                    continue

                ftp_settings = get_or_create_settings(db, FTPSettings, job.company_id)
                if not ftp_settings.host:
                    skipped += 1
                    update_job_progress(db, job, int(((processed + skipped) / total) * 100))
                    continue

                if not export:
                    export = ExportService().generate(db, order)

                export_settings = get_or_create_settings(
                    db,
                    ExportSettings,
                    job.company_id,
                )
                ok = FTPService().send(
                    export,
                    ftp_settings,
                    encoding=export_settings.encoding or "utf-8",
                )
                ORDER_STATE.export(order, ok=ok, when=_now())
                export.status = "sent" if ok else "error"

                if not ok:
                    raise RuntimeError(f"Exportacion fallida para pedido {order.id}")

                order.exported_at = order.exported_at or _now()
                db.commit()
                processed += 1
            elif action in {"delete", "discard"}:
                ORDER_STATE.discard(order)
                db.commit()
                processed += 1
            elif action == "mark_no_order":
                ORDER_STATE.mark_no_order(order)
                if order.email:
                    order.email.detected_type = "no_pedido"
                    order.email.status = "no_pedido"
                db.commit()
                processed += 1
            elif action == "change_state" and target_state:
                ORDER_STATE.change_state(order, target_state)
                db.commit()
                processed += 1
            elif action == "recalculate":
                order.score = ScoringService().score_order(db, order)
                ORDER_STATE.apply_score(db, order, job.company_id, order.score)
                db.commit()
                processed += 1
            else:
                skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
        elif kind == "email":
            email = db.get(Email, item_id)
            if not email or email.company_id != job.company_id:
                skipped += 1
                update_job_progress(db, job, int(((processed + skipped) / total) * 100))
                continue
            if action in {"process", "reprocess"}:
                processor.process_email(db, email)
                processed += 1
            elif action in {"mark_no_order", "resolve"}:
                email.status = "no_pedido"
                email.agent_status = "processed_no_order"
                email.detected_type = "no_pedido"
                email.processing_error = None
                db.commit()
                processed += 1
            elif action in {"delete", "discard"}:
                email.status = "descartado"
                email.agent_status = "discarded"
                db.commit()
                processed += 1
            elif action == "change_state" and target_state:
                email.status = target_state
                db.commit()
                processed += 1
            else:
                skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
        else:
            skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
    return {"ok": True, "message": f"Accion masiva {action}: {processed} aplicadas, {skipped} omitidas.", "processed": processed, "skipped": skipped}


def _handle_tenant_jobs(
    tenant: MasterTenantDatabase,
    owner: str,
    *,
    max_jobs: int | None = None,
) -> dict[str, int]:
    database_url = tenant.get_database_url()
    if not database_url:
        return {"recovered": 0, "attempted": 0, "processed": 0, "blocked": 0}
    session_factory = tenant_db_session(database_url)
    db = session_factory()
    recovered_jobs = 0
    attempted_jobs = 0
    processed_jobs = 0
    blocked_jobs = 0
    try:
        schema_report = tenant_migration_report(db, tenant.company_id, persist=False)
        if (
            get_settings().app_slug.strip().lower() == "kibak"
            and schema_report.get("version") == KIBAK_TENANT_BASELINE_VERSION
            and schema_report.get("status") == "current"
        ):
            schema_report["is_current"] = True
        if not schema_report.get("is_current"):
            logger.warning(
                "tenant schema incompatible company=%s status=%s version=%s checksum=%s",
                tenant.company_id,
                schema_report.get("status"),
                schema_report.get("version"),
                schema_report.get("checksum"),
            )
            return {"recovered": 0, "attempted": 0, "processed": 0, "blocked": 1}
        recovered = recover_stale_jobs(db, owner=owner, job_types=JOB_TYPES)
        recovered_jobs += len(recovered)
        while max_jobs is None or attempted_jobs < max_jobs:
            job = claim_next_job(
                db,
                owner=owner,
                job_types=JOB_TYPES,
            )
            if not job:
                break
            attempted_jobs += 1
            trace = job_trace(job)
            with observability_scope(
                request_id=trace.get("request_id"),
                correlation_id=trace.get("correlation_id") or trace.get("request_id"),
                tenant_id=job.company_id,
                user_id=trace.get("user_id"),
                membership_id=trace.get("membership_id"),
                job_id=job.id,
                worker_id=owner,
                route=f"job:{job.job_type}",
                method="worker",
            ):
                try:
                    logger.info(
                        "job.start",
                        extra={
                            "event": "job.start",
                            "job_id": job.id,
                            "job_type": job.job_type,
                            "company_id": job.company_id,
                            "worker_id": owner,
                        },
                    )
                    record_job(job_type=job.job_type, status="started")
                    result = _process_job(db, job)
                    if isinstance(result, dict) and result.get("ok") is False:
                        error = RuntimeError(str(result.get("message") or "El job devolvio ok=false."))
                        error.retryable = bool(result.get("retryable"))
                        error.error_type = str(result.get("error_type") or "job_failed")
                        raise error
                    finish_job(db, job, result)
                    record_job(job_type=job.job_type, status="success")
                    log_action(db, company_id=job.company_id, user=None, action=f"job.{job.job_type}.success", entity_type="job", entity_id=job.id, message=result.get("message") or "Trabajo completado")
                    processed_jobs += 1
                    logger.info(
                        "job.end",
                        extra={
                            "event": "job.end",
                            "job_id": job.id,
                            "job_type": job.job_type,
                            "company_id": job.company_id,
                            "worker_id": owner,
                            "status": "success",
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Job fallido company=%s job=%s", job.company_id, job.job_type)
                    should_retry = job.attempt_count < (max(0, job.max_retries or 0) + 1) and (
                        bool(getattr(exc, "retryable", False)) or _is_retryable_exception(exc)
                    )
                    failed_job = fail_job(
                        db,
                        job,
                        str(exc),
                        retry=should_retry,
                        error_type=str(getattr(exc, "error_type", None) or exc.__class__.__name__),
                    )
                    record_job(job_type=job.job_type, status=failed_job.status)
                    action_suffix = "retrying" if failed_job.status == "retrying" else "failed"
                    log_action(db, company_id=job.company_id, user=None, action=f"job.{job.job_type}.{action_suffix}", entity_type="job", entity_id=job.id, message=str(exc))
                    logger.info(
                        "job.end",
                        extra={
                            "event": "job.end",
                            "job_id": job.id,
                            "job_type": job.job_type,
                            "company_id": job.company_id,
                            "worker_id": owner,
                            "status": failed_job.status,
                        },
                    )
    finally:
        db.close()
    return {
        "recovered": recovered_jobs,
        "attempted": attempted_jobs,
        "processed": processed_jobs,
        "blocked": blocked_jobs,
    }


def run_worker_cycle(*, max_jobs: int | None = None) -> dict[str, int]:
    configure_logging()
    summary = {
        "tenants": 0,
        "recovered": 0,
        "attempted": 0,
        "processed": 0,
        "blocked": 0,
    }
    master_db = MasterSessionLocal()
    try:
        tenants = master_db.scalars(
            select(MasterTenantDatabase).where(
                MasterTenantDatabase.is_active.is_(True),
                MasterTenantDatabase.database_url.is_not(None),
            )
        ).all()
        summary["tenants"] = len(tenants)
        for tenant in tenants:
            remaining = None if max_jobs is None else max(max_jobs - summary["attempted"], 0)
            if remaining == 0:
                break
            tenant_summary = _handle_tenant_jobs(
                tenant,
                owner=_identity(),
                max_jobs=remaining,
            )
            summary["recovered"] += tenant_summary["recovered"]
            summary["attempted"] += tenant_summary.get("attempted", 0)
            summary["processed"] += tenant_summary["processed"]
            summary["blocked"] += tenant_summary.get("blocked", 0)
    finally:
        master_db.close()
    logger.info(
        "Job worker cycle completed tenants=%s recovered=%s processed=%s owner=%s",
        summary["tenants"],
        summary["recovered"],
        summary["processed"],
        _identity(),
    )
    return summary


def _worker_loop() -> None:
    settings = get_settings()
    poll_seconds = max(int(getattr(settings, "job_worker_poll_seconds", 10)), 5)
    while True:
        try:
            run_worker_cycle()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job worker error: %s", exc)
        time.sleep(poll_seconds)


def start_job_worker() -> None:
    configure_logging()
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_worker_loop, name="anchi-job-worker", daemon=True).start()


def is_job_worker_started() -> bool:
    return _worker_started


def main() -> None:
    configure_logging()
    _worker_loop()


if __name__ == "__main__":
    main()
