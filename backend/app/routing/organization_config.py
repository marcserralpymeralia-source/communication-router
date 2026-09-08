from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Department, DepartmentKnowledge, RaciAssignment


CONFIG_VERSION = "kibak.organization.v1"
KNOWLEDGE_TYPES = {"responsibility", "exclusion", "example", "exception", "guideline"}


class OrganizationConfigError(ValueError):
    pass


def export_organization_config(db: Session, company_id: int) -> dict[str, Any]:
    departments = list(db.scalars(select(Department).where(Department.company_id == company_id).order_by(Department.name, Department.id)))
    department_ids = [item.id for item in departments]
    knowledge = list(db.scalars(select(DepartmentKnowledge).where(DepartmentKnowledge.department_id.in_(department_ids or [-1])).order_by(DepartmentKnowledge.id)))
    raci = list(db.scalars(select(RaciAssignment).where(RaciAssignment.company_id == company_id).order_by(RaciAssignment.id)))
    return {
        "version": CONFIG_VERSION,
        "departments": [
            {"name": item.name, "description": item.description, "destination_email": item.destination_email, "active": item.active}
            for item in departments
        ],
        "knowledge": [
            {"department_name": next((department.name for department in departments if department.id == item.department_id), None), "title": item.title, "content": item.content, "knowledge_type": item.knowledge_type, "active": item.active}
            for item in knowledge
        ],
        "raci": [
            {"department_name": next((department.name for department in departments if department.id == item.department_id), None), "scope": item.scope, "raci_role": item.raci_role, "active": item.active}
            for item in raci
        ],
    }


def validate_organization_config(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("version") != CONFIG_VERSION:
        raise OrganizationConfigError(f"El archivo debe usar la versión {CONFIG_VERSION}.")
    for key in ("departments", "knowledge", "raci"):
        if not isinstance(payload.get(key), list):
            raise OrganizationConfigError(f"{key} debe ser una lista.")
    names: set[str] = set()
    for item in payload["departments"]:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise OrganizationConfigError("Cada departamento necesita un nombre.")
        name = str(item["name"]).strip()
        if name.lower() in names:
            raise OrganizationConfigError(f"Departamento duplicado: {name}.")
        names.add(name.lower())
        destination = item.get("destination_email")
        if destination and ("\r" in str(destination) or "\n" in str(destination)):
            raise OrganizationConfigError("destination_email contiene caracteres no válidos.")
    for item in [*payload["knowledge"], *payload["raci"]]:
        if not isinstance(item, dict) or str(item.get("department_name") or "").strip().lower() not in names:
            raise OrganizationConfigError("La referencia de departamento no es válida.")
    for item in payload["knowledge"]:
        knowledge_type = str(item.get("knowledge_type") or "").strip().lower()
        if knowledge_type not in KNOWLEDGE_TYPES:
            raise OrganizationConfigError(f"Tipo de knowledge no válido: {knowledge_type or 'vacío'}.")
    return payload


def preview_organization_config(db: Session, company_id: int, payload: Any) -> dict[str, Any]:
    validated = validate_organization_config(payload)
    existing = {item.name.strip().lower() for item in db.scalars(select(Department).where(Department.company_id == company_id))}
    incoming = {str(item["name"]).strip().lower() for item in validated["departments"]}
    return {
        "version": CONFIG_VERSION,
        "departments_to_create": len(incoming - existing),
        "departments_existing": len(incoming & existing),
        "knowledge_items": len(validated["knowledge"]),
        "raci_assignments": len(validated["raci"]),
        "destructive_changes": False,
    }


def apply_organization_config(db: Session, company_id: int, payload: Any) -> dict[str, Any]:
    validated = validate_organization_config(payload)
    departments = {item.name.strip().lower(): item for item in db.scalars(select(Department).where(Department.company_id == company_id))}
    created = 0
    for item in validated["departments"]:
        key = str(item["name"]).strip().lower()
        department = departments.get(key)
        if department is None:
            department = Department(company_id=company_id, name=str(item["name"]).strip())
            db.add(department)
            db.flush()
            departments[key] = department
            created += 1
        department.description = item.get("description")
        department.destination_email = item.get("destination_email")
        department.active = bool(item.get("active", True))
    for item in validated["knowledge"]:
        department = departments[str(item["department_name"]).strip().lower()]
        existing = db.scalar(select(DepartmentKnowledge).where(DepartmentKnowledge.department_id == department.id, DepartmentKnowledge.title == item["title"], DepartmentKnowledge.knowledge_type == item["knowledge_type"]))
        if existing is None:
            db.add(DepartmentKnowledge(department_id=department.id, title=item["title"], content=item["content"], knowledge_type=item["knowledge_type"], active=bool(item.get("active", True))))
    for item in validated["raci"]:
        department = departments[str(item["department_name"]).strip().lower()]
        existing = db.scalar(select(RaciAssignment).where(RaciAssignment.company_id == company_id, RaciAssignment.department_id == department.id, RaciAssignment.scope == item["scope"], RaciAssignment.raci_role == item["raci_role"], RaciAssignment.user_id.is_(None)))
        if existing is None:
            db.add(RaciAssignment(company_id=company_id, department_id=department.id, scope=item["scope"], raci_role=item["raci_role"], user_id=None, active=bool(item.get("active", True))))
    db.flush()
    return {"created_departments": created, "knowledge_items": len(validated["knowledge"]), "raci_assignments": len(validated["raci"]), "destructive_changes": False}
