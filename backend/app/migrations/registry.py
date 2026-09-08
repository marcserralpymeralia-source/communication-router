from __future__ import annotations

from datetime import datetime, timezone

from app.db.models import TenantSchemaMigration
from app.master.models import MasterSchemaMigration
from sqlalchemy import inspect, text

from app.migrations.helpers import checksum_text, ensure_columns, ensure_unique_index
from app.migrations.runner import MigrationSpec, registry_checksum


TENANT_MIGRATION_COLUMNS = {
    "name": "VARCHAR(180) DEFAULT 'unregistered'",
    "checksum": "VARCHAR(120)",
    "execution_ms": "INTEGER DEFAULT 0",
    "application_version": "VARCHAR(80)",
}

TENANT_COMPAT_COLUMNS = {
    "customers": {
        "delegation": "VARCHAR(120)",
        "assigned_salesperson": "VARCHAR(180)",
        "accounting_code": "VARCHAR(100)",
        "company_inactive": "BOOLEAN DEFAULT false",
        "category": "VARCHAR(120)",
        "deleted_at": "DATETIME",
        "deleted_by": "INTEGER",
    },
    "companies": {
        "legal_name": "VARCHAR(255)",
        "active": "BOOLEAN DEFAULT true",
        "plan": "VARCHAR(80)",
        "tax_id": "VARCHAR(80)",
        "email": "VARCHAR(255)",
        "phone": "VARCHAR(80)",
        "web": "VARCHAR(255)",
        "address": "VARCHAR(500)",
        "country": "VARCHAR(120)",
        "currency": "VARCHAR(10) DEFAULT 'EUR'",
        "notification_email": "VARCHAR(255)",
        "responsible_contact": "VARCHAR(255)",
        "default_language": "VARCHAR(20) DEFAULT 'es'",
        "updated_at": "DATETIME",
    },
    "products": {
        "brand": "VARCHAR(150)",
        "usual_supplier": "VARCHAR(255)",
        "sale_price": "FLOAT",
        "discount_percent": "FLOAT",
        "size_group": "VARCHAR(120)",
        "colors": "VARCHAR(255)",
        "entry_date": "VARCHAR(50)",
        "obsolete": "BOOLEAN DEFAULT false",
        "article_type": "VARCHAR(120)",
        "description_cont": "TEXT",
        "warehouse_location_code": "VARCHAR(120)",
        "replenishment_warehouse": "VARCHAR(120)",
        "deleted_at": "DATETIME",
        "deleted_by": "INTEGER",
    },
    "orders": {
        "conversation_id": "INTEGER",
        "archived": "BOOLEAN DEFAULT false",
        "deleted_at": "DATETIME",
        "deleted_by": "INTEGER",
        "delete_reason": "TEXT",
    },
    "decision_settings": {
        "enable_exact_match": "BOOLEAN DEFAULT true",
        "enable_alias_match": "BOOLEAN DEFAULT true",
        "enable_relation_match": "BOOLEAN DEFAULT true",
        "enable_history_match": "BOOLEAN DEFAULT true",
        "enable_rag_match": "BOOLEAN DEFAULT true",
        "enable_llm_support": "BOOLEAN DEFAULT true",
        "exact_priority": "INTEGER DEFAULT 1",
        "alias_priority": "INTEGER DEFAULT 2",
        "relation_priority": "INTEGER DEFAULT 3",
        "history_priority": "INTEGER DEFAULT 4",
        "rag_priority": "INTEGER DEFAULT 5",
        "llm_priority": "INTEGER DEFAULT 6",
        "customer_weight": "INTEGER DEFAULT 20",
        "product_weight": "INTEGER DEFAULT 35",
        "quantities_weight": "INTEGER DEFAULT 15",
        "history_weight": "INTEGER DEFAULT 10",
        "coherence_weight": "INTEGER DEFAULT 10",
        "rag_weight": "INTEGER DEFAULT 5",
        "llm_weight": "INTEGER DEFAULT 5",
        "min_alias_confidence": "FLOAT DEFAULT 0.85",
        "min_history_frequency": "INTEGER DEFAULT 3",
        "min_product_frequency": "INTEGER DEFAULT 2",
        "max_doubtful_lines": "INTEGER DEFAULT 1",
        "learning_mode": "VARCHAR(50) DEFAULT 'supervisado'",
        "always_human_review": "BOOLEAN DEFAULT true",
        "auto_approve_aliases": "BOOLEAN DEFAULT false",
        "block_new_customer": "BOOLEAN DEFAULT false",
        "block_conflicting_aliases": "BOOLEAN DEFAULT true",
        "block_missing_quantity": "BOOLEAN DEFAULT true",
        "block_missing_reference": "BOOLEAN DEFAULT true",
        "updated_at": "DATETIME",
    },
    "email_settings": {
        "provider": "VARCHAR(50) DEFAULT 'imap'",
        "connection_method": "VARCHAR(50) DEFAULT 'password'",
        "client_id": "VARCHAR(255)",
        "client_secret_encrypted": "TEXT",
        "tenant_id": "VARCHAR(255)",
        "redirect_uri": "VARCHAR(500)",
        "access_token_encrypted": "TEXT",
        "refresh_token_encrypted": "TEXT",
        "connected_email": "VARCHAR(255)",
        "imap_host": "VARCHAR(255)",
        "imap_port": "INTEGER DEFAULT 993",
        "imap_use_ssl": "BOOLEAN DEFAULT true",
        "imap_security": "VARCHAR(30) DEFAULT 'ssl_tls'",
        "imap_username": "VARCHAR(255)",
        "imap_password_encrypted": "TEXT",
        "test_read_limit": "INTEGER DEFAULT 10",
        "oauth_scopes": "TEXT",
        "mailbox": "VARCHAR(255)",
        "inbox_folder": "VARCHAR(100) DEFAULT 'INBOX'",
        "processed_folder": "VARCHAR(100)",
        "error_folder": "VARCHAR(100)",
        "no_order_folder": "VARCHAR(100)",
        "doubtful_folder": "VARCHAR(100)",
        "read_limit": "INTEGER DEFAULT 25",
        "auto_sync_enabled": "BOOLEAN DEFAULT false",
        "read_unread_only": "BOOLEAN DEFAULT true",
        "read_from_date": "VARCHAR(50)",
        "initial_history_mode": "VARCHAR(30) DEFAULT 'new'",
        "initial_history_limit": "INTEGER DEFAULT 50",
        "mark_as_read_after_import": "BOOLEAN DEFAULT false",
        "move_after_processing": "BOOLEAN DEFAULT false",
        "smtp_provider": "VARCHAR(50) DEFAULT 'smtp'",
        "smtp_enabled": "BOOLEAN DEFAULT false",
        "smtp_host": "VARCHAR(255)",
        "smtp_port": "INTEGER DEFAULT 587",
        "smtp_security": "VARCHAR(30) DEFAULT 'starttls'",
        "smtp_username": "VARCHAR(255)",
        "smtp_password_encrypted": "TEXT",
        "from_email": "VARCHAR(255)",
        "from_name": "VARCHAR(255)",
        "reply_to": "VARCHAR(255)",
        "default_cc": "TEXT",
        "default_bcc": "TEXT",
        "save_internal_copy": "BOOLEAN DEFAULT true",
        "preserve_thread_headers": "BOOLEAN DEFAULT true",
        "auto_process_on_fetch": "BOOLEAN DEFAULT false",
        "process_only_with_attachments": "BOOLEAN DEFAULT false",
        "process_only_with_pdf": "BOOLEAN DEFAULT false",
        "process_without_attachments": "BOOLEAN DEFAULT true",
        "process_read_emails": "BOOLEAN DEFAULT false",
        "avoid_duplicates_by_message_id": "BOOLEAN DEFAULT true",
        "allow_reprocess": "BOOLEAN DEFAULT false",
        "auto_create_order_if_detected": "BOOLEAN DEFAULT true",
        "always_human_review": "BOOLEAN DEFAULT true",
        "mark_doubtful_below_threshold": "BOOLEAN DEFAULT true",
        "mark_no_order_if_detected": "BOOLEAN DEFAULT true",
        "action_order_detected": "VARCHAR(80) DEFAULT 'move_processed'",
        "action_no_order": "VARCHAR(80) DEFAULT 'move_no_order'",
        "action_doubtful": "VARCHAR(80) DEFAULT 'move_doubtful'",
        "action_error": "VARCHAR(80) DEFAULT 'move_error'",
        "minimum_score_auto_order": "INTEGER DEFAULT 90",
        "visible_states": "TEXT DEFAULT 'pending,processing,pedido,no_pedido,dudoso,error_processing,pending_reprocess,responded,closed'",
        "default_filter": "VARCHAR(80) DEFAULT 'all'",
        "default_date_range": "VARCHAR(80) DEFAULT 'today'",
        "default_page_size": "INTEGER DEFAULT 25",
        "default_sort": "VARCHAR(80) DEFAULT 'date_desc'",
        "show_summary_cards": "BOOLEAN DEFAULT true",
        "show_score_column": "BOOLEAN DEFAULT true",
        "show_customer_column": "BOOLEAN DEFAULT true",
        "show_attachments_column": "BOOLEAN DEFAULT true",
        "show_order_column": "BOOLEAN DEFAULT true",
        "show_reply_button": "BOOLEAN DEFAULT true",
        "show_process_button": "BOOLEAN DEFAULT true",
        "signature_text": "TEXT DEFAULT 'Equipo de pedidos'",
        "signature_html": "TEXT",
        "use_signature": "BOOLEAN DEFAULT true",
        "include_logo_in_signature": "BOOLEAN DEFAULT false",
        "legal_footer": "TEXT",
        "last_imap_test_at": "DATETIME",
        "last_imap_test_ok": "BOOLEAN",
        "last_imap_test_message": "TEXT",
        "last_sync_at": "DATETIME",
        "last_sync_ok": "BOOLEAN",
        "last_sync_message": "TEXT",
        "last_sync_error": "TEXT",
        "last_sync_new": "INTEGER DEFAULT 0",
        "last_sync_duplicates": "INTEGER DEFAULT 0",
        "last_smtp_test_at": "DATETIME",
        "last_smtp_test_ok": "BOOLEAN",
        "last_smtp_test_message": "TEXT",
        "updated_by": "INTEGER",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    },
    "emails": {
        "conversation_id": "INTEGER",
        "agent_status": "VARCHAR(80) DEFAULT 'not_processed'",
        "message_id": "VARCHAR(255)",
        "imap_mailbox": "VARCHAR(255)",
        "imap_uidvalidity": "VARCHAR(120)",
        "imap_uid": "VARCHAR(120)",
        "is_read": "BOOLEAN DEFAULT false",
        "is_favorite": "BOOLEAN DEFAULT false",
        "archived": "BOOLEAN DEFAULT false",
        "has_attachments": "BOOLEAN DEFAULT false",
        "has_pdf": "BOOLEAN DEFAULT false",
        "processing_error": "TEXT",
        "processing_result_json": "TEXT",
        "last_processed_at": "DATETIME",
    },
    "email_attachments": {
        "size_bytes": "INTEGER DEFAULT 0",
        "is_pdf": "BOOLEAN DEFAULT false",
        "extraction_status": "VARCHAR(80) DEFAULT 'pending'",
        "extraction_error": "TEXT",
    },
    "llm_settings": {
        "agent_enabled": "BOOLEAN DEFAULT true",
        "agent_mode": "VARCHAR(80) DEFAULT 'semiautomatico'",
        "safety_level": "VARCHAR(50) DEFAULT 'equilibrado'",
        "use_same_model_for_all": "BOOLEAN DEFAULT true",
        "can_read_email": "BOOLEAN DEFAULT true",
        "can_extract_pdf": "BOOLEAN DEFAULT true",
        "can_classify_email": "BOOLEAN DEFAULT true",
        "can_extract_order": "BOOLEAN DEFAULT true",
        "can_suggest_customer": "BOOLEAN DEFAULT true",
        "can_suggest_products": "BOOLEAN DEFAULT true",
        "can_calculate_score": "BOOLEAN DEFAULT true",
        "can_create_pending_order": "BOOLEAN DEFAULT true",
        "can_mark_no_order": "BOOLEAN DEFAULT true",
        "can_reply_customer": "BOOLEAN DEFAULT false",
        "allow_auto_confirm": "BOOLEAN DEFAULT false",
        "allow_auto_export": "BOOLEAN DEFAULT false",
        "daily_cost_limit": "FLOAT DEFAULT 0",
        "batch_limit": "INTEGER DEFAULT 25",
        "detailed_llm_logs": "BOOLEAN DEFAULT false",
        "store_llm_payloads": "BOOLEAN DEFAULT false",
        "anonymize_llm_logs": "BOOLEAN DEFAULT true",
        "debug_mode": "BOOLEAN DEFAULT false",
        "organization_id": "VARCHAR(255)",
        "project_id": "VARCHAR(255)",
        "azure_deployment_name": "VARCHAR(255)",
        "last_test_at": "DATETIME",
        "last_test_ok": "BOOLEAN",
        "last_test_message": "TEXT",
        "last_error": "TEXT",
        "last_response_ms": "INTEGER",
        "updated_by": "INTEGER",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    },
    "imports": {
        "rows_ignored": "INTEGER DEFAULT 0",
        "mapping_used": "TEXT",
        "user_id": "INTEGER",
    },
    "prompt_versions": {
        "created_by_user_id": "INTEGER",
    },
    "branding_settings": {
        "dark_logo_url": "VARCHAR(500)",
        "favicon_url": "VARCHAR(500)",
        "updated_by": "INTEGER",
    },
    "input_channels": {
        "channel_type": "VARCHAR(50) DEFAULT 'message'",
        "is_active": "BOOLEAN DEFAULT true",
        "is_default": "BOOLEAN DEFAULT false",
        "supports_text": "BOOLEAN DEFAULT true",
        "supports_attachments": "BOOLEAN DEFAULT true",
        "supports_audio": "BOOLEAN DEFAULT false",
        "supports_documents": "BOOLEAN DEFAULT false",
        "supports_images": "BOOLEAN DEFAULT false",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    },
    "conversations": {
        "channel_id": "INTEGER",
        "provider": "VARCHAR(50) DEFAULT 'imap'",
        "external_thread_id": "VARCHAR(255)",
        "customer_id": "INTEGER",
        "assigned_user_id": "INTEGER",
        "status": "VARCHAR(50) DEFAULT 'open'",
        "subject": "VARCHAR(500)",
        "last_activity_at": "DATETIME",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    },
    "channel_settings": {
        "value_type": "VARCHAR(50) DEFAULT 'string'",
        "is_secret": "BOOLEAN DEFAULT false",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    },
    "inbound_messages": {
        "channel_id": "INTEGER",
        "provider": "VARCHAR(50) DEFAULT 'imap'",
        "conversation_id": "INTEGER",
        "source_thread_id": "VARCHAR(255)",
        "source_message_id": "VARCHAR(255)",
        "source_mailbox": "VARCHAR(255)",
        "source_uidvalidity": "VARCHAR(120)",
        "source_uid": "VARCHAR(120)",
        "direction": "VARCHAR(30) DEFAULT 'inbound'",
        "recipient": "VARCHAR(255)",
        "original_content": "TEXT",
        "raw_payload_json": "TEXT",
        "content_type": "VARCHAR(80)",
        "processing_step": "VARCHAR(80) DEFAULT 'received'",
        "normalized_text": "TEXT",
        "classification_json": "TEXT",
        "extraction_json": "TEXT",
        "customer_id": "INTEGER",
        "order_id": "INTEGER",
        "score": "FLOAT DEFAULT 0",
        "has_audio": "BOOLEAN DEFAULT false",
        "processing_error": "TEXT",
        "last_processed_at": "DATETIME",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    },
    "message_attachments": {
        "ocr_text": "TEXT",
        "transcription_text": "TEXT",
        "is_image": "BOOLEAN DEFAULT false",
        "is_audio": "BOOLEAN DEFAULT false",
        "created_at": "DATETIME",
    },
    "normalized_inputs": {
        "metadata_json": "TEXT",
        "created_at": "DATETIME",
    },
    "order_reviews": {
        "reviewer_user_id": "INTEGER",
        "status": "VARCHAR(80) DEFAULT 'pending'",
        "comments": "TEXT",
        "reviewed_at": "DATETIME",
        "created_at": "DATETIME",
    },
    "manual_corrections": {
        "inbound_message_id": "INTEGER",
        "order_id": "INTEGER",
        "order_line_id": "INTEGER",
        "agent_value": "TEXT",
        "corrected_entity_id": "INTEGER",
        "should_learn": "BOOLEAN DEFAULT false",
        "created_at": "DATETIME",
    },
    "learned_aliases": {
        "source_correction_id": "INTEGER",
        "approved": "BOOLEAN DEFAULT false",
        "approved_by": "INTEGER",
        "updated_at": "DATETIME",
    },
    "rag_cases": {
        "case_type": "VARCHAR(80)",
        "input_text": "TEXT",
        "agent_initial_result_json": "TEXT",
        "human_corrected_result_json": "TEXT",
        "resolution_summary": "TEXT",
        "linked_customer_id": "INTEGER",
        "embedding_id": "VARCHAR(255)",
        "usefulness_score": "FLOAT DEFAULT 0",
        "approved_for_retrieval": "BOOLEAN DEFAULT false",
    },
    "scoring_results": {
        "inbound_message_id": "INTEGER",
        "customer_score": "FLOAT DEFAULT 0",
        "product_score": "FLOAT DEFAULT 0",
        "confidence_score": "FLOAT DEFAULT 0",
        "rule_score": "FLOAT DEFAULT 0",
        "block_reason": "TEXT",
        "details_json": "TEXT",
        "created_at": "DATETIME",
    },
    "export_jobs": {
        "file_path": "VARCHAR(500)",
        "remote_path": "VARCHAR(500)",
        "export_format": "VARCHAR(50) DEFAULT 'csv'",
        "destination_type": "VARCHAR(50) DEFAULT 'sftp'",
        "status_message": "TEXT",
        "payload_json": "TEXT",
        "exported_at": "DATETIME",
    },
    "background_jobs": {
        "dedupe_key": "VARCHAR(255)",
        "attempt_count": "INTEGER DEFAULT 0",
        "max_retries": "INTEGER DEFAULT 3",
        "next_retry_at": "DATETIME",
        "last_error_at": "DATETIME",
        "last_error_type": "VARCHAR(120)",
        "last_heartbeat_at": "DATETIME",
    },
    "alerts": {
        "inbound_message_id": "INTEGER",
        "severity": "VARCHAR(50) DEFAULT 'medium'",
        "status": "VARCHAR(50) DEFAULT 'open'",
        "payload_json": "TEXT",
        "resolved_at": "DATETIME",
    },
    "agent_logs": {
        "channel_id": "INTEGER",
        "inbound_message_id": "INTEGER",
        "order_id": "INTEGER",
        "level": "VARCHAR(20) DEFAULT 'info'",
        "event": "VARCHAR(120)",
        "payload_json": "TEXT",
    },
}

TENANT_JOB_COLUMNS = {
    "result_json": "TEXT",
    "error_message": "TEXT",
    "created_by_user_id": "INTEGER",
    "progress": "INTEGER DEFAULT 0",
    "dedupe_key": "VARCHAR(255)",
    "retry_count": "INTEGER DEFAULT 0",
    "attempt_count": "INTEGER DEFAULT 0",
    "max_retries": "INTEGER DEFAULT 3",
    "lock_owner": "VARCHAR(120)",
    "lock_until": "DATETIME",
    "next_retry_at": "DATETIME",
    "last_error_at": "DATETIME",
    "last_error_type": "VARCHAR(120)",
    "last_heartbeat_at": "DATETIME",
    "queued_at": "DATETIME",
    "started_at": "DATETIME",
    "finished_at": "DATETIME",
    "updated_at": "DATETIME",
}


MASTER_EMAIL_SYNC_STATE_COLUMNS = {
    "mailbox": "VARCHAR(255)",
    "uidvalidity": "VARCHAR(120)",
    "source_provider": "VARCHAR(50)",
    "source_host": "VARCHAR(255)",
    "source_username": "VARCHAR(255)",
    "source_connected_email": "VARCHAR(255)",
    "last_successful_sync_at": "TIMESTAMP WITH TIME ZONE",
    "last_error_type": "VARCHAR(120)",
    "sync_status": "VARCHAR(50) DEFAULT 'idle'",
    "listener_status": "VARCHAR(50) DEFAULT 'inactive'",
    "listener_owner": "VARCHAR(120)",
    "listener_last_started_at": "TIMESTAMP WITH TIME ZONE",
    "listener_last_heartbeat_at": "TIMESTAMP WITH TIME ZONE",
    "listener_last_error_at": "TIMESTAMP WITH TIME ZONE",
    "listener_last_error_message": "TEXT",
    "last_checkpoint_uid": "VARCHAR(120)",
    "backfill_status": "VARCHAR(50) DEFAULT 'idle'",
    "backfill_total": "INTEGER DEFAULT 0",
    "backfill_processed": "INTEGER DEFAULT 0",
    "backfill_created": "INTEGER DEFAULT 0",
    "backfill_duplicates": "INTEGER DEFAULT 0",
    "backfill_errors": "INTEGER DEFAULT 0",
    "backfill_last_uid": "VARCHAR(120)",
    "backfill_checkpoint_json": "TEXT",
    "backfill_started_at": "TIMESTAMP WITH TIME ZONE",
    "backfill_last_checkpoint_at": "TIMESTAMP WITH TIME ZONE",
    "backfill_paused_at": "TIMESTAMP WITH TIME ZONE",
    "backfill_completed_at": "TIMESTAMP WITH TIME ZONE",
    "backfill_cancelled_at": "TIMESTAMP WITH TIME ZONE",
}

MASTER_EMAIL_LISTENER_COLUMNS = {
    "listener_status": "VARCHAR(50) DEFAULT 'inactive'",
    "listener_owner": "VARCHAR(120)",
    "listener_last_started_at": "TIMESTAMP WITH TIME ZONE",
    "listener_last_heartbeat_at": "TIMESTAMP WITH TIME ZONE",
    "listener_last_error_at": "TIMESTAMP WITH TIME ZONE",
    "listener_last_error_message": "TEXT",
}

MAILBOX_SYNC_STATE_COLUMNS = {
    "company_id": "INTEGER",
    "mailbox_id": "INTEGER",
    "uidvalidity": "VARCHAR(120)",
    "source_provider": "VARCHAR(50)",
    "source_host": "VARCHAR(255)",
    "source_username": "VARCHAR(255)",
    "source_connected_email": "VARCHAR(255)",
    "enabled": "BOOLEAN DEFAULT true",
    "frequency_seconds": "INTEGER DEFAULT 60",
    "last_sync_at": "TIMESTAMP WITH TIME ZONE",
    "last_success_at": "TIMESTAMP WITH TIME ZONE",
    "last_successful_sync_at": "TIMESTAMP WITH TIME ZONE",
    "last_error_at": "TIMESTAMP WITH TIME ZONE",
    "last_error_type": "VARCHAR(120)",
    "last_error_message": "TEXT",
    "next_run_at": "TIMESTAMP WITH TIME ZONE",
    "status": "VARCHAR(50) DEFAULT 'idle'",
    "sync_status": "VARCHAR(50) DEFAULT 'idle'",
    "listener_status": "VARCHAR(50) DEFAULT 'inactive'",
    "listener_owner": "VARCHAR(120)",
    "listener_last_started_at": "TIMESTAMP WITH TIME ZONE",
    "listener_last_heartbeat_at": "TIMESTAMP WITH TIME ZONE",
    "listener_last_error_at": "TIMESTAMP WITH TIME ZONE",
    "listener_last_error_message": "TEXT",
    "lock_until": "TIMESTAMP WITH TIME ZONE",
    "lock_owner": "VARCHAR(120)",
    "last_seen_uid": "VARCHAR(120)",
    "last_checkpoint_uid": "VARCHAR(120)",
    "backfill_status": "VARCHAR(50) DEFAULT 'idle'",
    "backfill_total": "INTEGER DEFAULT 0",
    "backfill_processed": "INTEGER DEFAULT 0",
    "backfill_created": "INTEGER DEFAULT 0",
    "backfill_duplicates": "INTEGER DEFAULT 0",
    "backfill_errors": "INTEGER DEFAULT 0",
    "backfill_last_uid": "VARCHAR(120)",
    "backfill_checkpoint_json": "TEXT",
    "backfill_started_at": "TIMESTAMP WITH TIME ZONE",
    "backfill_last_checkpoint_at": "TIMESTAMP WITH TIME ZONE",
    "backfill_paused_at": "TIMESTAMP WITH TIME ZONE",
    "backfill_completed_at": "TIMESTAMP WITH TIME ZONE",
    "backfill_cancelled_at": "TIMESTAMP WITH TIME ZONE",
    "created_at": "TIMESTAMP WITH TIME ZONE",
    "updated_at": "TIMESTAMP WITH TIME ZONE",
}

COMMUNICATION_COLUMNS = {
    "company_id": "INTEGER",
    "mailbox_id": "INTEGER",
    "external_message_id": "VARCHAR(255)",
    "thread_id": "VARCHAR(255)",
    "provider": "VARCHAR(50) DEFAULT 'imap'",
    "sender_email": "VARCHAR(255)",
    "sender_name": "VARCHAR(255)",
    "to_recipients": "TEXT",
    "cc_recipients": "TEXT",
    "bcc_recipients": "TEXT",
    "subject": "VARCHAR(500)",
    "body_text": "TEXT",
    "body_html": "TEXT",
    "metadata_json": "TEXT",
    "received_at": "TIMESTAMP WITH TIME ZONE",
    "processing_status": "VARCHAR(50) DEFAULT 'received'",
    "routing_status": "VARCHAR(50) DEFAULT 'unclassified'",
    "created_at": "TIMESTAMP WITH TIME ZONE",
    "updated_at": "TIMESTAMP WITH TIME ZONE",
}

COMMUNICATION_ATTACHMENT_COLUMNS = {
    "company_id": "INTEGER",
    "communication_id": "INTEGER",
    "filename": "VARCHAR(255)",
    "mime_type": "VARCHAR(120)",
    "size_bytes": "INTEGER DEFAULT 0",
    "storage_ref": "VARCHAR(500)",
    "extracted_text": "TEXT",
    "extraction_status": "VARCHAR(80) DEFAULT 'pending'",
    "extraction_error": "TEXT",
    "checksum": "VARCHAR(64)",
    "metadata_json": "TEXT",
    "created_at": "TIMESTAMP WITH TIME ZONE",
    "updated_at": "TIMESTAMP WITH TIME ZONE",
}

DEPARTMENT_COLUMNS = {
    "company_id": "INTEGER",
    "name": "VARCHAR(150)",
    "description": "TEXT",
    "destination_email": "VARCHAR(255)",
    "active": "BOOLEAN DEFAULT true",
    "created_at": "TIMESTAMP WITH TIME ZONE",
    "updated_at": "TIMESTAMP WITH TIME ZONE",
}

DEPARTMENT_KNOWLEDGE_COLUMNS = {
    "department_id": "INTEGER",
    "title": "VARCHAR(200)",
    "content": "TEXT",
    "knowledge_type": "VARCHAR(30)",
    "active": "BOOLEAN DEFAULT true",
    "created_at": "TIMESTAMP WITH TIME ZONE",
    "updated_at": "TIMESTAMP WITH TIME ZONE",
}

DEPARTMENT_MEMBER_COLUMNS = {
    "company_id": "INTEGER",
    "department_id": "INTEGER",
    "user_id": "INTEGER",
    "active": "BOOLEAN DEFAULT true",
    "role": "VARCHAR(100)",
    "created_at": "TIMESTAMP WITH TIME ZONE",
}

RACI_ASSIGNMENT_COLUMNS = {
    "company_id": "INTEGER",
    "department_id": "INTEGER",
    "user_id": "INTEGER",
    "scope": "VARCHAR(150) DEFAULT 'department'",
    "raci_role": "VARCHAR(15)",
    "active": "BOOLEAN DEFAULT true",
    "created_at": "TIMESTAMP WITH TIME ZONE",
}

ROUTING_DECISION_COLUMNS = {
    "company_id": "INTEGER",
    "communication_id": "INTEGER",
    "department_id": "INTEGER",
    "alternative_department_id": "INTEGER",
    "final_department_id": "INTEGER",
    "category": "VARCHAR(100)",
    "final_category": "VARCHAR(100)",
    "confidence": "DOUBLE PRECISION",
    "requires_review": "BOOLEAN DEFAULT true",
    "reason": "TEXT",
    "ambiguity_reason": "TEXT",
    "status": "VARCHAR(40) DEFAULT 'pending_review'",
    "source": "VARCHAR(30) DEFAULT 'agent'",
    "analysis_number": "INTEGER DEFAULT 1",
    "reviewed_by_user_id": "INTEGER",
    "reviewed_at": "TIMESTAMP WITH TIME ZONE",
    "created_at": "TIMESTAMP WITH TIME ZONE",
    "updated_at": "TIMESTAMP WITH TIME ZONE",
}

ROUTING_CORRECTION_COLUMNS = {
    "company_id": "INTEGER",
    "routing_decision_id": "INTEGER",
    "communication_id": "INTEGER",
    "original_department_id": "INTEGER",
    "corrected_department_id": "INTEGER",
    "original_category": "VARCHAR(100)",
    "corrected_category": "VARCHAR(100)",
    "reason": "TEXT",
    "corrected_by_user_id": "INTEGER",
    "created_at": "TIMESTAMP WITH TIME ZONE",
}


def _apply_tenant_metadata(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.db.models import TenantSchemaMigration

    actions: list[str] = []
    with engine.connect() as conn:
        if "schema_migrations" not in inspect(conn).get_table_names():
            actions.append("CREATE TABLE schema_migrations (...)")
            if not dry_run:
                TenantSchemaMigration.__table__.create(bind=engine, checkfirst=True)
        elif not dry_run:
            TenantSchemaMigration.__table__.create(bind=engine, checkfirst=True)
    actions.extend(ensure_columns(engine, "schema_migrations", TENANT_MIGRATION_COLUMNS, dry_run=dry_run))
    return actions


def _apply_tenant_compatibility(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    actions: list[str] = []
    for table_name, columns in TENANT_COMPAT_COLUMNS.items():
        actions.extend(ensure_columns(engine, table_name, columns, dry_run=dry_run))
    return actions


def _apply_tenant_mailboxes(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from sqlalchemy import MetaData, Table, insert, select

    from app.db.models import Mailbox

    actions: list[str] = []
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    if "mailboxes" not in tables:
        actions.append("CREATE TABLE mailboxes (...)")
        if not dry_run:
            Mailbox.__table__.create(bind=engine, checkfirst=True)
    elif not dry_run:
        Mailbox.__table__.create(bind=engine, checkfirst=True)
    actions.extend(ensure_columns(engine, "emails", {"mailbox_id": "INTEGER"}, dry_run=dry_run))
    actions.extend(ensure_columns(engine, "inbound_messages", {"mailbox_id": "INTEGER"}, dry_run=dry_run))
    actions.extend(ensure_unique_index(engine, "mailboxes", "uq_mailboxes_company_email", ("company_id", "email_address"), dry_run=dry_run))
    if dry_run or "email_settings" not in tables:
        return actions

    metadata = MetaData()
    email_settings_table = Table("email_settings", metadata, autoload_with=engine)
    mailboxes_table = Table("mailboxes", metadata, autoload_with=engine)
    copy_fields = (
        "provider", "connection_method", "client_id", "client_secret_encrypted", "tenant_id",
        "redirect_uri", "access_token_encrypted", "refresh_token_encrypted", "connected_email",
        "imap_host", "imap_port", "imap_use_ssl", "imap_security", "imap_username",
        "imap_password_encrypted", "mailbox", "inbox_folder", "processed_folder", "error_folder",
        "no_order_folder", "doubtful_folder", "read_limit", "auto_sync_enabled", "read_unread_only",
        "read_from_date", "polling_frequency_minutes", "mark_as_read_after_import", "move_after_processing", "smtp_provider",
        "smtp_enabled", "smtp_host", "smtp_port", "smtp_security", "smtp_username",
        "smtp_password_encrypted", "from_email", "from_name", "reply_to", "default_cc", "default_bcc",
        "save_internal_copy", "preserve_thread_headers", "auto_process_on_fetch", "updated_by",
    )
    with engine.begin() as conn:
        for row in conn.execute(select(email_settings_table)).mappings():
            email_address = (row.get("connected_email") or row.get("imap_username") or "").strip().lower()
            if not email_address:
                continue
            already_exists = conn.execute(
                select(mailboxes_table.c.id).where(
                    mailboxes_table.c.company_id == row["company_id"],
                    mailboxes_table.c.email_address == email_address,
                )
            ).first()
            if already_exists:
                continue
            values = {
                field: row.get(field)
                for field in copy_fields
                if field in mailboxes_table.c and row.get(field) is not None
            }
            values.update(
                {
                    "company_id": row["company_id"],
                    "name": f"Correo {email_address}",
                    "email_address": email_address,
                    "connected_email": row.get("connected_email") or email_address,
                    "enabled": True,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            conn.execute(insert(mailboxes_table).values(**values))
    return actions


def _apply_tenant_communications(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.db.models import Communication, CommunicationAttachment

    actions: list[str] = []
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    if "communications" not in tables:
        actions.append("CREATE TABLE communications (...)")
        if not dry_run:
            Communication.__table__.create(bind=engine, checkfirst=True)
    elif not dry_run:
        Communication.__table__.create(bind=engine, checkfirst=True)
    if "communication_attachments" not in tables:
        actions.append("CREATE TABLE communication_attachments (...)")
        if not dry_run:
            CommunicationAttachment.__table__.create(bind=engine, checkfirst=True)
    elif not dry_run:
        CommunicationAttachment.__table__.create(bind=engine, checkfirst=True)
    actions.extend(ensure_columns(engine, "communications", COMMUNICATION_COLUMNS, dry_run=dry_run))
    actions.extend(ensure_columns(engine, "communication_attachments", COMMUNICATION_ATTACHMENT_COLUMNS, dry_run=dry_run))
    actions.extend(ensure_columns(engine, "emails", {"communication_id": "INTEGER"}, dry_run=dry_run))
    actions.extend(
        ensure_unique_index(
            engine,
            "communications",
            "uq_communications_identity",
            ("company_id", "mailbox_id", "provider", "external_message_id"),
            dry_run=dry_run,
        )
    )
    return actions


def _ensure_raci_null_user_index(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    index_name = "uq_raci_assignments_null_user"
    statement = (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_raci_assignments_null_user "
        "ON raci_assignments (company_id, department_id, scope, raci_role) "
        "WHERE user_id IS NULL"
    )
    with engine.connect() as conn:
        if "raci_assignments" not in inspect(conn).get_table_names():
            return []
        existing_indexes = {index["name"] for index in inspect(conn).get_indexes("raci_assignments")}
    if index_name in existing_indexes:
        return []
    if not dry_run:
        with engine.begin() as conn:
            conn.execute(text(statement))
    return [statement]


def _apply_tenant_departments(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.db.models import Department, DepartmentKnowledge, DepartmentMember, RaciAssignment

    actions: list[str] = []
    models = (
        ("departments", Department, DEPARTMENT_COLUMNS),
        ("department_knowledge", DepartmentKnowledge, DEPARTMENT_KNOWLEDGE_COLUMNS),
        ("department_members", DepartmentMember, DEPARTMENT_MEMBER_COLUMNS),
        ("raci_assignments", RaciAssignment, RACI_ASSIGNMENT_COLUMNS),
    )
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    for table_name, model, columns in models:
        if table_name == "raci_assignments":
            actions.extend(
                ensure_unique_index(
                    engine,
                    "departments",
                    "uq_departments_company_id",
                    ("company_id", "id"),
                    dry_run=dry_run,
                )
            )
            actions.extend(
                ensure_unique_index(
                    engine,
                    "users",
                    "uq_users_company_id",
                    ("company_id", "id"),
                    dry_run=dry_run,
                )
            )
        if table_name not in tables:
            actions.append(f"CREATE TABLE {table_name} (...)")
            if not dry_run:
                model.__table__.create(bind=engine, checkfirst=True)
        elif not dry_run:
            model.__table__.create(bind=engine, checkfirst=True)
        actions.extend(ensure_columns(engine, table_name, columns, dry_run=dry_run))

    actions.extend(
        ensure_unique_index(
            engine,
            "departments",
            "uq_departments_company_name",
            ("company_id", "name"),
            dry_run=dry_run,
        )
    )
    actions.extend(
        ensure_unique_index(
            engine,
            "department_knowledge",
            "uq_department_knowledge_title_type",
            ("department_id", "title", "knowledge_type"),
            dry_run=dry_run,
        )
    )
    actions.extend(
        ensure_unique_index(
            engine,
            "department_members",
            "uq_department_members_company_department_user",
            ("company_id", "department_id", "user_id"),
            dry_run=dry_run,
        )
    )
    actions.extend(
        ensure_unique_index(
            engine,
            "raci_assignments",
            "uq_raci_assignments_identity",
            ("company_id", "department_id", "user_id", "scope", "raci_role"),
            dry_run=dry_run,
        )
    )
    actions.extend(_ensure_raci_null_user_index(engine, dry_run=dry_run))
    return actions


def _apply_tenant_routing_decisions(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.db.models import RoutingCorrection, RoutingDecision

    actions: list[str] = []
    models = (
        ("routing_decisions", RoutingDecision, ROUTING_DECISION_COLUMNS),
        ("routing_corrections", RoutingCorrection, ROUTING_CORRECTION_COLUMNS),
    )
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    for table_name, model, columns in models:
        if table_name not in tables:
            actions.append(f"CREATE TABLE {table_name} (...)")
        if not dry_run:
            model.__table__.create(bind=engine, checkfirst=True)
        actions.extend(ensure_columns(engine, table_name, columns, dry_run=dry_run))
    actions.extend(
        ensure_unique_index(
            engine,
            "communications",
            "uq_communications_company_id",
            ("company_id", "id"),
            dry_run=dry_run,
        )
    )
    actions.extend(
        ensure_unique_index(
            engine,
            "routing_decisions",
            "uq_routing_decisions_identity",
            ("company_id", "communication_id", "analysis_number"),
            dry_run=dry_run,
        )
    )
    return actions


def _apply_tenant_job_reliability(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.db.models import JobAttempt

    actions: list[str] = []
    with engine.connect() as conn:
        if "job_attempts" not in inspect(conn).get_table_names():
            actions.append("CREATE TABLE job_attempts (...)")
            if not dry_run:
                JobAttempt.__table__.create(bind=engine, checkfirst=True)
        elif not dry_run:
            JobAttempt.__table__.create(bind=engine, checkfirst=True)
    actions.extend(ensure_columns(engine, "background_jobs", TENANT_JOB_COLUMNS, dry_run=dry_run))
    actions.extend(ensure_unique_index(engine, "background_jobs", "uq_background_jobs_dedupe", ("company_id", "job_type", "dedupe_key"), dry_run=dry_run))
    actions.extend(ensure_unique_index(engine, "job_attempts", "uq_job_attempts_number", ("job_id", "attempt_number"), dry_run=dry_run))
    return actions


def _apply_tenant_messages(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from sqlalchemy import MetaData, Table, func, insert, select
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Conversation, Email, InboundMessage, InputChannel, Order
    from app.messages.service import get_or_create_conversation
    from app.migrations.runner import MigrationError

    actions: list[str] = []
    with engine.connect() as conn:
        if "conversations" not in inspect(conn).get_table_names():
            actions.append("CREATE TABLE conversations (...)")
            if not dry_run:
                Conversation.__table__.create(bind=engine, checkfirst=True)
        elif not dry_run:
            Conversation.__table__.create(bind=engine, checkfirst=True)
    actions.extend(ensure_columns(engine, "emails", {"conversation_id": "INTEGER"}, dry_run=dry_run))
    actions.extend(ensure_columns(engine, "inbound_messages", {"provider": "VARCHAR(50) DEFAULT 'imap'", "conversation_id": "INTEGER"}, dry_run=dry_run))
    actions.extend(ensure_columns(engine, "orders", {"conversation_id": "INTEGER"}, dry_run=dry_run))
    actions.extend(ensure_unique_index(engine, "conversations", "uq_conversations_thread", ("company_id", "channel_id", "provider", "external_thread_id"), dry_run=dry_run))
    actions.extend(ensure_unique_index(engine, "inbound_messages", "uq_inbound_messages_dedupe", ("company_id", "channel_id", "provider", "source_external_id"), dry_run=dry_run))
    if dry_run:
        return actions

    def _cell(row, column_name: str, default=None):  # noqa: ANN001
        if hasattr(row, "_mapping"):
            return row._mapping.get(column_name, default)
        return getattr(row, column_name, default)

    metadata = MetaData()
    emails_table = Table("emails", metadata, autoload_with=engine)
    inbound_table = Table("inbound_messages", metadata, autoload_with=engine)
    orders_table = Table("orders", metadata, autoload_with=engine)
    input_channels_table = Table("input_channels", metadata, autoload_with=engine)

    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        duplicate_inbound = db.execute(
            select(
                inbound_table.c.company_id,
                inbound_table.c.channel_id,
                inbound_table.c.provider,
                inbound_table.c.source_external_id,
                func.count().label("rows"),
            )
            .where(inbound_table.c.source_external_id.is_not(None))
            .group_by(
                inbound_table.c.company_id,
                inbound_table.c.channel_id,
                inbound_table.c.provider,
                inbound_table.c.source_external_id,
            )
            .having(func.count() > 1)
        ).mappings().all()
        if duplicate_inbound:
            first = duplicate_inbound[0]
            raise MigrationError(
                "Duplicados de inbound_messages impiden crear la unicidad: "
                f"company_id={first['company_id']} channel_id={first['channel_id']} provider={first['provider']} source_external_id={first['source_external_id']}"
            )
        existing_channels = db.execute(
            select(
                input_channels_table.c.id,
                input_channels_table.c.company_id,
                input_channels_table.c.key,
            ).where(input_channels_table.c.key == "email")
        ).mappings().all()
        email_channel_ids = {row["company_id"]: row["id"] for row in existing_channels}

        email_query = select(emails_table).order_by(emails_table.c.company_id, emails_table.c.id)
        for email in db.execute(email_query).mappings().all():
            company_id = _cell(email, "company_id")
            email_id = _cell(email, "id")
            channel_id = email_channel_ids.get(company_id)
            if not channel_id:
                if dry_run:
                    continue
                result = db.execute(
                    insert(input_channels_table).values(
                        company_id=company_id,
                        key="email",
                        name="Email",
                        channel_type="message",
                        is_active=True,
                        is_default=True,
                        supports_text=True,
                        supports_attachments=True,
                        supports_documents=True,
                    )
                )
                channel_id = result.lastrowid
                email_channel_ids[company_id] = channel_id
            conversation = get_or_create_conversation(
                db,
                company_id=company_id,
                channel_id=channel_id,
                provider="imap",
                external_thread_id=_cell(email, "external_id"),
                subject=_cell(email, "subject"),
                last_activity_at=_cell(email, "received_at"),
            )
            db.execute(emails_table.update().where(emails_table.c.id == email_id).values(conversation_id=conversation.id))

            inbound_where = [inbound_table.c.company_id == company_id]
            if _cell(email, "external_id") is not None:
                inbound_where.append(inbound_table.c.source_external_id == _cell(email, "external_id"))
            inbound_message = db.execute(select(inbound_table).where(*inbound_where)).mappings().one_or_none()
            if inbound_message:
                db.execute(
                    inbound_table.update()
                    .where(inbound_table.c.id == _cell(inbound_message, "id"))
                    .values(conversation_id=conversation.id, provider=_cell(inbound_message, "provider") or "imap")
                )
            order_rows = db.execute(
                select(orders_table.c.id).where(
                    orders_table.c.company_id == company_id,
                    orders_table.c.email_id == email_id,
                )
            ).mappings().all()
            for order_row in order_rows:
                db.execute(
                    orders_table.update()
                    .where(orders_table.c.id == order_row["id"])
                    .values(conversation_id=conversation.id)
                )

        message_query = select(inbound_table).order_by(inbound_table.c.company_id, inbound_table.c.id)
        for message in db.execute(message_query).mappings().all():
            if _cell(message, "conversation_id"):
                continue
            company_id = _cell(message, "company_id")
            message_id = _cell(message, "id")
            channel_id = _cell(message, "channel_id")
            if not channel_id:
                channel = db.execute(
                    select(input_channels_table.c.id).where(
                        input_channels_table.c.company_id == company_id,
                        input_channels_table.c.key == "email",
                    )
                ).mappings().one_or_none()
                if channel:
                    channel_id = channel["id"]
            if not channel_id:
                continue
            conversation = get_or_create_conversation(
                db,
                company_id=company_id,
                channel_id=channel_id,
                provider=_cell(message, "provider") or "imap",
                external_thread_id=_cell(message, "source_thread_id") or _cell(message, "source_external_id"),
                subject=_cell(message, "subject"),
                customer_id=_cell(message, "customer_id"),
                last_activity_at=_cell(message, "received_at"),
            )
            db.execute(inbound_table.update().where(inbound_table.c.id == message_id).values(conversation_id=conversation.id))
            if _cell(message, "order_id"):
                order_row = db.execute(
                    select(orders_table.c.id, orders_table.c.conversation_id).where(orders_table.c.company_id == company_id, orders_table.c.id == _cell(message, "order_id"))
                ).mappings().one_or_none()
                if order_row and not order_row.get("conversation_id"):
                    db.execute(orders_table.update().where(orders_table.c.id == order_row["id"]).values(conversation_id=conversation.id))
        db.commit()
    finally:
        db.close()
    return actions


def _apply_master_metadata(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.master.models import MasterSchemaMigration

    with engine.connect() as conn:
        if "schema_migrations" not in inspect(conn).get_table_names():
            if not dry_run:
                MasterSchemaMigration.__table__.create(bind=engine, checkfirst=True)
            return ["CREATE TABLE schema_migrations (...)"]
        if not dry_run:
            MasterSchemaMigration.__table__.create(bind=engine, checkfirst=True)
        return []


def _apply_master_email_sync_state(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.master.models import EmailSyncState

    actions: list[str] = []
    with engine.connect() as conn:
        if "email_sync_state" not in inspect(conn).get_table_names():
            actions.append("CREATE TABLE email_sync_state (...)")
            if not dry_run:
                EmailSyncState.__table__.create(bind=engine, checkfirst=True)
            return actions
        if not dry_run:
            EmailSyncState.__table__.create(bind=engine, checkfirst=True)
    actions.extend(
        ensure_columns(
            engine,
            "email_sync_state",
            MASTER_EMAIL_SYNC_STATE_COLUMNS,
            dry_run=dry_run,
        )
    )
    return actions


def _apply_master_email_listener_state(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    return ensure_columns(engine, "email_sync_state", MASTER_EMAIL_LISTENER_COLUMNS, dry_run=dry_run)


def _apply_master_email_sync_state_repair(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    return ensure_columns(engine, "email_sync_state", MASTER_EMAIL_SYNC_STATE_COLUMNS, dry_run=dry_run)


def _apply_master_mailbox_sync_state(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.master.models import MailboxSyncState

    actions: list[str] = []
    with engine.connect() as conn:
        if "mailbox_sync_state" not in inspect(conn).get_table_names():
            actions.append("CREATE TABLE mailbox_sync_state (...)")
            if not dry_run:
                MailboxSyncState.__table__.create(bind=engine, checkfirst=True)
            return actions
    if not dry_run:
        MailboxSyncState.__table__.create(bind=engine, checkfirst=True)
    actions.extend(ensure_columns(engine, "mailbox_sync_state", MAILBOX_SYNC_STATE_COLUMNS, dry_run=dry_run))
    actions.extend(ensure_unique_index(engine, "mailbox_sync_state", "uq_mailbox_sync_state_company_mailbox", ("company_id", "mailbox_id"), dry_run=dry_run))
    return actions


def _apply_tenant_ai_learning(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.db.models import LearningProposal, PromptExecution

    actions: list[str] = []
    with engine.connect() as conn:
        if "prompt_executions" not in inspect(conn).get_table_names():
            actions.append("CREATE TABLE prompt_executions (...)")
            if not dry_run:
                PromptExecution.__table__.create(bind=engine, checkfirst=True)
        elif not dry_run:
            PromptExecution.__table__.create(bind=engine, checkfirst=True)
        if "learning_proposals" not in inspect(conn).get_table_names():
            actions.append("CREATE TABLE learning_proposals (...)")
            if not dry_run:
                LearningProposal.__table__.create(bind=engine, checkfirst=True)
        elif not dry_run:
            LearningProposal.__table__.create(bind=engine, checkfirst=True)
    actions.extend(
        ensure_columns(
            engine,
            "emails",
            {
                "message_id": "VARCHAR(255)",
                "imap_mailbox": "VARCHAR(255)",
                "imap_uidvalidity": "VARCHAR(120)",
                "imap_uid": "VARCHAR(120)",
            },
            dry_run=dry_run,
        )
    )
    actions.extend(
        ensure_columns(
            engine,
            "inbound_messages",
            {
                "source_message_id": "VARCHAR(255)",
                "source_mailbox": "VARCHAR(255)",
                "source_uidvalidity": "VARCHAR(120)",
                "source_uid": "VARCHAR(120)",
            },
            dry_run=dry_run,
        )
    )
    return actions


def _apply_tenant_product_embeddings(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.db.models import ProductEmbedding

    actions: list[str] = []
    with engine.connect() as conn:
        if "product_embeddings" not in inspect(conn).get_table_names():
            actions.append("CREATE TABLE product_embeddings (...)")
            if not dry_run:
                ProductEmbedding.__table__.create(bind=engine, checkfirst=True)
        elif not dry_run:
            ProductEmbedding.__table__.create(bind=engine, checkfirst=True)
    actions.extend(
        ensure_unique_index(
            engine,
            "product_embeddings",
            "uq_product_embeddings_product_model_version",
            ("company_id", "product_id", "embedding_model", "embedding_version"),
            dry_run=dry_run,
        )
    )
    return actions


def _apply_tenant_knowledge_entries(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.db.models import KnowledgeEntry

    actions: list[str] = []
    with engine.connect() as conn:
        if "knowledge_entries" not in inspect(conn).get_table_names():
            actions.append("CREATE TABLE knowledge_entries (...)")
            if not dry_run:
                KnowledgeEntry.__table__.create(bind=engine, checkfirst=True)
        elif not dry_run:
            KnowledgeEntry.__table__.create(bind=engine, checkfirst=True)
    actions.extend(
        ensure_unique_index(
            engine,
            "knowledge_entries",
            "uq_knowledge_entries_source",
            ("company_id", "source_type", "source_id"),
            dry_run=dry_run,
        )
    )
    return actions


def _apply_tenant_order_archiving(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    return ensure_columns(engine, "orders", {"archived": "BOOLEAN DEFAULT false"}, dry_run=dry_run)


def _apply_tenant_email_favorites(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    return ensure_columns(engine, "emails", {"is_favorite": "BOOLEAN DEFAULT false"}, dry_run=dry_run)


def _apply_tenant_auto_routing(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    return ensure_columns(
        engine,
        "llm_settings",
        {"auto_routing_enabled": "BOOLEAN DEFAULT false"},
        dry_run=dry_run,
    )


def _apply_tenant_routing_actions(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.db.models import RoutingAction

    actions: list[str] = []
    with engine.connect() as conn:
        if "routing_actions" not in inspect(conn).get_table_names():
            actions.append("CREATE TABLE routing_actions (...)")
            if not dry_run:
                RoutingAction.__table__.create(bind=engine, checkfirst=True)
        elif not dry_run:
            RoutingAction.__table__.create(bind=engine, checkfirst=True)
    return actions


def _apply_tenant_auto_forwarding(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    actions = ensure_columns(
        engine,
        "llm_settings",
        {"auto_forwarding_enabled": "BOOLEAN DEFAULT false"},
        dry_run=dry_run,
    )
    actions.extend(
        ensure_columns(
            engine,
            "routing_actions",
            {"source": "VARCHAR(30) DEFAULT 'manual'"},
            dry_run=dry_run,
        )
    )
    return actions


def _apply_tenant_pre_pilot_controls(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    return ensure_columns(
        engine,
        "llm_settings",
        {
            "simulation_mode": "BOOLEAN DEFAULT false",
            "routing_review_threshold": "FLOAT DEFAULT 0.70",
            "routing_auto_threshold": "FLOAT DEFAULT 0.90",
        },
        dry_run=dry_run,
    )


def _apply_tenant_routing_evaluations(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.db.models import (
        RoutingEvaluationCase,
        RoutingEvaluationResult,
        RoutingEvaluationRun,
        RoutingEvaluationSet,
    )

    actions: list[str] = []
    tables = (
        ("routing_evaluation_sets", RoutingEvaluationSet),
        ("routing_evaluation_cases", RoutingEvaluationCase),
        ("routing_evaluation_runs", RoutingEvaluationRun),
        ("routing_evaluation_results", RoutingEvaluationResult),
    )
    with engine.connect() as conn:
        existing = set(inspect(conn).get_table_names())
    for table_name, model in tables:
        if table_name not in existing:
            actions.append(f"CREATE TABLE {table_name} (...)")
        if not dry_run:
            model.__table__.create(bind=engine, checkfirst=True)
    return actions


def _apply_tenant_worker_heartbeats(engine, dry_run: bool) -> list[str]:  # noqa: ANN001
    from app.db.models import WorkerHeartbeat

    if dry_run:
        return ["CREATE TABLE worker_heartbeats (...)" ]
    WorkerHeartbeat.__table__.create(bind=engine, checkfirst=True)
    return ["CREATE TABLE worker_heartbeats"]


TENANT_SCHEMA_MIGRATIONS = [
    MigrationSpec(
        version="2026.07.15.1",
        name="tenant schema ledger",
        checksum=checksum_text("tenant", "schema_ledger", *TENANT_MIGRATION_COLUMNS.keys()),
        upgrade=_apply_tenant_metadata,
    ),
    MigrationSpec(
        version="2026.07.15.2",
        name="tenant operational compatibility",
        checksum=checksum_text("tenant", "operational_compatibility", *TENANT_COMPAT_COLUMNS.keys()),
        upgrade=_apply_tenant_compatibility,
    ),
    MigrationSpec(
        version="2026.07.15.3",
        name="tenant job reliability",
        checksum=checksum_text("tenant", "job_reliability", "background_jobs", "job_attempts"),
        upgrade=_apply_tenant_job_reliability,
    ),
    MigrationSpec(
        version="2026.07.15.4",
        name="tenant messages and conversations",
        checksum=checksum_text("tenant", "messages_and_conversations", "conversations", "inbound_messages", "emails", "orders"),
        upgrade=_apply_tenant_messages,
    ),
    MigrationSpec(
        version="2026.07.16.1",
        name="tenant ai execution and learning control",
        checksum=checksum_text("tenant", "ai_execution_and_learning_control", "prompt_executions", "learning_proposals", "emails", "inbound_messages"),
        upgrade=_apply_tenant_ai_learning,
    ),
    MigrationSpec(
        version="2026.08.19.2",
        name="tenant product semantic embeddings",
        checksum=checksum_text("tenant", "product_semantic_embeddings", "product_embeddings"),
        upgrade=_apply_tenant_product_embeddings,
    ),
    MigrationSpec(
        version="2026.08.19.3",
        name="tenant business knowledge entries",
        checksum=checksum_text("tenant", "business_knowledge_entries", "knowledge_entries"),
        upgrade=_apply_tenant_knowledge_entries,
    ),
    MigrationSpec(
        version="2026.08.28.1",
        name="tenant order archiving",
        checksum=checksum_text("tenant", "order_archiving", "orders", "archived"),
        upgrade=_apply_tenant_order_archiving,
    ),
    MigrationSpec(
        version="2026.09.01.1",
        name="tenant email favorites",
        checksum=checksum_text("tenant", "email_favorites", "emails", "is_favorite"),
        upgrade=_apply_tenant_email_favorites,
    ),
    MigrationSpec(
        version="2026.09.03.1",
        name="tenant multi-mailbox foundation",
        checksum=checksum_text("tenant", "multi_mailbox_foundation", "mailboxes", "emails.mailbox_id", "inbound_messages.mailbox_id"),
        upgrade=_apply_tenant_mailboxes,
    ),
    MigrationSpec(
        version="2026.09.03.2",
        name="communication domain foundation",
        checksum=checksum_text("tenant", "communication_domain_foundation", "communications", "communication_attachments", "emails.communication_id"),
        upgrade=_apply_tenant_communications,
    ),
    MigrationSpec(
        version="2026.09.03.3",
        name="departments knowledge members raci",
        checksum=checksum_text(
            "tenant",
            "departments_knowledge_members_raci",
            "departments",
            "department_knowledge",
            "department_members",
            "raci_assignments",
            "department_members.company_id",
            "raci_assignments.null_user_unique",
        ),
        upgrade=_apply_tenant_departments,
    ),
    MigrationSpec(
        version="2026.09.03.4",
        name="communication routing decisions and corrections",
        checksum=checksum_text(
            "tenant",
            "communication_routing_decisions_and_corrections",
            "routing_decisions",
            "routing_corrections",
            "communication_history",
        ),
        upgrade=_apply_tenant_routing_decisions,
    ),
    MigrationSpec(
        version="2026.09.04.1",
        name="tenant automatic communication routing",
        checksum=checksum_text("tenant", "automatic_communication_routing", "llm_settings.auto_routing_enabled"),
        upgrade=_apply_tenant_auto_routing,
    ),
    MigrationSpec(
        version="2026.09.04.2",
        name="tenant routing actions",
        checksum=checksum_text("tenant", "routing_actions", "idempotency", "tenant_foreign_keys"),
        upgrade=_apply_tenant_routing_actions,
    ),
    MigrationSpec(
        version="2026.09.04.3",
        name="tenant automatic forwarding",
        checksum=checksum_text(
            "tenant",
            "automatic_forwarding",
            "llm_settings.auto_forwarding_enabled",
            "routing_actions.source",
        ),
        upgrade=_apply_tenant_auto_forwarding,
    ),
    MigrationSpec(
        version="2026.09.08.1",
        name="tenant routing evaluation lab",
        checksum=checksum_text(
            "tenant",
            "routing_evaluation_lab",
            "routing_evaluation_sets",
            "routing_evaluation_cases",
            "routing_evaluation_runs",
            "routing_evaluation_results",
        ),
        upgrade=_apply_tenant_routing_evaluations,
    ),
    MigrationSpec(
        version="2026.09.08.2",
        name="tenant routing policy and simulation controls",
        checksum=checksum_text(
            "tenant",
            "routing_policy",
            "llm_settings.simulation_mode",
            "llm_settings.routing_review_threshold",
            "llm_settings.routing_auto_threshold",
        ),
        upgrade=_apply_tenant_pre_pilot_controls,
    ),
    MigrationSpec(
        version="2026.09.08.3",
        name="tenant worker heartbeats",
        checksum=checksum_text("tenant", "worker_heartbeats", "worker_kind", "last_heartbeat_at"),
        upgrade=_apply_tenant_worker_heartbeats,
    ),
]

# KIBAK starts from the clean baseline and only advances through KIBAK migrations.
KIBAK_TENANT_SCHEMA_MIGRATIONS = [
    spec for spec in TENANT_SCHEMA_MIGRATIONS if spec.version in {"2026.09.08.1", "2026.09.08.2", "2026.09.08.3"}
]

MASTER_SCHEMA_MIGRATIONS = [
    MigrationSpec(
        version="2026.07.15.1",
        name="master schema ledger",
        checksum=checksum_text("master", "schema_ledger"),
        upgrade=_apply_master_metadata,
    ),
    MigrationSpec(
        version="2026.07.16.1",
        name="master email sync checkpoints",
        checksum=checksum_text("master", "email_sync_checkpoints", "email_sync_state"),
        upgrade=_apply_master_email_sync_state,
    ),
    MigrationSpec(
        version="2026.08.19.1",
        name="master email listener state",
        checksum=checksum_text("master", "email_listener_state", "email_sync_state"),
        upgrade=_apply_master_email_listener_state,
    ),
    MigrationSpec(
        version="2026.08.19.2",
        name="master email sync state repair",
        checksum=checksum_text("master", "email_sync_state_repair", "email_sync_state", *MASTER_EMAIL_SYNC_STATE_COLUMNS.keys()),
        upgrade=_apply_master_email_sync_state_repair,
    ),
    MigrationSpec(
        version="2026.09.03.1",
        name="master mailbox sync state",
        checksum=checksum_text("master", "mailbox_sync_state", *MAILBOX_SYNC_STATE_COLUMNS.keys()),
        upgrade=_apply_master_mailbox_sync_state,
    ),
]

CURRENT_TENANT_SCHEMA_VERSION = TENANT_SCHEMA_MIGRATIONS[-1].version
CURRENT_TENANT_SCHEMA_NAME = TENANT_SCHEMA_MIGRATIONS[-1].name
CURRENT_TENANT_SCHEMA_CHECKSUM = registry_checksum(TENANT_SCHEMA_MIGRATIONS)
CURRENT_KIBAK_TENANT_SCHEMA_VERSION = KIBAK_TENANT_SCHEMA_MIGRATIONS[-1].version
CURRENT_KIBAK_TENANT_SCHEMA_NAME = KIBAK_TENANT_SCHEMA_MIGRATIONS[-1].name
CURRENT_KIBAK_TENANT_SCHEMA_CHECKSUM = registry_checksum(KIBAK_TENANT_SCHEMA_MIGRATIONS)
SUPPORTED_TENANT_LEGACY_VERSIONS = {"2026.07.10.1", "kibak.tenant.1"}

CURRENT_MASTER_SCHEMA_VERSION = MASTER_SCHEMA_MIGRATIONS[-1].version
CURRENT_MASTER_SCHEMA_NAME = MASTER_SCHEMA_MIGRATIONS[-1].name
CURRENT_MASTER_SCHEMA_CHECKSUM = registry_checksum(MASTER_SCHEMA_MIGRATIONS)
SUPPORTED_MASTER_LEGACY_VERSIONS: set[str] = set()
