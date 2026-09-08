from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.master.database import MasterBase
from app.master.models import MasterSchemaMigration
from app.migrations.helpers import ensure_columns, existing_columns, table_exists
from app.migrations.registry import (
    CURRENT_MASTER_SCHEMA_CHECKSUM,
    CURRENT_MASTER_SCHEMA_NAME,
    CURRENT_MASTER_SCHEMA_VERSION,
    MASTER_SCHEMA_MIGRATIONS,
)
from app.migrations.kibak_baseline import KIBAK_MASTER_BASELINE_VERSION
from app.migrations.runner import migration_summary, run_migration_plan


MASTER_MIGRATION_COLUMNS = {
    "name": "VARCHAR(180) DEFAULT 'unregistered'",
    "checksum": "VARCHAR(120)",
    "execution_ms": "INTEGER DEFAULT 0",
    "application_version": "VARCHAR(80)",
}


def _master_contract(state: MasterSchemaMigration | None) -> tuple[str, str, str]:
    if state and state.version == KIBAK_MASTER_BASELINE_VERSION:
        return KIBAK_MASTER_BASELINE_VERSION, "KIBAK master baseline", KIBAK_MASTER_BASELINE_VERSION
    return CURRENT_MASTER_SCHEMA_VERSION, CURRENT_MASTER_SCHEMA_NAME, CURRENT_MASTER_SCHEMA_CHECKSUM


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _latest_state(db: Session) -> MasterSchemaMigration | None:
    return db.scalar(select(MasterSchemaMigration).order_by(MasterSchemaMigration.applied_at.desc().nullslast(), MasterSchemaMigration.id.desc()))


def ensure_master_migration_record(
    db: Session,
    *,
    notes: str | None = None,
    application_version: str | None = None,
) -> MasterSchemaMigration:
    state = _latest_state(db)
    now = _now()
    if not state:
        state = MasterSchemaMigration()
        db.add(state)
    state.version = CURRENT_MASTER_SCHEMA_VERSION
    state.name = CURRENT_MASTER_SCHEMA_NAME
    state.checksum = CURRENT_MASTER_SCHEMA_CHECKSUM
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


def record_master_migration_failure(db: Session, error_message: str) -> MasterSchemaMigration:
    state = _latest_state(db)
    now = _now()
    if not state:
        state = MasterSchemaMigration()
        db.add(state)
    state.version = state.version or "0"
    state.name = state.name or "schema failure"
    state.status = "failed"
    state.last_error = error_message
    state.last_checked_at = now
    state.updated_at = now
    db.commit()
    return state


def upgrade_master_schema(
    engine,
    *,
    application_version: str | None = None,
    dry_run: bool = False,
    baseline: bool = False,
) -> dict:
    if not dry_run:
        MasterBase.metadata.create_all(bind=engine)
        ensure_columns(engine, "schema_migrations", MASTER_MIGRATION_COLUMNS, dry_run=False)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        summary = run_migration_plan(
            engine,
            db,
            MasterSchemaMigration,
            MASTER_SCHEMA_MIGRATIONS,
            application_version=application_version,
            baseline=baseline,
            dry_run=dry_run,
        )
        state = _latest_state(db) if not dry_run and table_exists(db.get_bind(), "schema_migrations") else None
        if state:
            summary.update(
                migration_summary(
                    state,
                    current_version=CURRENT_MASTER_SCHEMA_VERSION,
                    current_name=CURRENT_MASTER_SCHEMA_NAME,
                    current_checksum=CURRENT_MASTER_SCHEMA_CHECKSUM,
                )
            )
        return summary
    finally:
        db.close()


def master_migration_report(db: Session, *, persist: bool = False) -> dict:
    schema_table_exists = table_exists(db.get_bind(), "schema_migrations")
    state = _latest_state(db) if schema_table_exists else None
    current_version, current_name, current_checksum = _master_contract(state)
    if not schema_table_exists:
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
    state = state or _latest_state(db)
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
    return {
        "version": state.version,
        "name": state.name,
        "checksum": state.checksum,
        "execution_ms": state.execution_ms,
        "application_version": state.application_version,
        "current_version": current_version,
        "current_name": current_name,
        "current_checksum": current_checksum,
        "status": state.status,
        "last_checked_at": state.last_checked_at,
        "applied_at": state.applied_at,
        "last_error": state.last_error,
        "notes": state.notes,
        "is_current": state.version == current_version and state.checksum == current_checksum and state.status == "current",
    }
