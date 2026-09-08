from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.config import get_settings
from app.core.pagination import normalize_page
from app.core.templating import templates
from app.db.models import Alert, Email, InboundMessage, Order, utcnow
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db
from app.learning.routes import active_channels_for_company

router = APIRouter()


def alert_is_active(alert: Alert) -> bool:
    return alert.status not in {"resolved", "ignored"}


def alert_severity_label(severity: str) -> str:
    return {
        "critical": "Crítica",
        "high": "Alta",
        "medium": "Media",
        "low": "Baja",
        "info": "Informativa",
    }.get(severity, severity.title())


def alert_status_label(status: str) -> str:
    return {
        "open": "Nueva",
        "seen": "Vista",
        "processing": "En proceso",
        "resolved": "Resuelta",
        "ignored": "Ignorada",
    }.get(status, status.title())


def alert_type_label(alert_type: str | None) -> str:
    mapping = {
        "order_review_required": "Revisión requerida",
        "order_blocked": "Pedido bloqueado",
        "automation_blocked": "Automatización bloqueada",
        "export_failed": "Error de exportación",
        "extraction_error": "Error de extracción",
        "llm_failed": "Error de IA",
        "inbound_failed": "Error de entrada",
        "general_error": "Incidencia",
        "info": "Información",
    }
    return mapping.get(alert_type or "", (alert_type or "").replace("_", " ").title())


def alert_default_action(alert: Alert) -> tuple[str, str]:
    if alert.order_id:
        if alert.alert_type == "export_failed":
            return "Reintentar", f"/orders/{alert.order_id}"
        if alert.alert_type in {"order_review_required", "order_blocked", "automation_blocked"}:
            return "Resolver", f"/orders/{alert.order_id}"
        return "Abrir pedido", f"/orders/{alert.order_id}"
    if alert.inbound_message_id:
        return "Abrir entrada", "/pedidos?kind=emails"
    return "Revisar", "/alerts"


def serialize_alert(alert: Alert) -> dict:
    action_label, action_href = alert_default_action(alert)
    related_entity_type = "pedido" if alert.order_id else "entrada" if alert.inbound_message_id else "sistema"
    related_entity_id = alert.order_id or alert.inbound_message_id or alert.id
    return {
        "id": alert.id,
        "title": alert.title,
        "message": alert.message,
        "severity": alert.severity,
        "severity_label": alert_severity_label(alert.severity),
        "status": alert.status,
        "status_label": alert_status_label(alert.status),
        "type": alert.alert_type,
        "type_label": alert_type_label(alert.alert_type),
        "entity_type": related_entity_type,
        "entity_id": related_entity_id,
        "created_at": alert.created_at,
        "created_label": alert.created_at.strftime("%d/%m %H:%M") if alert.created_at else "",
        "action_label": action_label,
        "action_href": action_href,
        "secondary_label": "Marcar vista" if alert.status == "open" else "Reabrir" if alert.status in {"resolved", "ignored"} else "Resolver",
        "secondary_action": "mark-read" if alert.status == "open" else "reopen" if alert.status in {"resolved", "ignored"} else "resolve",
        "resolved_at": alert.resolved_at,
        "is_active": alert_is_active(alert),
    }


def build_alert_center_context(db: Session, company_id: int, limit: int = 10) -> dict:
    active_alerts = db.scalars(
        select(Alert)
        .where(Alert.company_id == company_id, Alert.status.notin_(["resolved", "ignored"]))
        .order_by(Alert.created_at.desc())
    ).all()
    all_alerts = db.scalars(select(Alert).where(Alert.company_id == company_id)).all()
    return {
        "total": len(active_alerts),
        "critical": len([a for a in active_alerts if a.severity == "critical"]),
        "high": len([a for a in active_alerts if a.severity == "high"]),
        "medium": len([a for a in active_alerts if a.severity == "medium"]),
        "low": len([a for a in active_alerts if a.severity == "low"]),
        "info": len([a for a in active_alerts if a.severity == "info"]),
        "has_critical": any(a.severity == "critical" for a in active_alerts),
        "recent": [serialize_alert(alert) for alert in active_alerts[:limit]],
    }


def _get_alert_for_user(db: Session, user: TenantUser, alert_id: int) -> Alert | None:
    return db.scalar(select(Alert).where(Alert.id == alert_id, Alert.company_id == user.company_id))


def _redirect_alert(alert: Alert | None) -> RedirectResponse:
    if not alert:
        return RedirectResponse("/alerts", status_code=303)
    if alert.order_id:
        return RedirectResponse(f"/orders/{alert.order_id}", status_code=303)
    if alert.inbound_message_id:
        return RedirectResponse("/pedidos?kind=emails", status_code=303)
    return RedirectResponse("/alerts", status_code=303)


def _alert_action_response(request: Request, db: Session, user: TenantUser):
    if "application/json" in (request.headers.get("accept") or "") or request.headers.get("x-requested-with") == "fetch":
        return JSONResponse(jsonable_encoder(build_alert_center_context(db, user.company_id, limit=10)))
    return RedirectResponse("/alerts", status_code=303)


@router.get("/alerts")
def alerts_page(
    request: Request,
    status: str = "all",
    severity: str = "all",
    search: str = "",
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    stmt = select(Alert).where(Alert.company_id == user.company_id)
    if status != "all":
        stmt = stmt.where(Alert.status == status)
    if severity != "all":
        stmt = stmt.where(Alert.severity == severity)
    if search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(or_(Alert.title.ilike(term), Alert.message.ilike(term), Alert.alert_type.ilike(term)))
    stmt = stmt.order_by(Alert.created_at.desc())
    page, page_size = normalize_page(page, page_size)
    total_items = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    total_pages = (total_items + page_size - 1) // page_size if total_items else 0
    start_index = (page - 1) * page_size
    alerts = db.scalars(stmt.offset(start_index).limit(page_size)).all()
    summary = build_alert_center_context(db, user.company_id, limit=12)
    return templates.TemplateResponse(
        "alerts/list.html",
        {
            "request": request,
            "user": user,
            "summary": summary,
            "alerts": [serialize_alert(a) for a in alerts],
            "filters": {"status": status, "severity": severity, "search": search},
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_items": total_items,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1,
                "start_item": start_index + 1 if total_items else 0,
                "end_item": min(start_index + page_size, total_items),
                "allowed_page_sizes": (25, 50, 100),
            },
            "active_channels": active_channels_for_company(db, user.company_id),
        },
    )


@router.get("/alerts/summary")
def alerts_summary(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if get_settings().app_slug.strip().lower() == "kibak" and db.get_bind().dialect.name == "postgresql":
        return JSONResponse(
            {
                "total": 0,
                "critical": 0,
                "high": 0,
                "medium": 0,
                "low": 0,
                "info": 0,
                "has_critical": False,
                "recent": [],
            }
        )
    return JSONResponse(jsonable_encoder(build_alert_center_context(db, user.company_id, limit=10)))


@router.post("/alerts/{alert_id}/mark-read")
def alert_mark_read(alert_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    alert = _get_alert_for_user(db, user, alert_id)
    if alert and alert.status == "open":
        alert.status = "seen"
        db.commit()
    return _alert_action_response(request, db, user)


@router.post("/alerts/{alert_id}/resolve")
def alert_resolve(alert_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    alert = _get_alert_for_user(db, user, alert_id)
    if alert:
        alert.status = "resolved"
        alert.resolved_at = utcnow()
        db.commit()
    return _alert_action_response(request, db, user)


@router.post("/alerts/{alert_id}/ignore")
def alert_ignore(alert_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    alert = _get_alert_for_user(db, user, alert_id)
    if alert:
        alert.status = "ignored"
        db.commit()
    return _alert_action_response(request, db, user)


@router.post("/alerts/{alert_id}/reopen")
def alert_reopen(alert_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    alert = _get_alert_for_user(db, user, alert_id)
    if alert:
        alert.status = "open"
        alert.resolved_at = None
        db.commit()
    return _alert_action_response(request, db, user)


@router.post("/alerts/{alert_id}/action")
def alert_action(alert_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    alert = _get_alert_for_user(db, user, alert_id)
    if alert and alert.status == "open":
        alert.status = "seen"
        db.commit()
    return _redirect_alert(alert)
