from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.database import Base
from app.core.config import get_settings
from app.db.models import TenantSchemaMigration
from app.migrations.helpers import ensure_columns, existing_columns, table_exists
from app.migrations.registry import (
    CURRENT_TENANT_SCHEMA_CHECKSUM,
    CURRENT_TENANT_SCHEMA_NAME,
    CURRENT_TENANT_SCHEMA_VERSION,
    CURRENT_KIBAK_TENANT_SCHEMA_CHECKSUM,
    CURRENT_KIBAK_TENANT_SCHEMA_NAME,
    CURRENT_KIBAK_TENANT_SCHEMA_VERSION,
    KIBAK_TENANT_SCHEMA_MIGRATIONS,
    SUPPORTED_TENANT_LEGACY_VERSIONS,
    TENANT_MIGRATION_COLUMNS,
    TENANT_COMPAT_COLUMNS,
    TENANT_SCHEMA_MIGRATIONS,
)
from app.migrations.runner import migration_summary, run_migration_plan


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tenant_migration_config(db: Session) -> tuple[list, str, str, str, set[str]]:
    kibak_postgres = get_settings().app_slug.strip().lower() == "kibak" and not db.get_bind().url.drivername.startswith("sqlite")
    if kibak_postgres:
        return (
            KIBAK_TENANT_SCHEMA_MIGRATIONS,
            CURRENT_KIBAK_TENANT_SCHEMA_VERSION,
            CURRENT_KIBAK_TENANT_SCHEMA_NAME,
            CURRENT_KIBAK_TENANT_SCHEMA_CHECKSUM,
            SUPPORTED_TENANT_LEGACY_VERSIONS | {"kibak.tenant.1"},
        )
    return (
        TENANT_SCHEMA_MIGRATIONS,
        CURRENT_TENANT_SCHEMA_VERSION,
        CURRENT_TENANT_SCHEMA_NAME,
        CURRENT_TENANT_SCHEMA_CHECKSUM,
        SUPPORTED_TENANT_LEGACY_VERSIONS,
    )


def _latest_state(db: Session, company_id: int | None) -> TenantSchemaMigration | None:
    if company_id is not None:
        state = db.scalar(
            select(TenantSchemaMigration)
            .where(TenantSchemaMigration.company_id == company_id)
            .order_by(TenantSchemaMigration.applied_at.desc().nullslast(), TenantSchemaMigration.id.desc())
        )
        if state:
            return state
    return db.scalar(select(TenantSchemaMigration).order_by(TenantSchemaMigration.applied_at.desc().nullslast(), TenantSchemaMigration.id.desc()))


def ensure_tenant_migration_record(
    db: Session,
    company_id: int | None,
    *,
    notes: str | None = None,
    application_version: str | None = None,
) -> TenantSchemaMigration:
    _, current_version, current_name, current_checksum, _ = _tenant_migration_config(db)
    state = _latest_state(db, company_id)
    now = _now()
    if not state:
        state = TenantSchemaMigration()
        db.add(state)
    if company_id is not None:
        state.company_id = company_id
    state.version = current_version
    state.name = current_name
    state.checksum = current_checksum
    state.execution_ms = 0
    state.application_version = application_version
    state.status = "current"
    state.applied_at = state.applied_at or now
    state.last_checked_at = now
    state.last_error = None
    if notes:
        state.notes = notes
    state.updated_at = now
    db.commit()
    return state


def record_tenant_migration_failure(db: Session, company_id: int | None, error_message: str) -> TenantSchemaMigration:
    state = _latest_state(db, company_id)
    now = _now()
    if not state:
        state = TenantSchemaMigration()
        db.add(state)
    if company_id is not None:
        state.company_id = company_id
    state.version = state.version or "0"
    state.name = state.name or "schema failure"
    state.status = "failed"
    state.last_error = error_message
    state.last_checked_at = now
    state.updated_at = now
    db.commit()
    return state


def upgrade_tenant_schema(
    engine,
    *,
    company_id: int | None = None,
    application_version: str | None = None,
    dry_run: bool = False,
    baseline: bool = False,
) -> dict:
    kibak_postgres = get_settings().app_slug.strip().lower() == "kibak" and not engine.url.drivername.startswith("sqlite")
    if not dry_run:
        if kibak_postgres:
            from app.migrations.kibak_baseline import KIBAK_TENANT_TABLES

            Base.metadata.create_all(
                bind=engine,
                tables=[Base.metadata.tables[name] for name in KIBAK_TENANT_TABLES],
            )
        else:
            Base.metadata.create_all(bind=engine)
        ensure_columns(engine, "schema_migrations", TENANT_MIGRATION_COLUMNS, dry_run=False)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        specs, current_version, current_name, current_checksum, allowed_legacy_versions = _tenant_migration_config(db)
        summary = run_migration_plan(
            engine,
            db,
            TenantSchemaMigration,
            specs,
            application_version=application_version,
            company_id=company_id,
            allowed_legacy_versions=allowed_legacy_versions,
            baseline=baseline,
            dry_run=dry_run,
        )
        repair_actions: list[str] = []
        if not dry_run:
            for table_name, columns in TENANT_COMPAT_COLUMNS.items():
                repair_actions.extend(ensure_columns(engine, table_name, columns, dry_run=False))
        elif dry_run:
            for table_name, columns in TENANT_COMPAT_COLUMNS.items():
                repair_actions.extend(ensure_columns(engine, table_name, columns, dry_run=True))
        if repair_actions:
            summary.setdefault("planned_actions", []).extend(repair_actions)
            summary["repair_actions"] = repair_actions
        if company_id is not None and not dry_run and table_exists(db.get_bind(), "schema_migrations"):
            state = db.scalar(
                select(TenantSchemaMigration)
                .where(TenantSchemaMigration.company_id == company_id)
                .order_by(TenantSchemaMigration.applied_at.desc().nullslast(), TenantSchemaMigration.id.desc())
            )
            if state:
                summary.update(
                    migration_summary(
                        state,
                        current_version=current_version,
                        current_name=current_name,
                        current_checksum=current_checksum,
                    )
                )
        return summary
    finally:
        db.close()


def tenant_migration_report(db: Session, company_id: int | None, *, persist: bool = False) -> dict:
    _, current_version, current_name, current_checksum, _ = _tenant_migration_config(db)
    if not table_exists(db.get_bind(), "schema_migrations"):
        return {
            "version": None,
            "name": None,
            "checksum": None,
            "execution_ms": None,
            "application_version": None,
            "current_version": current_version,
            "current_name": current_name,
            "current_checksum": current_checksum,
            "status": "missing",
            "last_checked_at": None,
            "applied_at": None,
            "last_error": None,
            "notes": None,
            "is_current": False,
        }
    required_columns = {"version", "name", "checksum", "execution_ms", "application_version", "status", "applied_at", "last_checked_at", "last_error", "notes"}
    if not required_columns.issubset(existing_columns(db.get_bind(), "schema_migrations")):
        return {
            "version": None,
            "name": None,
            "checksum": None,
            "execution_ms": None,
            "application_version": None,
            "current_version": current_version,
            "current_name": current_name,
            "current_checksum": current_checksum,
            "status": "incomplete",
            "last_checked_at": None,
            "applied_at": None,
            "last_error": None,
            "notes": None,
            "is_current": False,
        }
    state = _latest_state(db, company_id)
    now = _now()
    if not state:
        return {
            "version": None,
            "name": None,
            "checksum": None,
            "execution_ms": None,
            "application_version": None,
            "current_version": current_version,
            "current_name": current_name,
            "current_checksum": current_checksum,
            "status": "missing",
            "last_checked_at": None,
            "applied_at": None,
            "last_error": None,
            "notes": None,
            "is_current": False,
        }
    if persist:
        state.last_checked_at = now
        state.updated_at = now
        if state.status != "failed":
            state.status = "current" if state.version == current_version and state.checksum == current_checksum else "outdated"
        db.commit()
    expected = current_version
    return {
        "version": state.version,
        "name": state.name,
        "checksum": state.checksum,
        "execution_ms": state.execution_ms,
        "application_version": state.application_version,
        "current_version": expected,
        "current_name": current_name,
        "current_checksum": current_checksum,
        "status": state.status,
        "last_checked_at": state.last_checked_at,
        "applied_at": state.applied_at,
        "last_error": state.last_error,
        "notes": state.notes,
        "is_current": state.version == expected and state.checksum == current_checksum and state.status == "current",
    }


def ensure_tenant_schema(database_url: str, *, company_id: int | None = None, application_version: str | None = None) -> dict:
    from app.tenancy.database import get_tenant_engine

    engine = get_tenant_engine(database_url)
    return upgrade_tenant_schema(engine, company_id=company_id, application_version=application_version)
