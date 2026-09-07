"""Create the independent KIBAK schema on an explicitly empty database."""

from __future__ import annotations

import os
from collections.abc import Iterable

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import MetaData

from app.core.config import get_settings
from app.db.database import Base
from app.db.models import Company, TenantSchemaMigration  # noqa: F401
from app.master.database import MasterBase
from app.master.models import MasterSchemaMigration  # noqa: F401


KIBAK_BASELINE_CONFIRMATION = "KIBAK_EMPTY_DATABASE"
KIBAK_MASTER_BASELINE_VERSION = "kibak.master.1"
KIBAK_TENANT_BASELINE_VERSION = "kibak.tenant.1"
_PROHIBITED_DATABASE_MARKERS = ("anchi", "gemavi", "comavi")

KIBAK_MASTER_TABLES = frozenset(
    {
        "companies",
        "users",
        "memberships",
        "tenant_databases",
        "mailbox_sync_state",
        "schema_migrations",
    }
)

KIBAK_TENANT_TABLES = frozenset(
    {
        "companies",
        "roles",
        "users",
        "settings",
        "branding_settings",
        "mailboxes",
        "communications",
        "communication_attachments",
        "departments",
        "department_knowledge",
        "department_members",
        "raci_assignments",
        "routing_decisions",
        "routing_corrections",
        "routing_actions",
        "llm_settings",
        "background_jobs",
        "job_attempts",
        "audit_logs",
        "prompt_templates",
        "prompt_versions",
        "prompt_executions",
        "schema_migrations",
    }
)


class KibakBaselineError(RuntimeError):
    """Raised when a KIBAK baseline target cannot be proven safe."""


def _require_confirmation(confirmation: str | None) -> None:
    expected = confirmation or os.getenv("KIBAK_BASELINE_CONFIRMATION", "")
    if expected != KIBAK_BASELINE_CONFIRMATION:
        raise KibakBaselineError(
            "Baseline rechazado: requiere KIBAK_BASELINE_CONFIRMATION=KIBAK_EMPTY_DATABASE."
        )


def _database_marker(engine: Engine) -> str:
    url = engine.url
    return " ".join(
        str(value or "")
        for value in (url.drivername, url.host, url.database, url.query)
    ).lower()


def _assert_empty_kibak_target(engine: Engine, *, confirmation: str | None) -> None:
    _require_confirmation(confirmation)
    if get_settings().app_slug.strip().lower() != "kibak":
        raise KibakBaselineError("Baseline rechazado: APP_SLUG debe ser exactamente 'kibak'.")
    marker = _database_marker(engine)
    if any(token in marker for token in _PROHIBITED_DATABASE_MARKERS):
        raise KibakBaselineError("Baseline rechazado: la URL identifica una base ANCHI/GEMAVI.")
    existing_tables = set(inspect(engine).get_table_names())
    if existing_tables:
        raise KibakBaselineError(
            "Baseline rechazado: la base no está vacía (tablas: "
            + ", ".join(sorted(existing_tables))
            + ")."
        )


def _selected_metadata(source: MetaData, table_names: Iterable[str]) -> MetaData:
    selected = MetaData()
    missing = sorted(set(table_names) - set(source.tables))
    if missing:
        raise KibakBaselineError(f"Tablas KIBAK no encontradas en metadata: {', '.join(missing)}")
    for name in table_names:
        source.tables[name].to_metadata(selected)
    return selected


def _create_schema(engine: Engine, source: MetaData, table_names: frozenset[str]) -> None:
    metadata = _selected_metadata(source, table_names)
    metadata.create_all(bind=engine, checkfirst=False)


def _record_master_baseline(engine: Engine) -> None:
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as db:
        db.add(
            MasterSchemaMigration(
                version=KIBAK_MASTER_BASELINE_VERSION,
                name="KIBAK master baseline",
                checksum=KIBAK_MASTER_BASELINE_VERSION,
                status="current",
                notes="Clean KIBAK baseline; no legacy tables.",
            )
        )
        db.commit()


def _record_tenant_baseline(engine: Engine, company_id: int) -> None:
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as db:
        if db.scalar(select(Company.id).where(Company.id == company_id)) is None:
            raise KibakBaselineError(
                "Tenant baseline requiere que Company exista antes de registrar schema_migrations."
            )
        db.add(
            TenantSchemaMigration(
                company_id=company_id,
                version=KIBAK_TENANT_BASELINE_VERSION,
                name="KIBAK tenant baseline",
                checksum=KIBAK_TENANT_BASELINE_VERSION,
                status="current",
                notes="Clean KIBAK baseline; no legacy tables.",
            )
        )
        db.commit()


def create_kibak_master_schema(engine: Engine, *, confirmation: str | None = None) -> dict[str, object]:
    """Create and ledger a new KIBAK master database, never an existing database."""

    _assert_empty_kibak_target(engine, confirmation=confirmation)
    _create_schema(engine, MasterBase.metadata, KIBAK_MASTER_TABLES)
    _record_master_baseline(engine)
    return {"kind": "master", "version": KIBAK_MASTER_BASELINE_VERSION, "tables": sorted(KIBAK_MASTER_TABLES)}


def create_kibak_tenant_schema(
    engine: Engine,
    *,
    company_id: int,
    company_name: str,
    confirmation: str | None = None,
) -> dict[str, object]:
    """Create a new KIBAK tenant schema and its minimal Company/ledger rows."""

    _assert_empty_kibak_target(engine, confirmation=confirmation)
    _create_schema(engine, Base.metadata, KIBAK_TENANT_TABLES)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as db:
        db.add(Company(id=company_id, name=company_name, active=True))
        db.commit()
    _record_tenant_baseline(engine, company_id)
    return {"kind": "tenant", "version": KIBAK_TENANT_BASELINE_VERSION, "tables": sorted(KIBAK_TENANT_TABLES)}


__all__ = [
    "KIBAK_BASELINE_CONFIRMATION",
    "KIBAK_MASTER_BASELINE_VERSION",
    "KIBAK_TENANT_BASELINE_VERSION",
    "KIBAK_MASTER_TABLES",
    "KIBAK_TENANT_TABLES",
    "KibakBaselineError",
    "create_kibak_master_schema",
    "create_kibak_tenant_schema",
]
