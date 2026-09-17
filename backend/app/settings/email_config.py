from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.encryption import mask_secret
from app.db.models import EmailSettings, EmailTemplate
from app.settings.service import get_or_create_settings


DEFAULT_EMAIL_TEMPLATES = [
    {
        "key": "confirmacion_recepcion",
        "name": "Confirmacion de recepcion",
        "template_type": "confirmacion_recepcion",
        "subject_template": "Re: {asunto_original}",
        "body_template": "Hola,\n\nHemos recibido vuestro pedido y queda pendiente de validacion por nuestro equipo.\n\nGracias.\n\n{firma}",
        "is_default_for_type": True,
    },
    {
        "key": "aclaracion_producto",
        "name": "Solicitud de aclaracion de producto",
        "template_type": "aclaracion_producto",
        "subject_template": "Re: {asunto_original}",
        "body_template": "Hola,\n\nHemos recibido vuestro pedido, pero necesitamos confirmar la referencia o descripcion de los siguientes productos:\n\n{lineas_dudosas}\n\n¿Nos lo podeis confirmar, por favor?\n\nGracias.\n\n{firma}",
        "is_default_for_type": True,
    },
    {
        "key": "pdf_no_legible",
        "name": "PDF no legible",
        "template_type": "pdf_no_legible",
        "subject_template": "Re: {asunto_original}",
        "body_template": "Hola,\n\nNo hemos podido leer correctamente el PDF adjunto. ¿Podeis reenviarlo en mejor calidad o en formato PDF legible?\n\nGracias.\n\n{firma}",
        "is_default_for_type": True,
    },
    {
        "key": "faltan_cantidades",
        "name": "Faltan cantidades",
        "template_type": "faltan_cantidades",
        "subject_template": "Re: {asunto_original}",
        "body_template": "Hola,\n\nHemos recibido vuestro pedido, pero nos faltan las cantidades de algunos productos:\n\n{lineas_sin_cantidad}\n\n¿Nos lo podeis confirmar, por favor?\n\nGracias.\n\n{firma}",
        "is_default_for_type": True,
    },
    {
        "key": "derivacion_interna",
        "name": "Correo derivado internamente",
        "template_type": "derivacion_interna",
        "subject_template": "Re: {asunto_original}",
        "body_template": "Hola,\n\nGracias por vuestro mensaje. Lo hemos derivado al equipo correspondiente para su revision.\n\nGracias.\n\n{firma}",
        "is_default_for_type": True,
    },
    {
        "key": "pedido_no_identificado",
        "name": "Pedido no identificado",
        "template_type": "pedido_no_identificado",
        "subject_template": "Re: {asunto_original}",
        "body_template": "Hola,\n\nHemos recibido vuestro correo, pero no hemos podido identificar correctamente el pedido o los datos necesarios para tramitarlo.\n\n¿Nos podeis enviar el pedido con referencias, cantidades y datos de entrega?\n\nGracias.\n\n{firma}",
        "is_default_for_type": True,
    },
]

TEMPLATE_VARIABLES = [
    "{asunto_original}",
    "{cliente_nombre}",
    "{remitente_email}",
    "{fecha_recepcion}",
    "{lineas_dudosas}",
    "{lineas_sin_cantidad}",
    "{productos_no_encontrados}",
    "{referencia_pedido}",
    "{usuario_nombre}",
    "{empresa_nombre}",
    "{firma}",
]


def ensure_default_email_templates(db: Session, company_id: int, user_id: int | None = None) -> None:
    for data in DEFAULT_EMAIL_TEMPLATES:
        template = db.scalar(select(EmailTemplate).where(EmailTemplate.company_id == company_id, EmailTemplate.key == data["key"]))
        if not template:
            db.add(EmailTemplate(company_id=company_id, updated_by=user_id, active=True, **data))
        else:
            template.active = True
            template.name = data["name"]
            template.template_type = data["template_type"]
            template.subject_template = data["subject_template"]
            template.body_template = data["body_template"]
            template.is_default_for_type = data["is_default_for_type"]
            template.updated_by = user_id
            template.updated_at = datetime.now(timezone.utc)


def email_templates(db: Session, company_id: int) -> list[EmailTemplate]:
    ensure_default_email_templates(db, company_id)
    db.commit()
    return db.scalars(select(EmailTemplate).where(EmailTemplate.company_id == company_id).order_by(EmailTemplate.template_type, EmailTemplate.name)).all()


def email_config_status(settings: EmailSettings) -> dict:
    oauth_ready = bool(
        settings.connection_method == "oauth2"
        and settings.provider in {"gmail", "microsoft365"}
        and settings.refresh_token_encrypted
    )
    imap_ready = bool(
        settings.imap_host
        and settings.imap_username
        and (settings.imap_password_encrypted or oauth_ready)
    )
    smtp_ready = bool(settings.smtp_host and settings.smtp_username and settings.smtp_password_encrypted and (settings.from_email or settings.smtp_username))
    provider_status = {
        "imap": "Conectada" if imap_ready else "Pendiente de conexión",
        "gmail": "Preparado para Gmail" if settings.provider == "gmail" else "IMAP tradicional",
        "microsoft365": "Preparado para Microsoft 365" if settings.provider == "microsoft365" else "IMAP tradicional",
        "smtp": "Activado" if settings.smtp_enabled else "Desactivado",
    }
    return {
        "imap_ready": imap_ready,
        "smtp_ready": smtp_ready,
        "smtp_enabled": settings.smtp_enabled,
        "provider_status": provider_status,
        "last_imap_test": {
            "ok": settings.last_imap_test_ok,
            "message": settings.last_imap_test_message,
            "at": settings.last_imap_test_at,
        },
        "last_smtp_test": {
            "ok": settings.last_smtp_test_ok,
            "message": settings.last_smtp_test_message,
            "at": settings.last_smtp_test_at,
        },
        "last_sync": {
            "ok": settings.last_sync_ok,
            "message": settings.last_sync_message,
            "error": settings.last_sync_error,
            "at": settings.last_sync_at,
            "new": settings.last_sync_new,
            "duplicates": settings.last_sync_duplicates,
        },
    }


def serialize_email_settings(db: Session, company_id: int) -> dict:
    settings = get_or_create_settings(db, EmailSettings, company_id)
    templates = email_templates(db, company_id)
    return {
        "receive": {
            "provider": settings.provider,
            "imap_host": settings.imap_host,
            "imap_port": settings.imap_port,
            "imap_security": settings.imap_security,
            "imap_username": settings.imap_username,
            "imap_password": mask_secret(settings.imap_password_encrypted),
            "inbox_folder": settings.inbox_folder,
            "processed_folder": settings.processed_folder,
            "error_folder": settings.error_folder,
            "no_order_folder": settings.no_order_folder,
            "doubtful_folder": settings.doubtful_folder,
            "read_limit": settings.read_limit,
            "auto_sync_enabled": settings.auto_sync_enabled,
            "read_unread_only": settings.read_unread_only,
            "read_from_date": settings.read_from_date,
            "initial_history_mode": settings.initial_history_mode,
            "initial_history_limit": settings.initial_history_limit,
            "mark_as_read_after_import": settings.mark_as_read_after_import,
            "move_after_processing": settings.move_after_processing,
            "post_process_action": settings.post_process_action,
            "polling_frequency_minutes": settings.polling_frequency_minutes,
        },
        "send": {
            "smtp_enabled": settings.smtp_enabled,
            "provider": settings.smtp_provider,
            "smtp_host": settings.smtp_host,
            "smtp_port": settings.smtp_port,
            "smtp_security": settings.smtp_security,
            "smtp_username": settings.smtp_username,
            "smtp_password": mask_secret(settings.smtp_password_encrypted),
            "from_email": settings.from_email,
            "from_name": settings.from_name,
            "reply_to": settings.reply_to,
            "default_cc": settings.default_cc,
            "default_bcc": settings.default_bcc,
            "save_internal_copy": settings.save_internal_copy,
            "preserve_thread_headers": settings.preserve_thread_headers,
        },
        "processing": {
            "auto_process_on_fetch": settings.auto_process_on_fetch,
            "process_only_with_attachments": settings.process_only_with_attachments,
            "process_only_with_pdf": settings.process_only_with_pdf,
            "process_without_attachments": settings.process_without_attachments,
            "process_read_emails": settings.process_read_emails,
            "avoid_duplicates_by_message_id": settings.avoid_duplicates_by_message_id,
            "allow_reprocess": settings.allow_reprocess,
            "auto_create_order_if_detected": settings.auto_create_order_if_detected,
            "always_human_review": settings.always_human_review,
            "mark_doubtful_below_threshold": settings.mark_doubtful_below_threshold,
            "mark_no_order_if_detected": settings.mark_no_order_if_detected,
            "action_order_detected": settings.action_order_detected,
            "action_no_order": settings.action_no_order,
            "action_doubtful": settings.action_doubtful,
            "action_error": settings.action_error,
            "minimum_score_auto_order": settings.minimum_score_auto_order,
            "visible_states": settings.visible_states,
        },
        "ui": {
            "default_filter": settings.default_filter,
            "default_date_range": settings.default_date_range,
            "default_page_size": settings.default_page_size,
            "default_sort": settings.default_sort,
            "show_summary_cards": settings.show_summary_cards,
            "show_score_column": settings.show_score_column,
            "show_customer_column": settings.show_customer_column,
            "show_attachments_column": settings.show_attachments_column,
            "show_order_column": settings.show_order_column,
            "show_reply_button": settings.show_reply_button,
            "show_process_button": settings.show_process_button,
        },
        "signature": {
            "from_name": settings.from_name,
            "from_email": settings.from_email,
            "reply_to": settings.reply_to,
            "signature_text": settings.signature_text,
            "signature_html": settings.signature_html,
            "use_signature": settings.use_signature,
            "include_logo": settings.include_logo_in_signature,
            "legal_footer": settings.legal_footer,
        },
        "templates": [
            {
                "id": template.id,
                "key": template.key,
                "name": template.name,
                "type": template.template_type,
                "subject_template": template.subject_template,
                "body_template": template.body_template,
                "active": template.active,
                "is_default_for_type": template.is_default_for_type,
            }
            for template in templates
        ],
        "status": email_config_status(settings),
        "variables": TEMPLATE_VARIABLES,
    }
