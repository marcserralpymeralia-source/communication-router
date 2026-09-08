from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.auth.redirects import login_location_for_request
from app.core.config import get_settings
from app.master.database import get_master_db
from app.master.service import load_tenant_context
from app.db.database import Base
from app.tenancy.migrations import upgrade_tenant_schema


def _connect_args(database_url: str) -> dict[str, object]:
    return {"check_same_thread": False} if database_url.startswith("sqlite") else {}


@lru_cache(maxsize=128)
def get_tenant_engine(database_url: str):
    return create_engine(database_url, connect_args=_connect_args(database_url), pool_pre_ping=True)


def tenant_db_session(database_url: str):
    engine = get_tenant_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@lru_cache(maxsize=128)
def _ensure_tenant_schema_cached(database_url: str, company_id: int | None, application_version: str | None, baseline: bool) -> tuple[tuple[str, object], ...]:
    summary = ensure_tenant_schema(database_url, company_id=company_id, application_version=application_version, baseline=baseline)
    return tuple(summary.items())


def ensure_tenant_schema_once(database_url: str, *, company_id: int | None = None, application_version: str | None = None, baseline: bool = False) -> dict:
    return dict(_ensure_tenant_schema_cached(database_url, company_id, application_version, baseline))


def clear_tenant_schema_cache() -> None:
    _ensure_tenant_schema_cached.cache_clear()


def _infer_company_id(engine) -> int | None:  # noqa: ANN001
    with engine.connect() as conn:
        inspector = inspect(conn)
        if "companies" not in inspector.get_table_names():
            return None
        value = conn.execute(text("SELECT id FROM companies ORDER BY id ASC LIMIT 1")).scalar()
    return int(value) if value is not None else None


def _validate_kibak_baseline(engine, company_id: int | None) -> dict:
    from app.migrations.kibak_baseline import KIBAK_TENANT_BASELINE_VERSION, KIBAK_TENANT_TABLES
    from app.migrations.registry import CURRENT_KIBAK_TENANT_SCHEMA_CHECKSUM, CURRENT_KIBAK_TENANT_SCHEMA_NAME, CURRENT_KIBAK_TENANT_SCHEMA_VERSION

    table_names = set(inspect(engine).get_table_names())
    missing_tables = sorted(KIBAK_TENANT_TABLES - table_names)
    if missing_tables:
        raise RuntimeError(f"KIBAK tenant schema incomplete: {', '.join(missing_tables)}")
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT version, name, checksum, execution_ms, application_version, status, "
                "applied_at, last_checked_at, last_error, notes "
                "FROM schema_migrations "
                "WHERE (:company_id IS NULL OR company_id = :company_id) "
                "ORDER BY applied_at DESC NULLS LAST, id DESC LIMIT 1"
            ),
            {"company_id": company_id},
        ).mappings().first()
    valid_versions = {KIBAK_TENANT_BASELINE_VERSION, CURRENT_KIBAK_TENANT_SCHEMA_VERSION}
    if row is None or row["version"] not in valid_versions:
        version = row["version"] if row else None
        raise RuntimeError(f"Version desconocida en schema_migrations: {version}")
    evolved = row["version"] == CURRENT_KIBAK_TENANT_SCHEMA_VERSION
    expected_checksum = CURRENT_KIBAK_TENANT_SCHEMA_CHECKSUM if evolved else KIBAK_TENANT_BASELINE_VERSION
    return {
        **dict(row),
        "current_version": row["version"],
        "current_name": CURRENT_KIBAK_TENANT_SCHEMA_NAME if evolved else "KIBAK tenant baseline",
        "current_checksum": expected_checksum,
        "is_current": row["status"] == "current" and row["checksum"] in {None, expected_checksum},
    }


def ensure_tenant_schema(database_url: str, *, company_id: int | None = None, application_version: str | None = None, baseline: bool = False) -> dict:
    engine = get_tenant_engine(database_url)
    if get_settings().app_slug.strip().lower() == "kibak" and not database_url.startswith("sqlite"):
        return _validate_kibak_baseline(engine, company_id)
    Base.metadata.create_all(bind=engine)
    resolved_company_id = company_id if company_id is not None else _infer_company_id(engine)
    if resolved_company_id is None:
        resolved_company_id = 1
    return upgrade_tenant_schema(engine, company_id=resolved_company_id, application_version=application_version, baseline=baseline)


def get_tenant_db(request: Request, master_db: Session = Depends(get_master_db)) -> Generator[Session, None, None]:
    tenant = getattr(request.state, "tenant", None)
    session = request.scope.get("session") or {}
    has_any_identity = any(session.get(key) for key in ("membership_id", "user_id", "company_id", "company_slug"))
    has_complete_identity = all(session.get(key) for key in ("membership_id", "user_id", "company_id"))
    if tenant is None:
        try:
            tenant = load_tenant_context(request, master_db)
        except SQLAlchemyError:
            if has_complete_identity:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Tenant no disponible")
            if has_any_identity and isinstance(session, dict):
                session.clear()
            raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": login_location_for_request(request)})
        if tenant:
            request.state.tenant = tenant
        elif has_complete_identity:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Membresia no disponible")

    database_url = tenant.company.database_url if tenant else None
    if not database_url:
        if has_complete_identity:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Tenant no disponible")
        if has_any_identity and isinstance(session, dict):
            session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": login_location_for_request(request)})

    try:
        ensure_tenant_schema_once(database_url, company_id=tenant.company.id)
        SessionFactory = tenant_db_session(database_url)
        db = SessionFactory()
    except SQLAlchemyError:
        if has_complete_identity:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Tenant no disponible")
        if has_any_identity and isinstance(session, dict):
            session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": login_location_for_request(request)})
    try:
        yield db
    finally:
        db.close()
