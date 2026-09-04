from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Department, DepartmentKnowledge, DepartmentMember, RaciAssignment, User


KNOWLEDGE_TYPES = ("responsibilities", "exclusions", "examples", "exceptions", "guidelines")
_KNOWLEDGE_TYPE_ALIASES = {
    "responsibility": "responsibilities",
    "responsibilities": "responsibilities",
    "exclusion": "exclusions",
    "exclusions": "exclusions",
    "example": "examples",
    "examples": "examples",
    "exception": "exceptions",
    "exceptions": "exceptions",
    "guideline": "guidelines",
    "guidelines": "guidelines",
}
_KNOWLEDGE_TYPE_CANONICAL = {
    "responsibility": "responsibility",
    "responsibilities": "responsibility",
    "exclusion": "exclusion",
    "exclusions": "exclusion",
    "example": "example",
    "examples": "example",
    "exception": "exception",
    "exceptions": "exception",
    "guideline": "guideline",
    "guidelines": "guideline",
}
_DEPARTMENT_FIELDS = {"name", "description", "destination_email", "active"}
_KNOWLEDGE_FIELDS = {"title", "content", "knowledge_type", "active"}
_MEMBER_FIELDS = {"user_id", "role", "active"}
_RACI_FIELDS = {"user_id", "scope", "raci_role", "active"}


class DepartmentInUseError(ValueError):
    """Raised when a department still owns knowledge, members, or RACI rows."""


def _commit(db: Session, commit: bool) -> None:
    if commit:
        db.commit()


def _department_query(company_id: int, department_id: int):
    return select(Department).where(Department.company_id == company_id, Department.id == department_id)


def get_department(db: Session, company_id: int, department_id: int) -> Department | None:
    return db.scalar(_department_query(company_id, department_id))


def list_departments(db: Session, company_id: int, *, active_only: bool = False) -> list[Department]:
    statement = select(Department).where(Department.company_id == company_id)
    if active_only:
        statement = statement.where(Department.active.is_(True))
    return list(db.scalars(statement.order_by(Department.name, Department.id)))


def create_department(
    db: Session,
    company_id: int,
    *,
    name: str,
    description: str | None = None,
    destination_email: str | None = None,
    active: bool = True,
    commit: bool = True,
) -> Department:
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("Department name cannot be empty")
    department = Department(
        company_id=company_id,
        name=normalized_name,
        description=description,
        destination_email=destination_email,
        active=active,
    )
    db.add(department)
    db.flush()
    _commit(db, commit)
    return department


def update_department(
    db: Session,
    company_id: int,
    department_id: int,
    *,
    commit: bool = True,
    **changes: Any,
) -> Department | None:
    department = get_department(db, company_id, department_id)
    if department is None:
        return None
    unknown = set(changes) - _DEPARTMENT_FIELDS
    if unknown:
        raise ValueError(f"Unsupported department fields: {', '.join(sorted(unknown))}")
    if "name" in changes:
        changes["name"] = str(changes["name"]).strip()
        if not changes["name"]:
            raise ValueError("Department name cannot be empty")
    for field, value in changes.items():
        setattr(department, field, value)
    db.flush()
    _commit(db, commit)
    return department


def set_department_active(
    db: Session,
    company_id: int,
    department_id: int,
    active: bool,
    *,
    commit: bool = True,
) -> Department | None:
    return update_department(db, company_id, department_id, active=active, commit=commit)


def activate_department(db: Session, company_id: int, department_id: int, *, commit: bool = True) -> Department | None:
    return set_department_active(db, company_id, department_id, True, commit=commit)


def deactivate_department(db: Session, company_id: int, department_id: int, *, commit: bool = True) -> Department | None:
    return set_department_active(db, company_id, department_id, False, commit=commit)


def _department_has_children(db: Session, department_id: int) -> bool:
    for model in (DepartmentKnowledge, DepartmentMember, RaciAssignment):
        if db.scalar(select(model.id).where(model.department_id == department_id).limit(1)) is not None:
            return True
    return False


def delete_department_safely(
    db: Session,
    company_id: int,
    department_id: int,
    *,
    commit: bool = True,
) -> bool | None:
    department = get_department(db, company_id, department_id)
    if department is None:
        return None
    if _department_has_children(db, department.id):
        raise DepartmentInUseError("Department has knowledge, members, or RACI assignments")
    db.delete(department)
    _commit(db, commit)
    return True


def _normalize_knowledge_type(value: str) -> str:
    normalized = value.strip().lower()
    try:
        return _KNOWLEDGE_TYPE_CANONICAL[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported knowledge type: {value}") from exc


def list_department_knowledge(
    db: Session,
    company_id: int,
    department_id: int,
    *,
    active_only: bool = False,
) -> list[DepartmentKnowledge]:
    statement = (
        select(DepartmentKnowledge)
        .join(Department, Department.id == DepartmentKnowledge.department_id)
        .where(Department.company_id == company_id, Department.id == department_id)
    )
    if active_only:
        statement = statement.where(DepartmentKnowledge.active.is_(True))
    return list(db.scalars(statement.order_by(DepartmentKnowledge.knowledge_type, DepartmentKnowledge.title, DepartmentKnowledge.id)))


def _get_knowledge(db: Session, company_id: int, knowledge_id: int) -> DepartmentKnowledge | None:
    return db.scalar(
        select(DepartmentKnowledge)
        .join(Department, Department.id == DepartmentKnowledge.department_id)
        .where(DepartmentKnowledge.id == knowledge_id, Department.company_id == company_id)
    )


def create_department_knowledge(
    db: Session,
    company_id: int,
    department_id: int,
    *,
    title: str,
    content: str,
    knowledge_type: str,
    active: bool = True,
    commit: bool = True,
) -> DepartmentKnowledge:
    if get_department(db, company_id, department_id) is None:
        raise ValueError("Department not found for tenant")
    item = DepartmentKnowledge(
        department_id=department_id,
        title=title.strip(),
        content=content,
        knowledge_type=_normalize_knowledge_type(knowledge_type),
        active=active,
    )
    db.add(item)
    db.flush()
    _commit(db, commit)
    return item


def update_department_knowledge(
    db: Session,
    company_id: int,
    knowledge_id: int,
    *,
    commit: bool = True,
    **changes: Any,
) -> DepartmentKnowledge | None:
    item = _get_knowledge(db, company_id, knowledge_id)
    if item is None:
        return None
    unknown = set(changes) - _KNOWLEDGE_FIELDS
    if unknown:
        raise ValueError(f"Unsupported knowledge fields: {', '.join(sorted(unknown))}")
    if "knowledge_type" in changes:
        changes["knowledge_type"] = _normalize_knowledge_type(changes["knowledge_type"])
    if "title" in changes:
        changes["title"] = str(changes["title"]).strip()
    for field, value in changes.items():
        setattr(item, field, value)
    db.flush()
    _commit(db, commit)
    return item


def set_department_knowledge_active(
    db: Session,
    company_id: int,
    knowledge_id: int,
    active: bool,
    *,
    commit: bool = True,
) -> DepartmentKnowledge | None:
    return update_department_knowledge(db, company_id, knowledge_id, active=active, commit=commit)


def delete_department_knowledge(db: Session, company_id: int, knowledge_id: int, *, commit: bool = True) -> bool | None:
    item = _get_knowledge(db, company_id, knowledge_id)
    if item is None:
        return None
    db.delete(item)
    _commit(db, commit)
    return True


def list_department_members(db: Session, company_id: int, department_id: int, *, active_only: bool = False) -> list[DepartmentMember]:
    statement = (
        select(DepartmentMember)
        .join(Department, Department.id == DepartmentMember.department_id)
        .join(User, User.id == DepartmentMember.user_id)
        .options(selectinload(DepartmentMember.user))
        .where(Department.company_id == company_id, Department.id == department_id, User.company_id == company_id)
    )
    if active_only:
        statement = statement.where(DepartmentMember.active.is_(True))
    return list(db.scalars(statement.order_by(DepartmentMember.id)))


def _get_member(db: Session, company_id: int, member_id: int) -> DepartmentMember | None:
    return db.scalar(
        select(DepartmentMember)
        .join(Department, Department.id == DepartmentMember.department_id)
        .join(User, User.id == DepartmentMember.user_id)
        .options(selectinload(DepartmentMember.user))
        .where(DepartmentMember.id == member_id, Department.company_id == company_id, User.company_id == company_id)
    )


def add_department_member(
    db: Session,
    company_id: int,
    department_id: int,
    *,
    user_id: int,
    role: str | None = None,
    active: bool = True,
    commit: bool = True,
) -> DepartmentMember:
    if get_department(db, company_id, department_id) is None:
        raise ValueError("Department not found for tenant")
    if db.scalar(select(User.id).where(User.id == user_id, User.company_id == company_id)) is None:
        raise ValueError("User not found for tenant")
    member = DepartmentMember(
        company_id=company_id,
        department_id=department_id,
        user_id=user_id,
        role=role,
        active=active,
    )
    db.add(member)
    db.flush()
    _commit(db, commit)
    return member


def update_department_member(
    db: Session,
    company_id: int,
    member_id: int,
    *,
    commit: bool = True,
    **changes: Any,
) -> DepartmentMember | None:
    member = _get_member(db, company_id, member_id)
    if member is None:
        return None
    unknown = set(changes) - _MEMBER_FIELDS
    if unknown:
        raise ValueError(f"Unsupported member fields: {', '.join(sorted(unknown))}")
    if "user_id" in changes and db.scalar(select(User.id).where(User.id == changes["user_id"], User.company_id == company_id)) is None:
        raise ValueError("User not found for tenant")
    for field, value in changes.items():
        setattr(member, field, value)
    db.flush()
    _commit(db, commit)
    return member


def remove_department_member(db: Session, company_id: int, member_id: int, *, commit: bool = True) -> bool | None:
    member = _get_member(db, company_id, member_id)
    if member is None:
        return None
    db.delete(member)
    _commit(db, commit)
    return True


def list_raci_assignments(db: Session, company_id: int, department_id: int, *, active_only: bool = False) -> list[RaciAssignment]:
    statement = (
        select(RaciAssignment)
        .join(Department, Department.id == RaciAssignment.department_id)
        .outerjoin(User, User.id == RaciAssignment.user_id)
        .options(selectinload(RaciAssignment.user))
        .where(
            RaciAssignment.company_id == company_id,
            Department.company_id == company_id,
            Department.id == department_id,
            or_(RaciAssignment.user_id.is_(None), User.company_id == company_id),
        )
    )
    if active_only:
        statement = statement.where(RaciAssignment.active.is_(True))
    return list(db.scalars(statement.order_by(RaciAssignment.scope, RaciAssignment.raci_role, RaciAssignment.id)))


def _get_raci(db: Session, company_id: int, assignment_id: int) -> RaciAssignment | None:
    return db.scalar(
        select(RaciAssignment)
        .join(Department, Department.id == RaciAssignment.department_id)
        .outerjoin(User, User.id == RaciAssignment.user_id)
        .options(selectinload(RaciAssignment.user))
        .where(
            RaciAssignment.id == assignment_id,
            RaciAssignment.company_id == company_id,
            Department.company_id == company_id,
            or_(RaciAssignment.user_id.is_(None), User.company_id == company_id),
        )
    )


def create_raci_assignment(
    db: Session,
    company_id: int,
    department_id: int,
    *,
    raci_role: str,
    scope: str = "department",
    user_id: int | None = None,
    active: bool = True,
    commit: bool = True,
) -> RaciAssignment:
    if get_department(db, company_id, department_id) is None:
        raise ValueError("Department not found for tenant")
    if user_id is not None and db.scalar(select(User.id).where(User.id == user_id, User.company_id == company_id)) is None:
        raise ValueError("User not found for tenant")
    assignment = RaciAssignment(
        company_id=company_id,
        department_id=department_id,
        user_id=user_id,
        scope=scope,
        raci_role=raci_role.strip().lower(),
        active=active,
    )
    db.add(assignment)
    db.flush()
    _commit(db, commit)
    return assignment


def update_raci_assignment(
    db: Session,
    company_id: int,
    assignment_id: int,
    *,
    commit: bool = True,
    **changes: Any,
) -> RaciAssignment | None:
    assignment = _get_raci(db, company_id, assignment_id)
    if assignment is None:
        return None
    unknown = set(changes) - _RACI_FIELDS
    if unknown:
        raise ValueError(f"Unsupported RACI fields: {', '.join(sorted(unknown))}")
    if "user_id" in changes and changes["user_id"] is not None and db.scalar(select(User.id).where(User.id == changes["user_id"], User.company_id == company_id)) is None:
        raise ValueError("User not found for tenant")
    if "raci_role" in changes:
        changes["raci_role"] = str(changes["raci_role"]).strip().lower()
    for field, value in changes.items():
        setattr(assignment, field, value)
    db.flush()
    _commit(db, commit)
    return assignment


def delete_raci_assignment(db: Session, company_id: int, assignment_id: int, *, commit: bool = True) -> bool | None:
    assignment = _get_raci(db, company_id, assignment_id)
    if assignment is None:
        return None
    db.delete(assignment)
    _commit(db, commit)
    return True


def _knowledge_bucket(knowledge_type: str) -> str:
    return _KNOWLEDGE_TYPE_ALIASES.get(knowledge_type.strip().lower(), knowledge_type.strip().lower())


def _serialize_knowledge(item: DepartmentKnowledge) -> dict[str, Any]:
    canonical_type = _KNOWLEDGE_TYPE_CANONICAL.get(item.knowledge_type.strip().lower(), item.knowledge_type.strip().lower())
    return {
        "id": item.id,
        "knowledge_id": item.id,
        "type": canonical_type,
        "knowledge_type": canonical_type,
        "title": item.title,
        "content": item.content,
    }


def _serialize_member(member: DepartmentMember) -> dict[str, Any]:
    user = member.user
    return {
        "id": member.id,
        "member_id": member.id,
        "user_id": member.user_id,
        "name": user.name if user else None,
        "email": user.email if user else None,
        "role": member.role,
    }


def _serialize_raci(assignment: RaciAssignment) -> dict[str, Any]:
    user = assignment.user
    return {
        "id": assignment.id,
        "raci_id": assignment.id,
        "department_id": assignment.department_id,
        "user_id": assignment.user_id,
        "name": user.name if user else None,
        "email": user.email if user else None,
        "scope": assignment.scope,
        "role": assignment.raci_role,
        "raci_role": assignment.raci_role,
    }


def _build_context_with_db(db: Session, company_id: int) -> dict[str, list[dict[str, Any]]]:
    departments = []
    for department in list_departments(db, company_id, active_only=True):
        knowledge: dict[str, list[dict[str, Any]]] = {key: [] for key in KNOWLEDGE_TYPES}
        for item in list_department_knowledge(db, company_id, department.id, active_only=True):
            bucket = _knowledge_bucket(item.knowledge_type)
            knowledge.setdefault(bucket, []).append(_serialize_knowledge(item))
        members = [
            member
            for member in list_department_members(db, company_id, department.id, active_only=True)
            if member.user is not None and member.user.is_active
        ]
        departments.append(
            {
                "id": department.id,
                "department_id": department.id,
                "name": department.name,
                "description": department.description,
                "destination_email": department.destination_email,
                "knowledge": knowledge,
                "members": [_serialize_member(member) for member in members],
                "raci": [_serialize_raci(item) for item in list_raci_assignments(db, company_id, department.id, active_only=True)],
            }
        )
    return {"departments": departments}


def build_department_routing_context(
    company_id: int | Session,
    db: Session | int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the active, tenant-scoped payload consumed by a future RoutingAgent.

    The preferred call is ``build_department_routing_context(company_id, db)``.
    ``build_department_routing_context(db, company_id)`` is also accepted to
    match existing service conventions. The session is always explicit so this
    helper cannot accidentally open a database for the wrong tenant.
    """
    if isinstance(company_id, Session):
        if not isinstance(db, int):
            raise TypeError("company_id is required when the session is the first argument")
        session, tenant_id = company_id, db
    else:
        if not isinstance(db, Session):
            raise TypeError("An operational database session is required")
        session, tenant_id = db, company_id
    return _build_context_with_db(session, tenant_id)
