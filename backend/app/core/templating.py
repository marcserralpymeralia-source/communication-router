from __future__ import annotations

import typing
from time import perf_counter
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.responses import HTMLResponse
from starlette.templating import _TemplateResponse

from app.core.assets import versioned_asset_url
from app.core.performance import performance_profiling_enabled, record_template_render
from app.core.timezones import DEFAULT_TIMEZONE, format_local_datetime, resolve_timezone_name


APP_DIR = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = APP_DIR / "templates"


def status_label(value: str | None) -> str:
    if not value:
        return ""
    labels = {
        "pedido_pendiente_revision": "Pendiente de revisión",
        "pending_review": "Pendiente de revisión",
        "unclassified": "Sin analizar",
        "routing_queued": "Routing en cola",
        "routing_processing": "Routing en curso",
        "routing_error": "Error de routing",
        "pending": "Pendiente",
        "pedido_confirmado": "Confirmado",
        "pedido_validado": "Confirmado",
        "confirmed": "Confirmado",
        "pedido_exportado": "Exportado",
        "exported": "Exportado",
        "error_exportacion": "Error de exportación",
        "export_failed": "Error de exportación",
        "error_procesamiento": "Error de procesamiento",
        "no_pedido": "No contiene pedido",
        "descartado": "Descartado",
        "discarded": "Descartado",
        "deleted": "Eliminado",
        "archived_deleted": "Eliminado",
        "cerrado": "Cerrado",
        "cancelado": "Cancelado",
        "sent": "Enviado",
        "processing": "Enviando",
        "failed": "Error de envío",
        "cancelled": "Cancelado",
        "error": "Error",
        "pedido": "Pedido",
        "consulta": "Consulta",
        "incidencia": "Incidencia",
        "no_importable": "No importable",
        "not_importable": "No importable",
        "dudoso": "Dudoso",
        "doubtful": "Dudoso",
        "reviewable": "Revisable",
        "safe": "Seguro",
        "ready": "Listo",
        "active": "Activo",
        "inactive": "Inactivo",
        "processed": "Procesado",
        "order_review_required": "Revisión requerida",
        "order_blocked": "Pedido bloqueado",
        "automation_blocked": "Automatización bloqueada",
        "confirmar_pedido": "Confirmar pedido",
        "without_score": "Sin puntuación",
        "no_score": "Sin puntuación",
        "draft": "Borrador",
        "open": "Nueva",
        "seen": "Vista",
        "resolved": "Resuelta",
        "ignored": "Ignorada",
    }
    val_clean = str(value).lower().strip()
    return labels.get(val_clean, labels.get(value, value.replace("_", " ").title()))


def status_class(value: str | None) -> str:
    value = (value or "").lower().strip()
    if value in {"pedido_pendiente_revision", "pending_review", "pending", "processing", "draft", "open", "routing_queued", "routing_processing"}:
        return "status-pending"
    if value in {"pedido_validado", "pedido_confirmado", "confirmed", "resolved", "safe", "ready"}:
        return "status-confirmed"
    if value in {"pedido_exportado", "exported", "sent"}:
        return "status-exported"
    if value in {"failed"}:
        return "status-error"
    if value in {"cerrado", "cancelado"}:
        return "status-confirmed"
    if value.startswith("error") or value in {"export_failed", "order_blocked", "automation_blocked", "routing_error"}:
        return "status-error"
    if value == "no_pedido":
        return "status-no-order"
    if value == "cancelled":
        return "status-discarded"
    if value in {"dudoso", "doubtful", "reviewable", "no_importable", "not_importable"}:
        return "status-doubtful"
    if value in {"descartado", "discarded", "deleted", "archived_deleted", "ignored"}:
        return "status-discarded"
    return ""


def page_url(request: Request, page: int | None = None, page_size: int | None = None) -> str:
    params = {key: value for key, value in request.query_params.items() if key != "partial"}
    if page is not None:
        params["page"] = str(page)
    if page_size is not None:
        params["page_size"] = str(page_size)
        params["page"] = "1"
    query = "&".join(f"{key}={value}" for key, value in params.items() if value not in ("", None))
    return f"{request.url.path}?{query}" if query else request.url.path


class PerformanceTemplateResponse(HTMLResponse):
    def __init__(
        self,
        template: typing.Any,
        context: dict[str, typing.Any],
        *,
        template_name: str,
        status_code: int = 200,
        headers: typing.Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ):
        self.template = template
        self.context = context
        self.template_name = template_name
        started_at = perf_counter()
        content = template.render(context)
        record_template_render(template_name, context, perf_counter() - started_at)
        super().__init__(content, status_code, headers, media_type, background)

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        request = self.context.get("request")
        request_scope = getattr(request, "scope", {}) if request is not None else {}
        extensions = request_scope.get("extensions", {})
        if "http.response.debug" in extensions:
            await send(
                {
                    "type": "http.response.debug",
                    "info": {
                        "template": self.template,
                        "context": self.context,
                    },
                }
            )
        await super().__call__(scope, receive, send)


class PerformanceAwareTemplates(Jinja2Templates):
    def TemplateResponse(self, *args: typing.Any, **kwargs: typing.Any) -> _TemplateResponse | PerformanceTemplateResponse:  # noqa: N802
        if not performance_profiling_enabled():
            return super().TemplateResponse(*args, **kwargs)

        if args:
            if isinstance(args[0], str):
                name = args[0]
                context = args[1] if len(args) > 1 else kwargs.get("context", {})
                status_code = args[2] if len(args) > 2 else kwargs.get("status_code", 200)
                headers = args[3] if len(args) > 3 else kwargs.get("headers")
                media_type = args[4] if len(args) > 4 else kwargs.get("media_type")
                background = args[5] if len(args) > 5 else kwargs.get("background")
                if "request" not in context:
                    raise ValueError('context must include a "request" key')
                request = context["request"]
            else:
                request = args[0]
                name = args[1] if len(args) > 1 else kwargs["name"]
                context = args[2] if len(args) > 2 else kwargs.get("context", {})
                status_code = args[3] if len(args) > 3 else kwargs.get("status_code", 200)
                headers = args[4] if len(args) > 4 else kwargs.get("headers")
                media_type = args[5] if len(args) > 5 else kwargs.get("media_type")
                background = args[6] if len(args) > 6 else kwargs.get("background")
        else:
            if "request" not in kwargs and "request" not in kwargs.get("context", {}):
                raise ValueError('context must include a "request" key')
            context = kwargs.get("context", {})
            request = kwargs.get("request", context.get("request"))
            name = typing.cast(str, kwargs["name"])
            status_code = kwargs.get("status_code", 200)
            headers = kwargs.get("headers")
            media_type = kwargs.get("media_type")
            background = kwargs.get("background")

        context.setdefault("request", request)
        for context_processor in self.context_processors:
            context.update(context_processor(request))

        template = self.get_template(name)
        return PerformanceTemplateResponse(
            template,
            context,
            template_name=name,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )


templates = PerformanceAwareTemplates(directory=str(TEMPLATES_DIR))


def tenant_timezone_context_processor(request):  # noqa: ANN001
    tenant = getattr(getattr(request, "state", None), "tenant", None)
    company = getattr(tenant, "company", None)
    timezone_name = resolve_timezone_name(getattr(company, "timezone", None) if company else None)
    return {"tenant_timezone": timezone_name, "default_timezone": DEFAULT_TIMEZONE}


def enabled_channels_context_processor(request):  # noqa: ANN001
    """Expose active tenant channels to the shared navigation after auth resolves."""
    tenant = getattr(getattr(request, "state", None), "tenant", None)
    company = getattr(tenant, "company", None)
    preloaded_channels = getattr(getattr(request, "state", None), "enabled_channels", None)
    if preloaded_channels is not None:
        return {"enabled_channels": tuple(preloaded_channels)}

    enabled_channels: tuple[str, ...] = ()
    if company and company.database_url:
        db = None
        try:
            from sqlalchemy import select

            from app.db.models import InputChannel
            from app.tenancy.database import tenant_db_session

            db = tenant_db_session(company.database_url)()
            enabled_channels = tuple(
                db.scalars(
                    select(InputChannel.key).where(
                        InputChannel.company_id == company.id,
                        InputChannel.is_active.is_(True),
                    )
                ).all()
            )
        except Exception:  # noqa: BLE001
            enabled_channels = ()
        finally:
            if db is not None:
                db.close()
    return {"enabled_channels": enabled_channels}


templates.context_processors.append(tenant_timezone_context_processor)
templates.context_processors.append(enabled_channels_context_processor)
templates.env.filters["local_dt"] = format_local_datetime
templates.env.filters["status_label"] = status_label
templates.env.filters["status_class"] = status_class
templates.env.globals["page_url"] = page_url
templates.env.globals["asset_url"] = versioned_asset_url
