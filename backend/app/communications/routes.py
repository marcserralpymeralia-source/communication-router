from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.communications.service import get_communication, list_communications, serialize_communication
from app.core.templating import templates
from app.db.models import Department
from app.master.service import TenantUser
from app.routing.service import (
    RoutingValidationError,
    analyze_communication,
    configured_routing_runtime,
    confirm_routing_decision,
    correct_routing_decision,
    current_routing_decision,
    list_routing_decisions,
    serialize_routing_decision,
)
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/communications", tags=["communications"])


@router.get("")
def communications_list(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    items = list_communications(db, user.company_id, limit=limit, offset=offset)
    return JSONResponse(
        {
            "ok": True,
            "items": [serialize_communication(item) for item in items],
            "limit": limit,
            "offset": offset,
        }
    )


def _routing_redirect(request: Request, communication_id: int, *, error: str | None = None):
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse({"ok": error is None, "message": error} if error else {"ok": True})
    suffix = f"?error={quote_plus(error)}" if error else ""
    return RedirectResponse(f"/communications/workbench/{communication_id}{suffix}", status_code=303)


@router.get("/workbench")
def communications_workbench(
    request: Request,
    status: str = Query("all"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    selected_status = status if status in {"all", "unclassified", "pending_review", "routed"} else "all"
    items = list_communications(
        db,
        user.company_id,
        limit=100,
        routing_status=None if selected_status == "all" else selected_status,
    )
    counts = {
        key: len(list_communications(db, user.company_id, limit=100, routing_status=key))
        for key in ("unclassified", "pending_review", "routed")
    }
    counts["all"] = len(list_communications(db, user.company_id, limit=100))
    return templates.TemplateResponse(
        "communications/workbench.html",
        {
            "request": request,
            "user": user,
            "title": "Comunicaciones",
            "items": items,
            "counts": counts,
            "selected_status": selected_status,
        },
    )


@router.get("/workbench/{communication_id}")
def communications_workbench_detail(
    communication_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    communication = get_communication(db, user.company_id, communication_id)
    if communication is None:
        return JSONResponse({"ok": False, "message": "Comunicación no encontrada."}, status_code=404)
    departments = db.scalars(
        select(Department)
        .where(Department.company_id == user.company_id, Department.active.is_(True))
        .order_by(Department.name)
    ).all()
    return templates.TemplateResponse(
        "communications/detail.html",
        {
            "request": request,
            "user": user,
            "title": communication.subject or "Comunicación",
            "communication": communication,
            "decision": current_routing_decision(db, user.company_id, communication_id),
            "history": list_routing_decisions(db, user.company_id, communication_id),
            "departments": departments,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/{communication_id}/analyze")
def analyze_communication_action(
    communication_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    try:
        decision = analyze_communication(
            db,
            user.company_id,
            communication_id,
            configured_routing_runtime(
                db,
                user.company_id,
                user_id=user.id,
                communication_id=communication_id,
            ),
            user_id=user.id,
        )
        db.commit()
    except RoutingValidationError as exc:
        db.rollback()
        return _routing_redirect(request, communication_id, error=str(exc))
    return _routing_redirect(request, communication_id) if "application/json" not in (request.headers.get("accept") or "") else JSONResponse(
        {"ok": True, "decision": serialize_routing_decision(decision)}
    )


@router.post("/{communication_id}/confirm")
def confirm_communication_action(
    communication_id: int,
    request: Request,
    decision_id: int | None = Form(None),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    try:
        decision = confirm_routing_decision(
            db, user.company_id, communication_id, user.id, decision_id=decision_id
        )
        db.commit()
    except RoutingValidationError as exc:
        db.rollback()
        return _routing_redirect(request, communication_id, error=str(exc))
    return _routing_redirect(request, communication_id) if "application/json" not in (request.headers.get("accept") or "") else JSONResponse(
        {"ok": True, "decision": serialize_routing_decision(decision)}
    )


@router.post("/{communication_id}/correct")
def correct_communication_action(
    communication_id: int,
    request: Request,
    corrected_department_id: int = Form(...),
    reason: str = Form(...),
    corrected_category: str | None = Form(None),
    decision_id: int | None = Form(None),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    try:
        decision = correct_routing_decision(
            db,
            user.company_id,
            communication_id,
            user.id,
            corrected_department_id,
            reason=reason,
            corrected_category=corrected_category,
            decision_id=decision_id,
        )
        db.commit()
    except RoutingValidationError as exc:
        db.rollback()
        return _routing_redirect(request, communication_id, error=str(exc))
    return _routing_redirect(request, communication_id) if "application/json" not in (request.headers.get("accept") or "") else JSONResponse(
        {"ok": True, "decision": serialize_routing_decision(decision)}
    )


@router.get("/{communication_id}")
def communication_detail(
    communication_id: int,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    communication = get_communication(db, user.company_id, communication_id)
    if not communication:
        return JSONResponse({"ok": False, "message": "Comunicación no encontrada."}, status_code=404)
    return JSONResponse(
        {
            "ok": True,
            "communication": serialize_communication(communication, include_detail=True),
            "decision": serialize_routing_decision(current_routing_decision(db, user.company_id, communication_id)),
            "decision_history": [
                serialize_routing_decision(item)
                for item in list_routing_decisions(db, user.company_id, communication_id)
            ],
        }
    )
