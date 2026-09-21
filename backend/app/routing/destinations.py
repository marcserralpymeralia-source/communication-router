from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Department, RoutingDecision, RoutingDecisionDestination


PRIMARY_ROLE_OPTIONS = frozenset({"operational", "responsible", "accountable"})
ADDITIONAL_DESTINATION_ROLES = frozenset(
    {"operational", "responsible", "accountable", "consulted", "informed"}
)
DESTINATION_ROLE_LABELS = {
    "primary": "destino principal",
    "operational": "destino operativo",
    "responsible": "responsable",
    "accountable": "accountable",
    "consulted": "consultado",
    "informed": "informado",
}


def normalize_destination_specs(items: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize additional destinations without collapsing runner-up semantics."""

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for item in items or ():
        department_id = item.get("department_id")
        role = str(item.get("role") or "operational").strip().lower()
        position = item.get("position", len(normalized) + 2)
        if not isinstance(department_id, int) or isinstance(department_id, bool):
            raise ValueError("additional destination department_id must be an integer")
        if role not in ADDITIONAL_DESTINATION_ROLES:
            raise ValueError(f"Unsupported routing destination role: {role}")
        if not isinstance(position, int) or isinstance(position, bool) or position < 2:
            raise ValueError("additional destination position must be an integer >= 2")
        key = (department_id, role)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"department_id": department_id, "role": role, "position": position})
    return normalized


def validate_destination_specs(
    db: Session,
    company_id: int,
    primary_department_id: int | None,
    runner_up_department_id: int | None,
    items: Iterable[dict[str, Any]] | None,
    *,
    primary_role: str = "operational",
) -> list[dict[str, Any]]:
    normalized = normalize_destination_specs(items)
    if normalized and primary_department_id is None:
        raise ValueError("Additional destinations require a primary department")
    active_ids = set(
        db.scalars(
            select(Department.id).where(
                Department.company_id == company_id,
                Department.active.is_(True),
            )
        ).all()
    )
    if primary_role not in PRIMARY_ROLE_OPTIONS:
        raise ValueError(f"Unsupported primary routing role: {primary_role}")
    if primary_department_id is not None and primary_department_id not in active_ids:
        raise ValueError("Primary department does not belong to an active department in this tenant")
    if runner_up_department_id is not None and runner_up_department_id not in active_ids:
        raise ValueError("Runner-up department does not belong to an active department in this tenant")
    seen_departments: set[int] = set()
    for item in normalized:
        department_id = item["department_id"]
        if department_id not in active_ids:
            raise ValueError("Additional destination does not belong to an active department in this tenant")
        if department_id == primary_department_id:
            raise ValueError("Additional destination cannot duplicate the primary department")
        if department_id == runner_up_department_id:
            raise ValueError("A runner-up cannot also be an additional destination")
        if department_id in seen_departments:
            raise ValueError("A department cannot appear more than once as an additional destination")
        seen_departments.add(department_id)
    return normalized


def persist_decision_destinations(
    db: Session,
    company_id: int,
    decision: RoutingDecision,
    *,
    primary_department_id: int | None,
    primary_role: str = "operational",
    additional_destinations: Iterable[dict[str, Any]] | None = None,
) -> list[RoutingDecisionDestination]:
    specs = validate_destination_specs(
        db,
        company_id,
        primary_department_id,
        decision.runner_up_department_id,
        additional_destinations,
        primary_role=primary_role,
    )
    existing = list(
        db.scalars(
            select(RoutingDecisionDestination).where(
                RoutingDecisionDestination.company_id == company_id,
                RoutingDecisionDestination.routing_decision_id == decision.id,
            )
        )
    )
    for destination in existing:
        db.delete(destination)
    rows: list[RoutingDecisionDestination] = []
    if primary_department_id is not None:
        rows.append(
            RoutingDecisionDestination(
                company_id=company_id,
                routing_decision_id=decision.id,
                department_id=primary_department_id,
                role=primary_role,
                position=1,
            )
        )
    rows.extend(
        RoutingDecisionDestination(
            company_id=company_id,
            routing_decision_id=decision.id,
            department_id=item["department_id"],
            role=item["role"],
            position=item["position"],
        )
        for item in specs
    )
    db.add_all(rows)
    db.flush()
    return rows


def update_primary_destination(
    db: Session,
    company_id: int,
    decision: RoutingDecision,
    department_id: int,
) -> RoutingDecisionDestination:
    validate_destination_specs(db, company_id, department_id, decision.runner_up_department_id, None)
    primary = db.scalar(
        select(RoutingDecisionDestination).where(
            RoutingDecisionDestination.company_id == company_id,
            RoutingDecisionDestination.routing_decision_id == decision.id,
            RoutingDecisionDestination.position == 1,
        )
    )
    if primary is None:
        primary = RoutingDecisionDestination(
            company_id=company_id,
            routing_decision_id=decision.id,
            department_id=department_id,
            role="operational",
            position=1,
        )
        db.add(primary)
    else:
        primary.department_id = department_id
    db.flush()
    return primary


def load_decision_destinations(
    db: Session,
    company_id: int,
    decision_ids: Iterable[int],
) -> dict[int, list[RoutingDecisionDestination]]:
    ids = set(decision_ids)
    if not ids:
        return {}
    rows = db.scalars(
        select(RoutingDecisionDestination)
        .where(
            RoutingDecisionDestination.company_id == company_id,
            RoutingDecisionDestination.routing_decision_id.in_(ids),
        )
        .order_by(RoutingDecisionDestination.position, RoutingDecisionDestination.id)
    ).all()
    result: dict[int, list[RoutingDecisionDestination]] = {}
    for row in rows:
        result.setdefault(row.routing_decision_id, []).append(row)
    return result


def serialize_destinations(rows: Iterable[RoutingDecisionDestination]) -> list[dict[str, Any]]:
    return [
        {
            "department_id": row.department_id,
            "role": row.role,
            "position": row.position,
        }
        for row in rows
    ]
