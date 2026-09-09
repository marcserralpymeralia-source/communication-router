"""Resolve tenant database URLs for the current runtime environment."""

from __future__ import annotations

from sqlalchemy.engine import make_url

from app.core.config import get_settings


def resolve_tenant_database_url(database_url: str, *, settings=None) -> str:
    """Return the tenant URL usable by the current process.

    The encrypted master value remains the canonical provisioning/host URL.
    Docker services can explicitly override only the network endpoint while
    preserving credentials and database name in memory.
    """

    raw_url = (database_url or "").strip()
    if not raw_url:
        return raw_url
    parsed = make_url(raw_url)
    if parsed.drivername.split("+", 1)[0] != "postgresql":
        return raw_url
    runtime_settings = settings or get_settings()
    runtime_host = (runtime_settings.tenant_runtime_database_host or "").strip()
    if not runtime_host:
        return raw_url
    runtime_port = runtime_settings.tenant_runtime_database_port or parsed.port
    resolved = parsed.set(host=runtime_host, port=runtime_port)
    return resolved.render_as_string(hide_password=False)


__all__ = ["resolve_tenant_database_url"]
