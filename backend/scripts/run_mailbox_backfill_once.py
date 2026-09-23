"""Run the single, bounded-by-date KIBAK mailbox backfill directly.

This is deliberately an operational script rather than an HTTP endpoint or a
worker job.  It has a production confirmation guard and calls the canonical
backfill service with processing disabled and no artificial message cap.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from time import monotonic

from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.attachment_storage import TenantStorageError, read_attachment  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.models import (  # noqa: E402
    BackgroundJob,
    Communication,
    CommunicationAttachment,
    Company,
    LLMSettings,
    Mailbox,
    PromptExecution,
    RoutingAction,
    RoutingDecision,
)
from app.mailboxes.service import get_or_create_mailbox_sync_state  # noqa: E402
from app.master.database import MasterSessionLocal  # noqa: E402
from app.master.models import MasterCompany, MasterTenantDatabase  # noqa: E402
from app.routing.policy import load_routing_policy  # noqa: E402
from app.settings.integrations import backfill_imap_emails  # noqa: E402
from app.tenancy.database import tenant_db_session  # noqa: E402
from app.workers.email_worker import _acquire_lock, _release_lock  # noqa: E402, PLC2701


REQUIRED_SINCE = date(2026, 9, 14)
DEFAULT_BATCH_SIZE = 25
EXPECTED_TENANT_DATABASE = "kibak_tenant_quibac"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ejecuta una única importación histórica de KIBAK.")
    parser.add_argument("--company-slug", required=True)
    parser.add_argument("--since", required=True, help="Fecha mínima inclusiva: AAAA-MM-DD")
    parser.add_argument("--to", help="Fecha máxima inclusiva: AAAA-MM-DD")
    parser.add_argument(
        "--confirm-production-backfill",
        action="store_true",
        help="Confirma explícitamente que se permite el backfill real de Production.",
    )
    return parser


def _today_madrid() -> str:
    return datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()


def _safe_database_name(database_url: str | None) -> str | None:
    if not database_url:
        return None
    parsed = urlsplit(database_url)
    name = parsed.path.rsplit("/", 1)[-1]
    return name or None


def _count(db, model, company_id: int) -> int:  # noqa: ANN001
    return int(db.scalar(select(func.count()).select_from(model).where(model.company_id == company_id)) or 0)


def _counts(db, company_id: int) -> dict[str, int]:  # noqa: ANN001
    return {
        "communications": _count(db, Communication, company_id),
        "attachments": _count(db, CommunicationAttachment, company_id),
        "prompt_executions": _count(db, PromptExecution, company_id),
        "routing_decisions": _count(db, RoutingDecision, company_id),
        "routing_actions": _count(db, RoutingAction, company_id),
        "background_jobs": _count(db, BackgroundJob, company_id),
    }


def _deltas(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after.get(key, 0) - before.get(key, 0) for key in before}


def _received_range(db, company_id: int, mailbox_id: int, since: date, to: date) -> dict[str, str | None]:  # noqa: ANN001
    start = datetime.combine(since, time.min, tzinfo=timezone.utc)
    end = datetime.combine(to + timedelta(days=1), time.min, tzinfo=timezone.utc)
    first, last = db.execute(
        select(func.min(Communication.received_at), func.max(Communication.received_at)).where(
            Communication.company_id == company_id,
            Communication.mailbox_id == mailbox_id,
            Communication.received_at >= start,
            Communication.received_at < end,
        )
    ).one()
    return {
        "first_received_at": first.isoformat() if first else None,
        "last_received_at": last.isoformat() if last else None,
    }


def _validate_runtime(settings) -> None:  # noqa: ANN001
    if str(getattr(settings, "app_slug", "")).strip().lower() != "kibak":
        raise RuntimeError("El runtime no es KIBAK.")
    if str(getattr(settings, "environment", "")).strip().lower() != "production":
        raise RuntimeError("El backfill real solo puede ejecutarse en Production.")
    if str(getattr(settings, "storage_backend", "")).strip().lower() != "s3":
        raise RuntimeError("El backfill requiere storage S3 persistente.")
    for name in ("s3_bucket", "s3_endpoint_url", "s3_access_key_id", "s3_secret_access_key"):
        if not getattr(settings, name, None):
            raise RuntimeError(f"Falta configuración persistente de storage: {name}.")


def validate_mailbox_safety(mailbox: Mailbox) -> None:
    """Fail closed; the caller serializes active sync with a mailbox lease."""
    if (mailbox.provider or "").strip().lower() != "microsoft365":
        raise RuntimeError("El buzón piloto no es Microsoft 365.")
    if (mailbox.connection_method or "").strip().lower() != "oauth2":
        raise RuntimeError("El buzón piloto no usa OAuth2.")
    if not mailbox.refresh_token_encrypted:
        raise RuntimeError("El buzón piloto no tiene credencial OAuth almacenada.")
    if mailbox.mark_as_read_after_import:
        raise RuntimeError("mark_as_read_after_import debe estar desactivado.")
    if mailbox.smtp_enabled:
        raise RuntimeError("SMTP debe permanecer desactivado.")
    if mailbox.move_after_processing:
        raise RuntimeError("move_after_processing debe estar desactivado.")
    if mailbox.auto_process_on_fetch:
        raise RuntimeError("auto_process_on_fetch debe estar desactivado.")


def _storage_reference_is_tenant_scoped(storage_ref: str | None, tenant_id: int, prefix: str) -> bool:
    if not storage_ref or not storage_ref.startswith("s3://"):
        return False
    key = storage_ref[5:].partition("/")[2]
    expected = f"{prefix.strip('/')}/tenants/{int(tenant_id)}/"
    return bool(key and key.startswith(expected))


def _storage_audit(db, settings, company_id: int) -> dict[str, bool | int]:  # noqa: ANN001
    attachment_count = _count(db, CommunicationAttachment, company_id)
    attachment = db.scalar(
        select(CommunicationAttachment)
        .where(CommunicationAttachment.company_id == company_id)
        .order_by(CommunicationAttachment.id.asc())
        .limit(1)
    )
    if attachment is None:
        return {
            "db_attachment_present": False,
            "persistent_storage_ref": False,
            "object_read_ok": False,
            "tenant_isolation_ok": False,
            "attachments": attachment_count,
        }
    prefix = str(getattr(settings, "s3_prefix", "kibak") or "kibak")
    scoped = _storage_reference_is_tenant_scoped(attachment.storage_ref, company_id, prefix)
    read_ok = False
    isolation_ok = False
    if scoped:
        try:
            read_ok = bool(read_attachment(attachment.storage_ref or "", tenant_id=company_id))
        except Exception:  # noqa: BLE001
            read_ok = False
        try:
            read_attachment(attachment.storage_ref or "", tenant_id=company_id + 1)
        except TenantStorageError:
            isolation_ok = True
        except Exception:  # noqa: BLE001
            isolation_ok = False
    return {
        "db_attachment_present": True,
        "persistent_storage_ref": scoped,
        "object_read_ok": read_ok,
        "tenant_isolation_ok": isolation_ok,
        "attachments": attachment_count,
    }


def _execute_canonical_backfill(
    db,
    settings,
    company_id: int,
    *,
    since: str,
    to: str,
    sync_state,
    master_db,
    mailbox_id: int,
) -> dict:
    """Invoke the existing service once; it hard-codes auto_process=False."""
    return backfill_imap_emails(
        db,
        settings,
        company_id,
        from_date=since,
        to_date=to,
        limit=None,
        batch_size=DEFAULT_BATCH_SIZE,
        resume=False,
        stop_after_batch=False,
        sync_state=sync_state,
        sync_session=master_db,
        mailbox_id=mailbox_id,
        unbounded=True,
        preserve_normal_cursor=True,
    )


def _safe_backfill_result(result: dict) -> dict[str, object]:
    safe_keys = (
        "ok",
        "found",
        "downloaded",
        "saved",
        "duplicates",
        "discarded",
        "attachments",
        "routing_jobs_enqueued",
        "routing_job_errors",
        "errors",
        "batch_count",
        "has_more",
        "last_seen_uid_before",
        "last_seen_uid_after",
    )
    return {key: result.get(key) for key in safe_keys if key in result}


def _parse_backfill_range(args: argparse.Namespace) -> tuple[date, date]:
    if args.company_slug != "kibak-pilot":
        raise RuntimeError("El runner solo admite el tenant piloto autorizado.")
    since = date.fromisoformat(args.since)
    if since != REQUIRED_SINCE:
        raise RuntimeError("La fecha mínima obligatoria es 2026-09-14.")
    to = date.fromisoformat(args.to or _today_madrid())
    if to < since:
        raise RuntimeError("La fecha final no puede ser anterior a la fecha mínima.")
    return since, to


def run_backfill_in_context(
    args: argparse.Namespace,
    *,
    settings,
    master_db,
    tenant_db,
    company,
    mailbox,
    database_name: str | None,
) -> dict[str, object]:
    """Run the one-shot operation using an already authenticated app context."""
    _validate_runtime(settings)
    since, to = _parse_backfill_range(args)
    if database_name != EXPECTED_TENANT_DATABASE:
        raise RuntimeError("El contexto tenant no apunta a la base autorizada.")
    validate_mailbox_safety(mailbox)

    llm = tenant_db.scalar(select(LLMSettings).where(LLMSettings.company_id == company.id))
    policy = load_routing_policy(tenant_db, company.id)
    if llm is None:
        raise RuntimeError("No existe LLMSettings para el tenant piloto.")
    if not policy.simulation_mode or policy.auto_forwarding_enabled:
        raise RuntimeError("La policy efectiva no cumple simulation=true y forwarding=false.")

    sync_state = get_or_create_mailbox_sync_state(master_db, mailbox, commit=True)
    if not _acquire_lock(master_db, sync_state, owner="admin-backfill"):
        raise RuntimeError("Ya existe una sincronización o backfill en curso.")
    normal_cursor_before = sync_state.last_seen_uid
    restore_sync_enabled = bool(mailbox.enabled and mailbox.auto_sync_enabled)
    sync_state.enabled = False
    master_db.commit()
    counts_before = _counts(tenant_db, company.id)
    started = monotonic()
    result = None
    operation_ok = False
    try:
        result = _execute_canonical_backfill(
            tenant_db,
            mailbox,
            company.id,
            since=since.isoformat(),
            to=to.isoformat(),
            sync_state=sync_state,
            master_db=master_db,
            mailbox_id=mailbox.id,
        )
        operation_ok = bool(result.get("ok"))
        counts_after = _counts(tenant_db, company.id)
        master_db.refresh(sync_state)
        storage = _storage_audit(tenant_db, settings, company.id)
    finally:
        sync_state.enabled = restore_sync_enabled
        _release_lock(master_db, sync_state, success=operation_ok, error=None if operation_ok else "Backfill administrativo detenido")
        master_db.refresh(sync_state)

    if result is None:
        raise RuntimeError("El backfill administrativo no devolvió resultado.")

    return {
        "ok": bool(result.get("ok")),
        "tenant": {"slug": args.company_slug, "company_id": company.id, "database": EXPECTED_TENANT_DATABASE},
        "range": {"since": since.isoformat(), "to": to.isoformat()},
        "mailbox": {
            "id": mailbox.id,
            "provider": mailbox.provider,
            "connection_method": mailbox.connection_method,
            "enabled": bool(mailbox.enabled),
            "auto_sync_enabled": bool(mailbox.auto_sync_enabled),
            "mark_as_read_after_import": bool(mailbox.mark_as_read_after_import),
            "smtp_enabled": bool(mailbox.smtp_enabled),
            "imap_readonly": not bool(mailbox.mark_as_read_after_import),
        },
        "policy": {
            "simulation_mode": bool(policy.simulation_mode),
            "auto_forwarding_enabled": bool(policy.auto_forwarding_enabled),
        },
        "backfill": _safe_backfill_result(result),
        "counts_before": counts_before,
        "counts_after": counts_after,
        "count_deltas": _deltas(counts_before, counts_after),
        "received_range": _received_range(tenant_db, company.id, mailbox.id, since, to),
        "sync_state": {
            "enabled": bool(sync_state.enabled),
            "normal_cursor_unchanged": sync_state.last_seen_uid == normal_cursor_before,
            "backfill_status": sync_state.backfill_status,
        },
        "latency_ms": round((monotonic() - started) * 1000, 1),
        "storage": storage,
        "execution": {
            "canonical_backfill_invocations": 1,
            "background_worker_started": False,
            "openai_called": False,
            "smtp_called": False,
            "forwarding_called": False,
        },
    }


def run_backfill_once(args: argparse.Namespace) -> dict[str, object]:
    settings = get_settings()
    _validate_runtime(settings)
    _parse_backfill_range(args)

    master_db = MasterSessionLocal()
    tenant_db = None
    try:
        company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == args.company_slug, MasterCompany.active.is_(True)))
        if company is None:
            raise RuntimeError("No se encontró el tenant piloto en master.")
        bindings = list(
            master_db.scalars(
                select(MasterTenantDatabase).where(
                    MasterTenantDatabase.company_id == company.id,
                    MasterTenantDatabase.is_active.is_(True),
                )
            )
        )
        if len(bindings) != 1:
            raise RuntimeError("El tenant piloto no tiene un único binding activo.")
        binding = bindings[0]
        if binding.database_key != args.company_slug or binding.database_type != "postgresql":
            raise RuntimeError("El binding del tenant piloto no es el esperado.")
        database_url = binding.get_database_url()
        if _safe_database_name(database_url) != EXPECTED_TENANT_DATABASE:
            raise RuntimeError("El binding no apunta a la base tenant autorizada.")

        tenant_session = tenant_db_session(database_url)
        tenant_db = tenant_session()
        tenant_company = tenant_db.scalar(select(Company).where(Company.id == company.id))
        if tenant_company is None:
            raise RuntimeError("La base tenant no contiene el company_id esperado.")
        mailboxes = list(
            tenant_db.scalars(
                select(Mailbox).where(
                    Mailbox.company_id == company.id,
                    Mailbox.provider == "microsoft365",
                    Mailbox.connection_method == "oauth2",
                )
            )
        )
        if len(mailboxes) != 1:
            raise RuntimeError("Debe existir exactamente un buzón Microsoft OAuth para el piloto.")
        return run_backfill_in_context(
            args,
            settings=settings,
            master_db=master_db,
            tenant_db=tenant_db,
            company=tenant_company,
            mailbox=mailboxes[0],
            database_name=_safe_database_name(database_url),
        )
    finally:
        if tenant_db is not None:
            tenant_db.close()
        master_db.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_production_backfill:
        print("Falta --confirm-production-backfill; no se abrió IMAP.", file=sys.stderr)
        return 2
    try:
        result = run_backfill_once(args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "phase": "preflight_or_backfill", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
