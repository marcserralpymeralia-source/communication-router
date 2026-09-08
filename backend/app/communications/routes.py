from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.communications.service import WORKBENCH_FILTERS, get_communication, list_communications, recipient_values, serialize_communication
from app.core.templating import templates
from app.db.models import Department, Mailbox, RoutingAction, RoutingCorrection, RoutingDecision, User
from app.master.service import TenantUser
from app.routing.auto import enqueue_forwarding_for_decision
from app.routing.forwarding import current_forward_action, serialize_forward_action
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

WORKBENCH_FILTER_OPTIONS = (
    ("all", "Todas"),
    ("pending_review", "Pendientes de revisión"),
    ("automatic", "Automáticas"),
    ("reviewed", "Revisadas"),
    ("unclassified", "Sin clasificar"),
    ("error", "Error"),
)
WORKBENCH_STATUS_LABELS = {
    "pending_review": "Pendiente de revisión",
    "automatic": "Automática",
    "reviewed": "Revisada",
    "corrected": "Corregida",
    "unclassified": "Sin clasificar",
    "error": "Error",
}
FORWARDING_STATUS_LABELS = {
    "pending": "Pendiente",
    "processing": "Enviando",
    "sent": "Enviado",
    "failed": "Error de envío",
    "cancelled": "Cancelado",
    "simulated": "Simulada: no enviada",
}


def _workbench_status(communication, decision, action) -> tuple[str, str, str]:  # noqa: ANN001
    if (
        communication.processing_status == "error"
        or communication.routing_status == "routing_error"
        or (action is not None and action.status == "failed")
    ):
        return "error", WORKBENCH_STATUS_LABELS["error"], "status-error"
    if action is not None and action.status == "simulated":
        return "reviewed", "Simulada: no enviada", "status-pending"
    if decision is None:
        return "unclassified", WORKBENCH_STATUS_LABELS["unclassified"], "status-doubtful"
    if decision.status == "pending_review":
        return "pending_review", WORKBENCH_STATUS_LABELS["pending_review"], "status-pending"
    if decision.status == "routed" and decision.source == "agent" and not decision.requires_review:
        return "automatic", WORKBENCH_STATUS_LABELS["automatic"], "status-exported"
    if decision.status == "corrected":
        return "reviewed", WORKBENCH_STATUS_LABELS["corrected"], "status-confirmed"
    if decision.status == "confirmed":
        return "reviewed", WORKBENCH_STATUS_LABELS["reviewed"], "status-confirmed"
    return "reviewed", WORKBENCH_STATUS_LABELS["reviewed"], "status-confirmed"


def _workbench_rows(db: Session, company_id: int, items: list) -> list[dict]:  # noqa: ANN001
    if not items:
        return []
    communication_ids = [item.id for item in items]
    mailboxes = {
        mailbox.id: mailbox
        for mailbox in db.scalars(
            select(Mailbox).where(
                Mailbox.company_id == company_id,
                Mailbox.id.in_({item.mailbox_id for item in items}),
            )
        ).all()
    }
    decisions = db.scalars(
        select(RoutingDecision)
        .where(
            RoutingDecision.company_id == company_id,
            RoutingDecision.communication_id.in_(communication_ids),
        )
        .order_by(RoutingDecision.analysis_number.desc(), RoutingDecision.id.desc())
    ).all()
    decisions_by_communication: dict[int, list] = {}
    for decision in decisions:
        decisions_by_communication.setdefault(decision.communication_id, []).append(decision)
    action_rows = db.scalars(
        select(RoutingAction)
        .where(
            RoutingAction.company_id == company_id,
            RoutingAction.communication_id.in_(communication_ids),
        )
        .order_by(RoutingAction.id.desc())
    ).all()
    actions_by_communication: dict[int, RoutingAction] = {}
    for action in action_rows:
        actions_by_communication.setdefault(action.communication_id, action)
    department_ids = {
        department_id
        for decision in decisions
        for department_id in (
            decision.department_id,
            decision.final_department_id,
            decision.alternative_department_id,
        )
        if department_id is not None
    }
    departments = {
        department.id: department
        for department in db.scalars(
            select(Department).where(
                Department.company_id == company_id,
                Department.id.in_(department_ids or {-1}),
            )
        ).all()
    }
    rows = []
    for communication in items:
        communication_decisions = decisions_by_communication.get(communication.id, [])
        decision = next((item for item in communication_decisions if item.status != "superseded"), None)
        action = actions_by_communication.get(communication.id)
        status_key, status_label, status_class = _workbench_status(communication, decision, action)
        rows.append(
            {
                "communication": communication,
                "mailbox": mailboxes.get(communication.mailbox_id),
                "decision": decision,
                "proposed_department": departments.get(decision.department_id) if decision and decision.department_id else None,
                "alternative_department": departments.get(decision.alternative_department_id) if decision and decision.alternative_department_id else None,
                "final_department": departments.get(decision.final_department_id) if decision and decision.final_department_id else None,
                "forward_action": action,
                "status_key": status_key,
                "status_label": status_label,
                "status_class": status_class,
                "confidence_label": f"{decision.confidence:.0%}" if decision else "—",
                "decisions": communication_decisions,
                "departments": departments,
            }
        )
    return rows


def _timeline(row: dict, corrections: list[RoutingCorrection], users: dict[int, User]) -> list[dict]:
    communication = row["communication"]
    events = []
    received_at = communication.received_at or communication.created_at
    if received_at:
        events.append({"at": received_at, "label": "Comunicación recibida", "detail": communication.sender_email or "Entrada registrada", "tone": "neutral"})
    for decision in sorted(row["decisions"], key=lambda item: (item.created_at or communication.created_at, item.id)):
        department = row["departments"].get(decision.department_id) if decision.department_id else None
        department_name = department.name if department else "Sin departamento"
        label = "IA propone destino" if decision.source == "agent" else "Análisis registrado"
        detail = f"{department_name} · {decision.confidence:.0%} · {decision.category}"
        if decision.status == "superseded":
            detail += " · análisis anterior"
        events.append({"at": decision.created_at, "label": label, "detail": detail, "tone": "pending" if decision.requires_review else "positive"})
    for correction in corrections:
        reviewer = users.get(correction.corrected_by_user_id)
        events.append(
            {
                "at": correction.created_at,
                "label": "Departamento cambiado",
                "detail": f"{reviewer.name if reviewer else 'Usuario'} · {correction.reason}",
                "tone": "positive",
            }
        )
    action = row["forward_action"]
    if action is not None:
        if action.created_at:
            label = "Derivada automáticamente" if action.source == "auto" and action.status == "sent" else "Acción simulada" if action.status == "simulated" else "Derivación solicitada"
            events.append({"at": action.created_at, "label": label, "detail": FORWARDING_STATUS_LABELS.get(action.status, action.status), "tone": "positive" if action.status in {"sent", "simulated"} else "pending"})
        if action.completed_at and action.status == "failed":
            events.append({"at": action.completed_at, "label": "Error en la derivación", "detail": "Requiere revisión", "tone": "error"})
    return sorted((event for event in events if event["at"] is not None), key=lambda event: event["at"])


@router.get("")
def communications_list(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str = Query("all"),
    q: str = Query("", max_length=120),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    items = list_communications(
        db,
        user.company_id,
        limit=limit,
        offset=offset,
        workbench_filter=status if status in WORKBENCH_FILTERS else "all",
        search=q,
    )
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
    q: str = Query("", max_length=120),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    status_value = status if isinstance(status, str) else "all"
    search_value = q if isinstance(q, str) else ""
    selected_status = status_value if status_value in WORKBENCH_FILTERS else "all"
    items = list_communications(
        db,
        user.company_id,
        limit=100,
        workbench_filter=selected_status,
        search=search_value,
    )
    counts = {
        key: len(list_communications(db, user.company_id, limit=100, workbench_filter=key, search=search_value))
        for key, _label in WORKBENCH_FILTER_OPTIONS
        if key != "all"
    }
    counts["all"] = len(list_communications(db, user.company_id, limit=100, workbench_filter="all", search=search_value))
    return templates.TemplateResponse(
        "communications/workbench.html",
        {
            "request": request,
            "user": user,
            "title": "Comunicaciones",
            "items": _workbench_rows(db, user.company_id, items),
            "counts": counts,
            "selected_status": selected_status,
            "search": search_value,
            "filter_options": WORKBENCH_FILTER_OPTIONS,
            "forwarding_status_labels": FORWARDING_STATUS_LABELS,
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
    row = _workbench_rows(db, user.company_id, [communication])[0]
    corrections = db.scalars(
        select(RoutingCorrection)
        .where(
            RoutingCorrection.company_id == user.company_id,
            RoutingCorrection.communication_id == communication_id,
        )
        .order_by(RoutingCorrection.created_at.asc(), RoutingCorrection.id.asc())
    ).all()
    reviewer_ids = {item.corrected_by_user_id for item in corrections}
    reviewers = {
        reviewer.id: reviewer
        for reviewer in db.scalars(select(User).where(User.company_id == user.company_id, User.id.in_(reviewer_ids or {-1}))).all()
    }
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
            "recipients": {
                "to": recipient_values(communication.to_recipients),
                "cc": recipient_values(communication.cc_recipients),
                "bcc": recipient_values(communication.bcc_recipients),
            },
            "row": row,
            "decision": row["decision"],
            "history": row["decisions"],
            "timeline": _timeline(row, corrections, reviewers),
            "corrections": corrections,
            "departments": departments,
            "forward_action": row["forward_action"],
            "forwarding_status_labels": FORWARDING_STATUS_LABELS,
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
        enqueue_forwarding_for_decision(
            db,
            company_id=user.company_id,
            decision=decision,
            triggered_by_user_id=user.id,
            source="human",
            human_confirmed=True,
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
        enqueue_forwarding_for_decision(
            db,
            company_id=user.company_id,
            decision=decision,
            triggered_by_user_id=user.id,
            source="human",
            human_confirmed=True,
        )
        db.commit()
    except RoutingValidationError as exc:
        db.rollback()
        return _routing_redirect(request, communication_id, error=str(exc))
    return _routing_redirect(request, communication_id) if "application/json" not in (request.headers.get("accept") or "") else JSONResponse(
        {"ok": True, "decision": serialize_routing_decision(decision)}
    )


@router.post("/{communication_id}/forward")
def forward_communication_action(
    communication_id: int,
    request: Request,
    decision_id: int | None = Form(None),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    if user.role.name not in {"Administrador", "Supervisor"}:
        return _routing_redirect(request, communication_id, error="No tienes permiso para reenviar comunicaciones.")
    decision = current_routing_decision(db, user.company_id, communication_id)
    if decision is None or (decision_id is not None and decision.id != decision_id):
        return _routing_redirect(request, communication_id, error="No existe una decisión final válida para reenviar.")
    if decision.final_department_id is None:
        return _routing_redirect(request, communication_id, error="La comunicación todavía no tiene departamento final.")
    try:
        job = enqueue_forwarding_for_decision(
            db,
            company_id=user.company_id,
            decision=decision,
            triggered_by_user_id=user.id,
            source="human",
            human_confirmed=True,
        )
        action = current_forward_action(db, user.company_id, communication_id)
        db.commit()
    except RoutingValidationError as exc:
        db.rollback()
        return _routing_redirect(request, communication_id, error=str(exc))
    payload = {
        "ok": True,
        "action_id": action.id if action else None,
        "status": action.status if action else "blocked",
        "job_id": job.id if job else None,
    }
    return _routing_redirect(request, communication_id) if "application/json" not in (request.headers.get("accept") or "") else JSONResponse(payload)


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
            "forward_action": serialize_forward_action(current_forward_action(db, user.company_id, communication_id)),
            "decision_history": [
                serialize_routing_decision(item)
                for item in list_routing_decisions(db, user.company_id, communication_id)
            ],
        }
    )
