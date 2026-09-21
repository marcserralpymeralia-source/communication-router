from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable, Iterable

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationSpec:
    version: str
    name: str
    checksum: str
    upgrade: Callable[[object, bool], list[str]]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def latest_state(session: Session, model):  # noqa: ANN001
    try:
        return session.scalar(select(model).order_by(model.applied_at.desc().nullslast(), model.id.desc()))
    except Exception:  # noqa: BLE001
        return None


def registry_checksum(specs: Iterable[MigrationSpec]) -> str:
    from hashlib import sha256

    digest = sha256()
    for spec in specs:
        digest.update(spec.version.encode("utf-8"))
        digest.update(b"\0")
        digest.update(spec.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(spec.checksum.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def migration_summary(state, *, current_version: str, current_name: str, current_checksum: str) -> dict:
    if not state:
        return {
            "version": None,
            "name": None,
            "checksum": None,
            "execution_ms": None,
            "application_version": None,
            "status": "missing",
            "applied_at": None,
            "last_checked_at": None,
            "last_error": None,
            "notes": None,
            "current_version": current_version,
            "current_name": current_name,
            "current_checksum": current_checksum,
            "is_current": False,
        }
    return {
        "version": state.version,
        "name": getattr(state, "name", None),
        "checksum": getattr(state, "checksum", None),
        "execution_ms": getattr(state, "execution_ms", None),
        "application_version": getattr(state, "application_version", None),
        "status": getattr(state, "status", "missing"),
        "applied_at": getattr(state, "applied_at", None),
        "last_checked_at": getattr(state, "last_checked_at", None),
        "last_error": getattr(state, "last_error", None),
        "notes": getattr(state, "notes", None),
        "current_version": current_version,
        "current_name": current_name,
        "current_checksum": current_checksum,
        "is_current": state.version == current_version and getattr(state, "checksum", None) == current_checksum and getattr(state, "status", None) == "current",
    }


def register_schema_baseline(
    session: Session,
    model,
    *,
    version: str,
    name: str,
    checksum: str,
    application_version: str | None = None,
    company_id: int | None = None,
    notes: str | None = None,
) -> dict:
    state = latest_state(session, model)
    now = now_utc()
    _store_state(
        session,
        model,
        state,
        version=version,
        name=name,
        checksum=checksum,
        execution_ms=0,
        application_version=application_version,
        status="current",
        applied_at=state.applied_at if state and getattr(state, "applied_at", None) else now,
        last_checked_at=now,
        last_error=None,
        company_id=company_id,
    )
    if notes and hasattr(model, "__table__"):
        refreshed = session.scalar(select(model).order_by(model.applied_at.desc().nullslast(), model.id.desc()))
        if refreshed and hasattr(refreshed, "notes"):
            refreshed.notes = notes
            refreshed.updated_at = now_utc()
            session.commit()
            state = refreshed
    state = session.scalar(select(model).order_by(model.applied_at.desc().nullslast(), model.id.desc()))
    return migration_summary(state, current_version=version, current_name=name, current_checksum=checksum)


def run_migration_plan(
    engine,
    session: Session,
    model,
    specs: list[MigrationSpec],
    *,
    application_version: str | None = None,
    company_id: int | None = None,
    allowed_legacy_versions: set[str] | None = None,
    baseline: bool = False,
    dry_run: bool = False,
) -> dict:
    if not specs:
        raise MigrationError("No hay migraciones registradas")

    current_version = specs[-1].version
    current_name = specs[-1].name
    current_checksum = registry_checksum(specs)
    state = latest_state(session, model)

    allowed_legacy_versions = allowed_legacy_versions or set()
    if state and state.version not in {spec.version for spec in specs} and state.version not in allowed_legacy_versions:
        raise MigrationError(f"Version desconocida en schema_migrations: {state.version}")
    if state and state.version == current_version and getattr(state, "checksum", None) not in {None, current_checksum}:
        raise MigrationError("El checksum guardado no coincide con el esquema registrado")

    if baseline:
        if not dry_run:
            _store_state(
                session,
                model,
                state,
                version=current_version,
                name=current_name,
                checksum=current_checksum,
                execution_ms=0,
                application_version=application_version,
                status="current",
                applied_at=state.applied_at if state and state.applied_at else now_utc(),
                last_checked_at=now_utc(),
                last_error=None,
                company_id=company_id,
            )
        refreshed_state = session.scalar(select(model).order_by(model.applied_at.desc().nullslast(), model.id.desc()))
        return migration_summary(
            refreshed_state,
            current_version=current_version,
            current_name=current_name,
            current_checksum=current_checksum,
        )

    applied_index = _spec_index(specs, state.version if state else None)
    pending = specs[applied_index + 1 :] if applied_index is not None else specs
    # Release the read transaction used to inspect the ledger before opening
    # the per-migration connection/transaction below.
    session.rollback()
    total_execution_ms = 0
    planned_actions: list[str] = []
    applied_versions: list[str] = []
    last_summary: dict | None = None
    first_pending_index = applied_index + 1 if applied_index is not None else 0

    for offset, spec in enumerate(pending):
        spec_index = first_pending_index + offset
        start = perf_counter()
        if dry_run:
            actions = spec.upgrade(engine, dry_run=True)
            elapsed_ms = int(round((perf_counter() - start) * 1000))
            planned_actions.extend(actions)
            applied_versions.append(spec.version)
            total_execution_ms += elapsed_ms
            continue

        # Each migration owns one database transaction. The ledger update is
        # flushed on the same connection before the transaction commits.
        with engine.begin() as connection:
            migration_session = sessionmaker(
                bind=connection,
                autoflush=False,
                expire_on_commit=False,
            )()
            try:
                actions = spec.upgrade(connection, dry_run=False)
                elapsed_ms = int(round((perf_counter() - start) * 1000))
                migration_state = latest_state(migration_session, model)
                _store_state(
                    migration_session,
                    model,
                    migration_state,
                    version=spec.version,
                    name=spec.name,
                    checksum=registry_checksum(specs[: spec_index + 1]),
                    execution_ms=elapsed_ms,
                    application_version=application_version,
                    status="current",
                    applied_at=(
                        migration_state.applied_at
                        if migration_state and getattr(migration_state, "applied_at", None)
                        else now_utc()
                    ),
                    last_checked_at=now_utc(),
                    last_error=None,
                    company_id=company_id,
                    commit=False,
                )
                migration_session.flush()
                last_summary = migration_summary(
                    migration_state,
                    current_version=current_version,
                    current_name=current_name,
                    current_checksum=current_checksum,
                )
            finally:
                migration_session.close()

        total_execution_ms += elapsed_ms
        planned_actions.extend(actions)
        applied_versions.append(spec.version)

    should_persist = not dry_run and not applied_versions and (
        state is not None
        and state.version == current_version
        and getattr(state, "checksum", None) == current_checksum
        and getattr(state, "status", None) != "current"
    )
    if should_persist:
        _store_state(
            session,
            model,
            state,
            version=current_version,
            name=current_name,
            checksum=current_checksum,
            execution_ms=getattr(state, "execution_ms", 0),
            application_version=application_version,
            status="current",
            applied_at=state.applied_at if state and getattr(state, "applied_at", None) else now_utc(),
            last_checked_at=now_utc(),
            last_error=None,
            company_id=company_id,
        )

    summary = last_summary or migration_summary(
        state,
        current_version=current_version,
        current_name=current_name,
        current_checksum=current_checksum,
    )
    summary["applied_versions"] = applied_versions
    summary["planned_actions"] = planned_actions
    summary["execution_ms"] = total_execution_ms if applied_versions else summary["execution_ms"]
    summary["dry_run"] = dry_run
    return summary


def _spec_index(specs: list[MigrationSpec], version: str | None) -> int | None:
    if not version:
        return None
    for index, spec in enumerate(specs):
        if spec.version == version:
            return index
    return None


def _store_state(
    session: Session,
    model,
    state,
    *,
    version: str,
    name: str,
    checksum: str,
    execution_ms: int,
    application_version: str | None,
    status: str,
    applied_at: datetime,
    last_checked_at: datetime,
    last_error: str | None,
    company_id: int | None = None,
    commit: bool = True,
) -> None:
    if state is None and company_id is not None and hasattr(model, "__tablename__"):
        table_name = model.__tablename__
        update = text(
            f"""
            UPDATE {table_name}
               SET version = :version,
                   name = :name,
                   checksum = :checksum,
                   execution_ms = :execution_ms,
                   application_version = :application_version,
                   status = :status,
                   applied_at = :applied_at,
                   last_checked_at = :last_checked_at,
                   last_error = :last_error,
                   updated_at = :updated_at
             WHERE company_id = :company_id
            """
        )
        result = session.execute(
            update,
            {
                "version": version,
                "name": name,
                "checksum": checksum,
                "execution_ms": execution_ms,
                "application_version": application_version,
                "status": status,
                "applied_at": applied_at,
                "last_checked_at": last_checked_at,
                "last_error": last_error,
                "updated_at": now_utc(),
                "company_id": company_id,
            },
        )
        if result.rowcount:
            if commit:
                session.commit()
            return
    if state is None:
        state = model()
        session.add(state)
    if company_id is not None and hasattr(state, "company_id"):
        state.company_id = company_id
    state.version = version
    if hasattr(state, "name"):
        state.name = name
    if hasattr(state, "checksum"):
        state.checksum = checksum
    if hasattr(state, "execution_ms"):
        state.execution_ms = execution_ms
    if hasattr(state, "application_version"):
        state.application_version = application_version
    if hasattr(state, "status"):
        state.status = status
    if hasattr(state, "applied_at"):
        state.applied_at = applied_at
    if hasattr(state, "last_checked_at"):
        state.last_checked_at = last_checked_at
    if hasattr(state, "last_error"):
        state.last_error = last_error
    if hasattr(state, "updated_at"):
        state.updated_at = now_utc()
    if commit:
        session.commit()
