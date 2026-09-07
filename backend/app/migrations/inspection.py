from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.master.database import MasterBase
from app.master.models import MasterCompany, MasterSchemaMigration, MasterTenantDatabase, MasterUser
from app.db.database import Base
from app.db.models import BackgroundJob, JobAttempt, TenantSchemaMigration
from app.migrations.registry import (
    CURRENT_MASTER_SCHEMA_CHECKSUM,
    CURRENT_MASTER_SCHEMA_NAME,
    CURRENT_MASTER_SCHEMA_VERSION,
    CURRENT_TENANT_SCHEMA_CHECKSUM,
    CURRENT_TENANT_SCHEMA_NAME,
    CURRENT_TENANT_SCHEMA_VERSION,
    SUPPORTED_MASTER_LEGACY_VERSIONS,
    SUPPORTED_TENANT_LEGACY_VERSIONS,
)


@dataclass(slots=True)
class DatabaseReference:
    logical_name: str
    reference_type: str
    database_url: str
    engine: str
    kind_hint: str | None = None
    company_id: int | None = None
    company_slug: str | None = None
    company_name: str | None = None
    database_key: str | None = None
    source: str | None = None


@dataclass(slots=True)
class FileInfo:
    path: str
    exists: bool
    size_bytes: int | None
    modified_at: datetime | None
    checksum: str | None


@dataclass(slots=True)
class SimulationResult:
    logical_name: str
    source_path: str
    copy_path: str
    kind: str
    classification: str
    baseline_safe: bool
    dry_run: dict[str, Any]
    baseline: dict[str, Any] | None
    upgrade: dict[str, Any] | None
    second_run: dict[str, Any] | None
    before: dict[str, Any]
    after: dict[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_path(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite:///"):
        return None
    raw = database_url.replace("sqlite:///", "", 1)
    return Path(raw).expanduser()


def _safe_checksum(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_info(database_url: str) -> FileInfo:
    path = _sqlite_path(database_url)
    if not path:
        return FileInfo(path="", exists=False, size_bytes=None, modified_at=None, checksum=None)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return FileInfo(path=path.name, exists=False, size_bytes=None, modified_at=None, checksum=None)
    return FileInfo(
        path=path.name,
        exists=True,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        checksum=_safe_checksum(path),
    )


def _connect(database_url: str):
    return create_engine(database_url, connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {})


def _current_tables(kind: str) -> set[str]:
    if kind == "master":
        return {name for name in MasterBase.metadata.tables if name != "schema_migrations"}
    return {name for name in Base.metadata.tables if name != "schema_migrations"}


def _kind_from_tables(tables: set[str]) -> str:
    if not tables:
        return "unknown"
    master_markers = {"companies", "users", "memberships", "tenant_databases", "email_sync_state"}
    tenant_markers = {"customers", "products", "orders", "background_jobs", "emails"}
    if master_markers.issubset(tables) and not tenant_markers.intersection(tables):
        return "master"
    if tenant_markers.intersection(tables):
        return "tenant"
    return "unknown"


def _table_columns(insp, table_name: str) -> list[str]:  # noqa: ANN001
    try:
        return [column["name"] for column in insp.get_columns(table_name)]
    except Exception:  # noqa: BLE001
        return []


def _table_indexes(insp, table_name: str) -> list[dict[str, Any]]:  # noqa: ANN001
    try:
        return insp.get_indexes(table_name)
    except Exception:  # noqa: BLE001
        return []


def _table_foreign_keys(insp, table_name: str) -> list[dict[str, Any]]:  # noqa: ANN001
    try:
        return insp.get_foreign_keys(table_name)
    except Exception:  # noqa: BLE001
        return []


def _schema_fingerprint(insp, tables: list[str]) -> str:  # noqa: ANN001
    digest = hashlib.sha256()
    for table in sorted(tables):
        digest.update(table.encode("utf-8"))
        digest.update(b"\0")
        for column in sorted(_table_columns(insp, table)):
            digest.update(column.encode("utf-8"))
            digest.update(b"\0")
        for index in sorted(_table_indexes(insp, table), key=lambda item: item.get("name") or ""):
            digest.update((index.get("name") or "").encode("utf-8"))
            digest.update(b"\0")
        for fk in sorted(_table_foreign_keys(insp, table), key=lambda item: item.get("name") or ""):
            digest.update((fk.get("name") or "").encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def _ledger_snapshot(db, ledger_model) -> dict[str, Any]:  # noqa: ANN001
    try:
        row = db.scalar(select(ledger_model).order_by(ledger_model.applied_at.desc().nullslast(), ledger_model.id.desc()))
    except Exception:
        row = None
    if not row:
        return {
            "present": False,
            "complete": False,
            "version": None,
            "name": None,
            "checksum": None,
            "execution_ms": None,
            "application_version": None,
            "status": None,
        }
    return {
        "present": True,
        "complete": {"version", "name", "checksum", "execution_ms", "application_version", "status", "applied_at", "last_checked_at", "last_error", "notes"}.issubset(set(row.__dict__.keys())),
        "version": getattr(row, "version", None),
        "name": getattr(row, "name", None),
        "checksum": getattr(row, "checksum", None),
        "execution_ms": getattr(row, "execution_ms", None),
        "application_version": getattr(row, "application_version", None),
        "status": getattr(row, "status", None),
    }


def _job_blockers(db) -> list[str]:  # noqa: ANN001
    blockers: list[str] = []
    bind = db.get_bind()
    with bind.connect() as conn:
        inspector = inspect(conn)
        tables = set(inspector.get_table_names())
    if "background_jobs" in tables:
        dupes = db.execute(
            text(
                """
                SELECT company_id, job_type, dedupe_key, COUNT(*) AS count
                  FROM background_jobs
                 WHERE dedupe_key IS NOT NULL
                 GROUP BY company_id, job_type, dedupe_key
                HAVING COUNT(*) > 1
                LIMIT 5
                """
            )
        ).fetchall()
        if dupes:
            blockers.append("background_jobs duplicated dedupe_key")
    if "job_attempts" in tables:
        dupes = db.execute(
            text(
                """
                SELECT job_id, attempt_number, COUNT(*) AS count
                  FROM job_attempts
                 GROUP BY job_id, attempt_number
                HAVING COUNT(*) > 1
                LIMIT 5
                """
            )
        ).fetchall()
        if dupes:
            blockers.append("job_attempts duplicated attempt_number")
    return blockers


def inspect_database_url(database_url: str, *, logical_name: str | None = None, kind_hint: str | None = None) -> dict[str, Any]:
    engine = _connect(database_url)
    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            tables = insp.get_table_names()
            kind = kind_hint or _kind_from_tables(set(tables))
            file_details = file_info(database_url)
            ledger = None
            if "schema_migrations" in tables:
                session = None
                try:
                    from sqlalchemy.orm import sessionmaker

                    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
                    ledger_model = MasterSchemaMigration if kind == "master" else TenantSchemaMigration
                    ledger = _ledger_snapshot(session, ledger_model)
                finally:
                    if session is not None:
                        session.close()
            schema_fingerprint = _schema_fingerprint(insp, tables)
            current_version = CURRENT_MASTER_SCHEMA_VERSION if kind == "master" else CURRENT_TENANT_SCHEMA_VERSION
            current_name = CURRENT_MASTER_SCHEMA_NAME if kind == "master" else CURRENT_TENANT_SCHEMA_NAME
            current_checksum = CURRENT_MASTER_SCHEMA_CHECKSUM if kind == "master" else CURRENT_TENANT_SCHEMA_CHECKSUM
            target_tables = _current_tables(kind if kind in {"master", "tenant"} else "tenant")
            missing_tables = sorted(target_tables - set(tables)) if kind in {"master", "tenant"} else []
            blockers: list[str] = []
            if "background_jobs" in tables:
                blockers.extend(_job_blockers_from_insp(engine, insp))
            readiness = "ready"
            if kind == "unknown":
                readiness = "not ready"
            if ledger and ledger["present"] and ledger["status"] == "current" and ledger["version"] == current_version and ledger["checksum"] == current_checksum:
                classification = "versioned-current"
            elif ledger and ledger["present"] and ledger["version"] == current_version and ledger["checksum"] not in {None, current_checksum}:
                classification = "checksum-mismatch"
                readiness = "not ready"
            elif ledger and ledger["present"] and ledger["version"] and ledger["version"] != current_version:
                classification = "versioned-outdated"
                if ledger["version"] not in (SUPPORTED_MASTER_LEGACY_VERSIONS if kind == "master" else SUPPORTED_TENANT_LEGACY_VERSIONS) and kind in {"master", "tenant"}:
                    readiness = "not ready"
            elif kind in {"master", "tenant"} and not missing_tables and not blockers:
                classification = "current-without-ledger"
            elif kind in {"master", "tenant"}:
                classification = "legacy-recognized"
            else:
                classification = "unknown-schema"
                readiness = "not ready"
            if blockers:
                classification = "blocked-by-data"
                readiness = "not ready"
            pending: list[str] = []
            if kind == "master":
                pending = ["master schema ledger"] if classification != "versioned-current" else []
            elif kind == "tenant":
                pending = ["tenant schema ledger", "tenant operational compatibility", "tenant job reliability"] if classification != "versioned-current" else []
            baseline_safe = classification in {"current-without-ledger", "legacy-recognized", "versioned-outdated", "versioned-current"}
            if classification == "unknown-schema":
                baseline_safe = False
            return {
                "logical_name": logical_name or file_details.path,
                "reference_type": kind_hint or kind,
                "engine": "sqlite" if database_url.startswith("sqlite") else "postgresql",
                "database_url": database_url,
                "exists": file_details.exists if file_details.path else True,
                "size_bytes": file_details.size_bytes,
                "modified_at": file_details.modified_at,
                "checksum": file_details.checksum,
                "tables": tables,
                "table_count": len(tables),
                "kind": kind,
                "classification": classification,
                "readiness": readiness,
                "version": ledger["version"] if ledger else None,
                "name": ledger["name"] if ledger else None,
                "ledger_checksum": ledger["checksum"] if ledger else None,
                "ledger_complete": ledger["complete"] if ledger else False,
                "current_version": current_version,
                "current_name": current_name,
                "current_checksum": current_checksum,
                "schema_fingerprint": schema_fingerprint,
                "missing_tables": missing_tables,
                "pending": pending,
                "pending_count": len(pending),
                "blockers": blockers,
                "baseline_safe": baseline_safe,
                "baseline_proposal": current_version if classification == "current-without-ledger" else ledger["version"] if ledger and ledger["version"] else None,
            }
    except (SQLAlchemyError, sqlite3.Error, OSError) as exc:
        return {
            "logical_name": logical_name or Path(database_url.split("///")[-1]).name,
            "reference_type": kind_hint or "unknown",
            "engine": "sqlite" if database_url.startswith("sqlite") else "postgresql",
            "database_url": database_url,
            "exists": False,
            "size_bytes": file_info(database_url).size_bytes,
            "modified_at": file_info(database_url).modified_at,
            "checksum": file_info(database_url).checksum,
            "tables": [],
            "table_count": 0,
            "kind": "unknown",
            "classification": "connection-error",
            "readiness": "not ready",
            "version": None,
            "name": None,
            "ledger_checksum": None,
            "ledger_complete": False,
            "current_version": CURRENT_MASTER_SCHEMA_VERSION if kind_hint == "master" else CURRENT_TENANT_SCHEMA_VERSION,
            "current_name": CURRENT_MASTER_SCHEMA_NAME if kind_hint == "master" else CURRENT_TENANT_SCHEMA_NAME,
            "current_checksum": CURRENT_MASTER_SCHEMA_CHECKSUM if kind_hint == "master" else CURRENT_TENANT_SCHEMA_CHECKSUM,
            "schema_fingerprint": None,
            "missing_tables": [],
            "pending": [],
            "pending_count": 0,
            "blockers": [str(exc)],
            "baseline_safe": False,
            "baseline_proposal": None,
        }
    finally:
        engine.dispose()


def _job_blockers_from_insp(engine, insp) -> list[str]:  # noqa: ANN001
    blockers: list[str] = []
    if "background_jobs" in insp.get_table_names():
        with engine.connect() as conn:
            dupes = conn.execute(
                text(
                    """
                    SELECT company_id, job_type, dedupe_key, COUNT(*) AS count
                      FROM background_jobs
                     WHERE dedupe_key IS NOT NULL
                     GROUP BY company_id, job_type, dedupe_key
                    HAVING COUNT(*) > 1
                    LIMIT 1
                    """
                )
            ).fetchall()
        if dupes:
            blockers.append("background_jobs duplicated dedupe_key")
    if "job_attempts" in insp.get_table_names():
        with engine.connect() as conn:
            dupes = conn.execute(
                text(
                    """
                    SELECT job_id, attempt_number, COUNT(*) AS count
                      FROM job_attempts
                     GROUP BY job_id, attempt_number
                    HAVING COUNT(*) > 1
                    LIMIT 1
                    """
                )
            ).fetchall()
        if dupes:
            blockers.append("job_attempts duplicated attempt_number")
    return blockers


def resolve_master_references(master_db) -> list[DatabaseReference]:  # noqa: ANN001
    settings = get_settings()
    refs: list[DatabaseReference] = []
    refs.append(
        DatabaseReference(
            logical_name="master",
            reference_type="master",
            database_url=settings.master_database_url,
            engine="sqlite" if settings.master_database_url.startswith("sqlite") else "postgresql",
            kind_hint="master",
            source="settings.master_database_url",
        )
    )
    companies = master_db.scalars(select(MasterCompany).order_by(MasterCompany.id)).all()
    tenants = master_db.scalars(select(MasterTenantDatabase).order_by(MasterTenantDatabase.company_id)).all()
    user_count = master_db.scalar(select(MasterUser.id).limit(1))
    for tenant in tenants:
        company = tenant.company
        database_url = tenant.get_database_url()
        refs.append(
            DatabaseReference(
                logical_name=f"tenant:{company.slug if company else tenant.database_key or tenant.company_id}",
                reference_type="tenant",
                database_url=database_url,
                engine="sqlite" if database_url.startswith("sqlite") else "postgresql",
                kind_hint="tenant",
                company_id=tenant.company_id,
                company_slug=company.slug if company else None,
                company_name=company.name if company else None,
                database_key=tenant.database_key,
                source="master.tenant_databases",
            )
        )
    return refs


def discover_sqlite_files(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for pattern in ("*.db", "*.sqlite"):
        candidates.extend(root.rglob(pattern))
    ignored_fragments = {"/.git/", "/.venv/", "/storage/migration-simulations/"}
    filtered: list[Path] = []
    for candidate in candidates:
        candidate_str = candidate.as_posix()
        if any(fragment in candidate_str for fragment in ignored_fragments):
            continue
        if candidate.is_file():
            filtered.append(candidate)
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in filtered:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(candidate)
    return sorted(unique)


def copy_sqlite_database(source_path: Path, target_root: Path, *, label: str) -> Path:
    target_root.mkdir(parents=True, exist_ok=True)
    timestamp = _now().strftime("%Y%m%d-%H%M%S-%f")
    target_dir = target_root / timestamp
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{label}-{source_path.name}"
    if target_path.exists():
        raise FileExistsError(f"Ya existe una copia para {target_path.name}")
    shutil.copy2(source_path, target_path)
    if target_path.stat().st_size != source_path.stat().st_size:
        raise IOError("La copia no conserva el tamaño del archivo original")
    with sqlite3.connect(target_path.as_posix()) as conn:
        conn.execute("SELECT 1")
    return target_path


def snapshot_counts(database_url: str) -> dict[str, int]:
    engine = _connect(database_url)
    try:
        counts: dict[str, int] = {}
        with engine.connect() as conn:
            insp = inspect(conn)
            for table in insp.get_table_names():
                try:
                    counts[table] = int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
                except Exception:  # noqa: BLE001
                    counts[table] = -1
        return counts
    finally:
        engine.dispose()


def render_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    header = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join(["---"] * len(headers)) + " |"
    body = "\n".join("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join([header, separator, body]) if body else "\n".join([header, separator])


def _sqlite_database_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def inventory_records(master_db, project_root: Path) -> list[dict[str, Any]]:  # noqa: ANN001
    referenced_paths: set[Path] = set()
    records: list[dict[str, Any]] = []
    for reference in resolve_master_references(master_db):
        if reference.database_url.startswith("sqlite:///"):
            path = _sqlite_path(reference.database_url)
            if path:
                referenced_paths.add(path.resolve())
        inspection = inspect_database_url(reference.database_url, logical_name=reference.logical_name, kind_hint=reference.kind_hint)
        records.append(
            {
                "logical_name": reference.logical_name,
                "type": reference.reference_type,
                "engine": inspection["engine"],
                "source": reference.source or "master",
                "exists": inspection["exists"],
                "size_bytes": inspection["size_bytes"],
                "state": inspection["classification"],
                "readiness": inspection["readiness"],
                "company_slug": reference.company_slug,
                "company_name": reference.company_name,
                "database_key": reference.database_key,
                "path": inspection["logical_name"],
                "baseline_safe": inspection["baseline_safe"],
                "pending_count": inspection["pending_count"],
                "blockers": inspection["blockers"],
            }
        )

    for path in discover_sqlite_files(project_root):
        resolved = path.resolve()
        if resolved in referenced_paths:
            continue
        inspection = inspect_database_url(_sqlite_database_url(path), logical_name=path.name)
        records.append(
            {
                "logical_name": path.name,
                "type": inspection["kind"],
                "engine": inspection["engine"],
                "source": "filesystem",
                "exists": inspection["exists"],
                "size_bytes": inspection["size_bytes"],
                "state": inspection["classification"],
                "readiness": inspection["readiness"],
                "company_slug": None,
                "company_name": None,
                "database_key": None,
                "path": inspection["logical_name"],
                "baseline_safe": inspection["baseline_safe"],
                "pending_count": inspection["pending_count"],
                "blockers": inspection["blockers"],
            }
        )
    return sorted(records, key=lambda item: (item["type"] or "", item["logical_name"]))


def simulate_sqlite_reference(
    source_path: Path,
    target_root: Path,
    *,
    label: str,
    kind_hint: str | None = None,
    company_id: int | None = None,
    application_version: str | None = None,
) -> dict[str, Any]:
    copy_path = copy_sqlite_database(source_path, target_root, label=label)
    copy_url = _sqlite_database_url(copy_path)
    before = inspect_database_url(copy_url, logical_name=copy_path.name, kind_hint=kind_hint)
    from app.master.migrations import upgrade_master_schema
    from app.tenancy.migrations import upgrade_tenant_schema
    engine = _connect(copy_url)

    dry_run_result: dict[str, Any]
    baseline_result: dict[str, Any] | None = None
    upgrade_result: dict[str, Any] | None = None
    second_run_result: dict[str, Any] | None = None

    try:
        if before["kind"] == "master":
            dry_run_result = upgrade_master_schema(engine, application_version=application_version, dry_run=True)
            if before["baseline_safe"]:
                baseline_result = upgrade_master_schema(engine, application_version=application_version, baseline=True)
                upgrade_result = upgrade_master_schema(engine, application_version=application_version)
                second_run_result = upgrade_master_schema(engine, application_version=application_version)
        else:
            dry_run_result = upgrade_tenant_schema(
                engine,
                company_id=company_id,
                application_version=application_version,
                dry_run=True,
            )
            if before["baseline_safe"]:
                baseline_result = upgrade_tenant_schema(
                    engine,
                    company_id=company_id,
                    application_version=application_version,
                    baseline=True,
                )
                upgrade_result = upgrade_tenant_schema(engine, company_id=company_id, application_version=application_version)
                second_run_result = upgrade_tenant_schema(engine, company_id=company_id, application_version=application_version)
        after = inspect_database_url(copy_url, logical_name=copy_path.name, kind_hint=kind_hint)
    finally:
        engine.dispose()
    return {
        "logical_name": before["logical_name"],
        "source_path": source_path.as_posix(),
        "copy_path": copy_path.as_posix(),
        "kind": before["kind"],
        "classification": before["classification"],
        "baseline_safe": before["baseline_safe"],
        "dry_run": dry_run_result,
        "baseline": baseline_result,
        "upgrade": upgrade_result,
        "second_run": second_run_result,
        "before": before,
        "after": after,
    }
