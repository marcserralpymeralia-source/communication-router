from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Communication, Department, RoutingAction, RoutingDecision


STATUS_ORDER = ("automatic", "review", "reviewed", "unclassified", "error", "forwarded")
STATUS_LABELS = {
    "automatic": "Automática",
    "review": "Pendiente de revisión",
    "reviewed": "Revisada",
    "unclassified": "Sin clasificar",
    "error": "Error",
    "forwarded": "Reenviada",
}
STATUS_CLASSES = {key: f"kibak-status-{key.replace('_', '-')}" for key in STATUS_LABELS}


def _status_for(communication, decision, action):
    if action is not None and action.status == "sent":
        return "forwarded"
    if communication.routing_status == "routing_error":
        return "error"
    if communication.routing_status == "routed":
        return "reviewed" if decision is not None and decision.status in {"confirmed", "corrected"} else "automatic"
    if communication.routing_status in {"pending_review", "routing_queued", "routing_processing"}:
        return "review"
    return "unclassified"


def _latest_decisions(decisions):
    latest = {}
    for decision in decisions:
        current = latest.get(decision.communication_id)
        if current is None or (decision.analysis_number, decision.id) > (current.analysis_number, current.id):
            latest[decision.communication_id] = decision
    return latest


def _confidence_label(confidence):
    if confidence is None:
        return "--"
    value = confidence * 100 if confidence <= 1 else confidence
    return f"{value:.0f}%"


def _format_duration(seconds):
    if seconds is None or seconds < 0:
        return "--"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, remainder = divmod(minutes, 60)
    return f"{hours} h {remainder:02d} min" if remainder else f"{hours} h"


def _percentage(value, total):
    return round(value * 100 / total) if total else 0


def kibak_dashboard_summary(db: Session, company_id: int, recent_limit: int = 8) -> dict:
    communications = list(
        db.scalars(
            select(Communication)
            .where(Communication.company_id == company_id)
            .order_by(Communication.received_at.desc(), Communication.id.desc())
        )
    )
    decisions = list(
        db.scalars(
            select(RoutingDecision)
            .where(RoutingDecision.company_id == company_id)
            .order_by(RoutingDecision.analysis_number.desc(), RoutingDecision.id.desc())
        )
    )
    departments = {
        department.id: department.name
        for department in db.scalars(
            select(Department).where(Department.company_id == company_id, Department.active.is_(True))
        )
    }
    actions = list(
        db.scalars(select(RoutingAction).where(RoutingAction.company_id == company_id).order_by(RoutingAction.id.desc()))
    )

    latest = _latest_decisions(decisions)
    latest_actions = {}
    for action in actions:
        latest_actions.setdefault(action.communication_id, action)

    status_counts = defaultdict(int)
    department_counts = defaultdict(int)
    confidence_values = []
    routing_seconds = []
    recent = []
    for communication in communications:
        decision = latest.get(communication.id)
        action = latest_actions.get(communication.id)
        status_key = _status_for(communication, decision, action)
        status_counts[status_key] += 1
        department_id = (
            decision.final_department_id
            if decision and decision.final_department_id
            else decision.department_id
            if decision
            else None
        )
        department_name = departments.get(department_id, "Sin departamento")
        if decision and decision.confidence is not None:
            confidence_values.append(decision.confidence)
        if decision and communication.received_at and decision.created_at:
            elapsed = (decision.created_at - communication.received_at).total_seconds()
            if elapsed >= 0:
                routing_seconds.append(elapsed)
        if decision and department_id:
            department_counts[department_name] += 1
        if len(recent) < recent_limit:
            recent.append(
                {
                    "id": communication.id,
                    "received_at": communication.received_at or communication.created_at,
                    "sender": communication.sender_name or communication.sender_email or "Remitente desconocido",
                    "subject": communication.subject or "Sin asunto",
                    "department": department_name if decision and department_id else "Sin clasificar",
                    "confidence": _confidence_label(decision.confidence if decision else None),
                    "status_key": status_key,
                    "status_label": STATUS_LABELS[status_key],
                    "status_class": STATUS_CLASSES[status_key],
                    "href": f"/communications/workbench/{communication.id}",
                }
            )

    total = len(communications)
    automatic_count = status_counts["automatic"] + status_counts["forwarded"]
    status_distribution = [
        {
            "key": key,
            "label": STATUS_LABELS[key],
            "count": status_counts[key],
            "percentage": _percentage(status_counts[key], total),
            "class": STATUS_CLASSES[key],
        }
        for key in STATUS_ORDER
        if status_counts[key] or total == 0
    ]
    department_distribution = [
        {"name": name, "count": count, "percentage": _percentage(count, total)}
        for name, count in sorted(department_counts.items(), key=lambda item: (-item[1], item[0]))[:6]
    ]
    average_confidence = sum(confidence_values) / len(confidence_values) if confidence_values else None

    return {
        "has_data": bool(total),
        "kpis": [
            {"label": "Comunicaciones recibidas", "value": total, "meta": "En este buzón de trabajo"},
            {"label": "Derivadas automáticamente", "value": automatic_count, "meta": "Incluye comunicaciones reenviadas"},
            {"label": "Pendientes de revisión", "value": status_counts["review"], "meta": "Requieren una decisión"},
            {"label": "Sin clasificar", "value": status_counts["unclassified"], "meta": "Aún sin departamento"},
            {"label": "% automatizadas", "value": f"{_percentage(automatic_count, total)}%", "meta": "Sobre el total recibido"},
            {"label": "Confianza media", "value": _confidence_label(average_confidence), "meta": "De las decisiones disponibles"},
            {"label": "Tiempo medio de derivación", "value": _format_duration(sum(routing_seconds) / len(routing_seconds) if routing_seconds else None), "meta": "Cuando existe trazabilidad"},
        ],
        "status_distribution": status_distribution,
        "department_distribution": department_distribution,
        "recent": recent,
    }
