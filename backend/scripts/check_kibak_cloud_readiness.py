"""Read-only KIBAK staging preflight.

The command reports PASS/WARN/FAIL and never prints secret values.  It does
not contact OpenAI, Google, IMAP, SMTP, object storage, or any cloud API.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402


def _check(results: list[tuple[str, str, str]], name: str, status: str, message: str) -> None:
    results.append((status, name, message))


def run_checks(*, check_database: bool = True) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        return [("FAIL", "configuration", f"No se pudo cargar configuración ({exc.__class__.__name__}).")]

    from app.core.attachment_storage import validate_storage_configuration
    from app.db.models import LLMSettings, Mailbox
    from app.master.database import MasterSessionLocal
    from app.master.models import MasterTenantDatabase
    from app.tenancy.database import tenant_db_session

    _check(results, "APP_SLUG", "PASS" if settings.app_slug == "kibak" else "FAIL", "kibak" if settings.app_slug == "kibak" else "debe ser kibak")
    _check(results, "APP_ENV", "PASS" if settings.environment == "staging" else "FAIL", settings.environment)
    parsed_url = urlsplit(settings.app_url.strip())
    url_ok = parsed_url.scheme == "https" and bool(parsed_url.hostname) and not parsed_url.username and not parsed_url.password
    _check(results, "APP_URL", "PASS" if url_ok else "FAIL", "HTTPS sin credenciales" if url_ok else "debe ser HTTPS sin credenciales")
    _check(results, "DEBUG", "PASS" if not settings.debug else "FAIL", "desactivado" if not settings.debug else "activado")
    _check(results, "secure cookies", "PASS" if settings.session_cookie_secure else "FAIL", "activas" if settings.session_cookie_secure else "inactivas")
    _check(results, "ALLOWED_HOSTS", "PASS" if settings.allowed_hosts and "*" not in settings.allowed_hosts else "FAIL", "explícitos" if settings.allowed_hosts and "*" not in settings.allowed_hosts else "faltan o contienen wildcard")
    _check(results, "CORS", "PASS" if settings.cors_allowed_origins and "*" not in settings.cors_allowed_origins else "FAIL", "explícito" if settings.cors_allowed_origins and "*" not in settings.cors_allowed_origins else "falta o contiene wildcard")
    external_db = settings.tenant_db_mode == "external" and settings.master_database_url.startswith("postgresql") and settings.database_url.startswith("postgresql")
    _check(results, "PostgreSQL", "PASS" if external_db else "FAIL", "master y tenant externos" if external_db else "se requiere PostgreSQL externo")
    _check(results, "Fernet", "PASS" if settings.tenant_db_encryption_key else "FAIL", "configurada" if settings.tenant_db_encryption_key else "ausente")
    storage = validate_storage_configuration()
    _check(results, "Storage", "PASS" if storage["ok"] and storage.get("persistent") else "FAIL", "object storage configurado" if storage["ok"] and storage.get("persistent") else "se requiere S3-compatible en staging")
    google_ready = bool(settings.google_oauth_client_id and settings.google_oauth_client_secret and settings.google_oauth_redirect_uri)
    _check(results, "Google OAuth", "PASS" if google_ready else "WARN", "configurado" if google_ready else "pendiente de configuración del cliente")

    if not check_database:
        _check(results, "Database inspection", "WARN", "omitida explícitamente")
        return results
    master_db = None
    try:
        master_db = MasterSessionLocal()
        tenants = master_db.scalars(select(MasterTenantDatabase).where(MasterTenantDatabase.is_active.is_(True))).all()
        if not tenants:
            _check(results, "Tenant records", "WARN", "no hay tenants activos")
        for tenant in tenants:
            database_url = tenant.get_database_url()
            if not database_url:
                _check(results, f"Tenant {tenant.company_id}", "FAIL", "database URL ausente")
                continue
            tenant_db = tenant_db_session(database_url)()
            try:
                llm = tenant_db.scalar(select(LLMSettings).where(LLMSettings.company_id == tenant.company_id))
                mailboxes = list(tenant_db.scalars(select(Mailbox).where(Mailbox.company_id == tenant.company_id)))
                safe = bool(llm and llm.simulation_mode and not llm.auto_forwarding_enabled and not any(item.enabled or item.auto_sync_enabled or item.mark_as_read_after_import for item in mailboxes))
                _check(results, f"Tenant safety {tenant.company_id}", "PASS" if safe else "FAIL", "simulation ON, forwarding OFF, mailboxes inactive" if safe else "revisar policy/mailboxes")
            except Exception as exc:  # noqa: BLE001
                _check(results, f"Tenant DB {tenant.company_id}", "FAIL", f"lectura fallida ({exc.__class__.__name__})")
            finally:
                tenant_db.close()
    except Exception as exc:  # noqa: BLE001
        _check(results, "Database inspection", "FAIL", f"lectura master fallida ({exc.__class__.__name__})")
    finally:
        if master_db is not None:
            master_db.close()
    return results


def main() -> int:
    results = run_checks()
    for status, name, message in results:
        print(f"{status} {name}: {message}")
    return 1 if any(status == "FAIL" for status, _, _ in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
