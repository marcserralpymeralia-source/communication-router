from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import get_settings
from app.core.assets import STATIC_DIR, versioned_asset_url
from app.core.logging import configure_logging
from app.core.performance import configure_performance
from app.core.lifespan import app_lifespan
from app.core.middleware import branding_middleware
from app.core.router_registry import get_registered_routers
from app.core.templating import templates
from app.settings.branding import branding_css_vars

logger = logging.getLogger(__name__)


class CachedStaticFiles(StaticFiles):
    """Cache bundled assets for a long time while keeping uploads refreshable."""

    def file_response(self, full_path, stat_result, scope, status_code=200):  # noqa: ANN001
        response = super().file_response(full_path, stat_result, scope, status_code)
        request_path = (scope.get("path") or "").lower()
        if "/uploads/" in request_path:
            response.headers["Cache-Control"] = "public, max-age=3600"
        elif request_path.endswith((".css", ".js")):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response


def internal_server_error_response(request) -> JSONResponse:  # noqa: ANN001
    request_id = getattr(request.state, "request_id", None)
    correlation_id = getattr(request.state, "correlation_id", None) or request_id
    payload = {
        "error": "internal_error",
        "message": "Ha ocurrido un error interno.",
        "request_id": request_id,
        "correlation_id": correlation_id,
    }
    response = JSONResponse(status_code=500, content=payload)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    if correlation_id:
        response.headers["X-Correlation-ID"] = correlation_id
    return response


def sqlalchemy_error_response(request, exc: Exception):  # noqa: ANN001
    logger.exception(
        "AUTH_REASON=schema_error route=%s method=%s error_type=%s",
        request.url.path,
        request.method,
        exc.__class__.__name__,
    )
    accept = (request.headers.get("accept") or "").lower()
    if request.method in {"GET", "HEAD"} or "text/html" in accept:
        response = HTMLResponse(
            "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\"><title>Servicio temporalmente no disponible</title></head>"
            "<body><main style=\"font-family:system-ui,sans-serif;padding:24px\">"
            "<h1>Servicio temporalmente no disponible</h1>"
            "<p>No se ha podido cargar esta pantalla por un problema temporal de base de datos.</p>"
            "</main></body></html>",
            status_code=503,
        )
        request_id = getattr(request.state, "request_id", None)
        correlation_id = getattr(request.state, "correlation_id", None) or request_id
        if request_id:
            response.headers["X-Request-ID"] = request_id
        if correlation_id:
            response.headers["X-Correlation-ID"] = correlation_id
        return response
    payload = {
        "error": "database_unavailable",
        "message": "La base de datos no está disponible temporalmente.",
        "request_id": getattr(request.state, "request_id", None),
        "correlation_id": getattr(request.state, "correlation_id", None) or getattr(request.state, "request_id", None),
    }
    response = JSONResponse(status_code=503, content=payload)
    request_id = payload["request_id"]
    correlation_id = payload["correlation_id"]
    if request_id:
        response.headers["X-Request-ID"] = request_id
    if correlation_id:
        response.headers["X-Correlation-ID"] = correlation_id
    return response


def create_app() -> FastAPI:
    configure_logging()
    configure_performance()
    settings = get_settings()
    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=app_lifespan)

    @app.middleware("http")
    async def security_headers(request, call_next):  # noqa: ANN001
        response = await call_next(request)
        if settings.environment in {"staging", "production"}:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
            if settings.app_url.startswith("https://"):
                response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response
    # The branding middleware needs the decoded session to resolve the tenant.
    # Register it before SessionMiddleware so Starlette executes the session
    # middleware first on incoming requests.
    app.middleware("http")(branding_middleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key,
        session_cookie=settings.session_cookie,
        https_only=bool(settings.session_cookie_secure),
        same_site=settings.session_cookie_samesite or "lax",
        max_age=settings.session_max_age,
        domain=settings.session_cookie_domain or None,
    )
    if settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    cors_origins = settings.cors_allowed_origins
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.mount("/static", CachedStaticFiles(directory=STATIC_DIR.as_posix()), name="static")
    templates.env.globals["branding_css_vars"] = branding_css_vars
    templates.env.globals["app_settings"] = settings
    templates.env.globals["asset_url"] = versioned_asset_url

    for router in get_registered_routers():
        app.include_router(router)

    @app.exception_handler(Exception)
    async def _fallback_exception_handler(request, exc):  # noqa: ANN001
        return internal_server_error_response(request)

    @app.exception_handler(SQLAlchemyError)
    async def _sqlalchemy_exception_handler(request, exc):  # noqa: ANN001
        return sqlalchemy_error_response(request, exc)

    return app
