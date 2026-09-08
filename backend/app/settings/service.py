from sqlalchemy.orm import Session

from app.core.encryption import decrypt_secret, encrypt_secret
from app.db.models import DecisionSettings, EmailSettings, ExportSettings, FTPSettings, LLMSettings, ScoringSettings, User


MODEL_MAP = {
    "email": EmailSettings,
    "llm": LLMSettings,
    "ftp": FTPSettings,
    "export": ExportSettings,
    "scoring": ScoringSettings,
    "decision": DecisionSettings,
}


def get_or_create_settings(db: Session, model, company_id: int):
    instance = db.query(model).filter(model.company_id == company_id).one_or_none()
    if not instance:
        instance = model(company_id=company_id)
        db.add(instance)
        db.commit()
        db.refresh(instance)
    return instance


def update_with_form(instance, data: dict[str, str], secret_fields: set[str] | None = None) -> None:
    secret_fields = secret_fields or set()
    bool_fields = {
        "passive_mode", "overwrite_files", "include_header", "block_without_customer", "block_without_reference", "block_without_quantity", "block_below_threshold",
        "imap_use_ssl", "auto_sync_enabled", "read_unread_only", "mark_as_read_after_import", "move_after_processing", "save_internal_copy", "preserve_thread_headers",
        "auto_process_on_fetch", "process_only_with_attachments", "process_only_with_pdf", "process_without_attachments", "process_read_emails",
        "avoid_duplicates_by_message_id", "allow_reprocess", "auto_create_order_if_detected", "always_human_review", "mark_doubtful_below_threshold",
        "mark_no_order_if_detected", "show_summary_cards", "show_score_column", "show_customer_column", "show_attachments_column", "show_order_column",
        "show_reply_button", "show_process_button", "use_signature", "include_logo_in_signature", "active", "is_default_for_type",
        "smtp_enabled", "enabled",
        "is_active", "is_default", "supports_text", "supports_attachments", "supports_audio", "supports_documents", "is_secret",
        "agent_enabled", "use_same_model_for_all", "can_read_email", "can_extract_pdf", "can_classify_email", "can_extract_order",
        "can_suggest_customer", "can_suggest_products", "can_calculate_score", "can_create_pending_order", "can_mark_no_order",
        "can_reply_customer", "allow_auto_confirm", "allow_auto_export", "detailed_llm_logs", "store_llm_payloads", "anonymize_llm_logs",
        "debug_mode",
        "auto_routing_enabled",
        "auto_forwarding_enabled",
        "simulation_mode",
        "enable_exact_match", "enable_alias_match", "enable_relation_match", "enable_history_match", "enable_rag_match", "enable_llm_support",
        "always_human_review", "auto_approve_aliases", "block_new_customer", "block_conflicting_aliases", "block_missing_quantity", "block_missing_reference",
    }
    for key, value in data.items():
        if not hasattr(instance, key):
            continue
        if key in secret_fields:
            normalized = (value or "").strip()
            if not normalized or normalized in {"********", "••••••••"}:
                continue
            if decrypt_secret(normalized) is not None:
                setattr(instance, key, normalized)
                continue
            setattr(instance, key, encrypt_secret(normalized))
            continue
        current = getattr(instance, key)
        if key in bool_fields:
            setattr(instance, key, value == "on")
        elif isinstance(current, int):
            setattr(instance, key, int(value or 0))
        elif isinstance(current, float):
            setattr(instance, key, float(value or 0))
        else:
            setattr(instance, key, value)


def resolve_updated_by_id(db: Session, user) -> int | None:
    if not user:
        return None

    try:
        user_id = getattr(user, "id", None)
        if user_id is not None:
            tenant_user = db.get(User, user_id)
            if tenant_user and tenant_user.company_id == user.company_id:
                return tenant_user.id

        email = (getattr(user, "email", None) or "").strip().lower()
        if not email:
            return None

        tenant_user = db.query(User).filter(
            User.company_id == user.company_id,
            User.email.ilike(email),
        ).one_or_none()

        return tenant_user.id if tenant_user else None
    except Exception:
        return None
