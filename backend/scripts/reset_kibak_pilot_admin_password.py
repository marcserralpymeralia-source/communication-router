"""Reset the fixed KIBAK Pilot admin password through an interactive prompt.

The password is accepted only through ``getpass`` and exists only in memory
long enough to create the application hash.  This script never creates users,
changes memberships, or contacts any external provider.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.models import Company, User  # noqa: E402
from app.master.database import MasterSessionLocal  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.tenancy.database import tenant_db_session  # noqa: E402


PILOT_NAME = "KIBAK Pilot"
PILOT_SLUG = "kibak-pilot"
PILOT_DATABASE_NAME = "kibak_tenant_pilot"
PILOT_ADMIN_EMAIL = "admin@kibak-pilot.local"
RESET_CONFIRMATION = "KIBAK_PILOT_PASSWORD_RESET"
_PROHIBITED_DATABASE_MARKERS = ("anchi", "gemavi", "comavi")
_ALLOWED_LOCAL_ENVIRONMENTS = {"development", "local", "test"}


class ResetTargets(NamedTuple):
    company: MasterCompany
    tenant_database: MasterTenantDatabase
    master_user: MasterUser
    membership: CompanyMembership
    tenant_company: Company
    tenant_user: User


def _assert_kibak_local_runtime(settings) -> None:
    if settings.app_slug.strip().lower() != "kibak":
        raise RuntimeError("Reset rechazado: APP_SLUG debe ser exactamente 'kibak'.")
    environment = settings.environment.strip().lower()
    if environment not in _ALLOWED_LOCAL_ENVIRONMENTS:
        raise RuntimeError("Reset rechazado: APP_ENV debe ser development/local/test.")


def _assert_safe_tenant_url(database_url: str) -> None:
    lowered = database_url.lower()
    if any(marker in lowered for marker in _PROHIBITED_DATABASE_MARKERS):
        raise RuntimeError("Reset rechazado: la base identifica un recurso ANCHI/GEMAVI.")
    if make_url(database_url).drivername.split("+", 1)[0] != "postgresql":
        raise RuntimeError("Reset rechazado: el tenant piloto requiere PostgreSQL.")


def load_reset_targets(master_db: Session, tenant_db: Session) -> ResetTargets:
    """Resolve and validate only the fixed pilot admin identity."""

    company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == PILOT_SLUG))
    if company is None:
        raise RuntimeError("Reset rechazado: no existe el tenant kibak-pilot.")
    if company.name != PILOT_NAME or not company.active:
        raise RuntimeError("Reset rechazado: el tenant kibak-pilot no coincide con KIBAK Pilot.")

    conflicting_name = master_db.scalar(
        select(MasterCompany).where(MasterCompany.name == PILOT_NAME, MasterCompany.slug != PILOT_SLUG)
    )
    if conflicting_name is not None:
        raise RuntimeError("Reset rechazado: existe otro tenant con la identidad de KIBAK Pilot.")

    tenant_databases = master_db.scalars(
        select(MasterTenantDatabase).where(
            MasterTenantDatabase.company_id == company.id,
            MasterTenantDatabase.is_active.is_(True),
        )
    ).all()
    if len(tenant_databases) != 1:
        raise RuntimeError("Reset rechazado: la base activa del tenant piloto no es unívoca.")
    tenant_database = tenant_databases[0]
    if tenant_database.database_key != PILOT_SLUG or tenant_database.database_type != "postgresql":
        raise RuntimeError("Reset rechazado: la base asociada no es la base PostgreSQL del piloto.")

    tenant_company = tenant_db.get(Company, company.id)
    if tenant_company is None or tenant_company.name != PILOT_NAME or not tenant_company.active:
        raise RuntimeError("Reset rechazado: la Company de la base piloto no coincide.")

    master_user = master_db.scalar(select(MasterUser).where(MasterUser.email == PILOT_ADMIN_EMAIL))
    if master_user is None or not master_user.is_active:
        raise RuntimeError("Reset rechazado: no existe el administrador piloto activo.")
    membership = master_db.scalar(
        select(CompanyMembership).where(
            CompanyMembership.user_id == master_user.id,
            CompanyMembership.company_id == company.id,
            CompanyMembership.is_active.is_(True),
        )
    )
    if membership is None:
        raise RuntimeError("Reset rechazado: el administrador no pertenece a kibak-pilot.")

    tenant_user = tenant_db.scalar(select(User).where(User.email == PILOT_ADMIN_EMAIL))
    if tenant_user is None or tenant_user.company_id != company.id or not tenant_user.is_active:
        raise RuntimeError("Reset rechazado: el usuario tenant no coincide con kibak-pilot.")

    return ResetTargets(company, tenant_database, master_user, membership, tenant_company, tenant_user)


def reset_password_records(
    master_db: Session,
    tenant_db: Session,
    *,
    new_password: str,
) -> None:
    """Set the same application hash on the two existing pilot user records."""

    if not new_password or not new_password.strip():
        raise ValueError("La nueva contraseña no puede estar vacía.")
    targets = load_reset_targets(master_db, tenant_db)
    new_hash = hash_password(new_password)
    targets.master_user.password_hash = new_hash
    targets.tenant_user.password_hash = new_hash
    master_db.flush()
    tenant_db.flush()


def _tenant_session_for(tenant_database: MasterTenantDatabase):
    database_url = tenant_database.get_database_url()
    if not database_url:
        raise RuntimeError("Reset rechazado: la base del tenant no está configurada.")
    _assert_safe_tenant_url(database_url)
    return tenant_db_session(database_url)()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restablece la contraseña del administrador piloto KIBAK.")
    parser.parse_args(argv)
    master_db = tenant_db = None
    password = confirmation = None
    try:
        settings = get_settings()
        _assert_kibak_local_runtime(settings)
        master_db = MasterSessionLocal()
        company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == PILOT_SLUG))
        if company is None:
            raise RuntimeError("Reset rechazado: no existe el tenant kibak-pilot.")
        tenant_record = master_db.scalar(
            select(MasterTenantDatabase).where(
                MasterTenantDatabase.company_id == company.id,
                MasterTenantDatabase.is_active.is_(True),
            )
        )
        if tenant_record is None:
            raise RuntimeError("Reset rechazado: no existe una base activa para kibak-pilot.")
        tenant_db = _tenant_session_for(tenant_record)
        load_reset_targets(master_db, tenant_db)

        print(f"Tenant: {PILOT_NAME}")
        print(f"Usuario: {PILOT_ADMIN_EMAIL}")
        print("Acción: Reset password")
        if input(f"Escribe {RESET_CONFIRMATION} para continuar: ").strip() != RESET_CONFIRMATION:
            print("Reset cancelado.")
            return 1

        password = getpass.getpass("Nueva contraseña: ")
        confirmation = getpass.getpass("Repite la nueva contraseña: ")
        if password != confirmation:
            raise RuntimeError("Las contraseñas no coinciden.")
        reset_password_records(master_db, tenant_db, new_password=password)
        tenant_db.commit()
        master_db.commit()
        print("Contraseña restablecida.")
        return 0
    except KeyboardInterrupt:
        print("Reset cancelado.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        if tenant_db is not None:
            tenant_db.rollback()
        if master_db is not None:
            master_db.rollback()
        print(f"Reset KIBAK no ejecutado: {exc}", file=sys.stderr)
        return 1
    finally:
        password = confirmation = None
        if tenant_db is not None:
            tenant_db.close()
        if master_db is not None:
            master_db.close()


if __name__ == "__main__":
    raise SystemExit(main())
