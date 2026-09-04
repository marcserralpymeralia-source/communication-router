from __future__ import annotations

from email.utils import parseaddr
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.templating import templates
from app.db.models import User
from app.departments.service import (
    DepartmentInUseError,
    activate_department,
    add_department_member,
    create_department,
    create_department_knowledge,
    create_raci_assignment,
    deactivate_department,
    delete_department_knowledge,
    delete_department_safely,
    delete_raci_assignment,
    get_department,
    list_department_knowledge,
    list_department_members,
    list_departments,
    list_raci_assignments,
    remove_department_member,
    set_department_knowledge_active,
    update_department,
    update_department_knowledge,
    update_department_member,
    update_raci_assignment,
)
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/departments", tags=["departments"])

KNOWLEDGE_TYPES = (
    ("responsibility", "Responsabilidad"),
    ("exclusion", "Exclusión"),
    ("example", "Ejemplo"),
    ("exception", "Excepción"),
    ("guideline", "Directriz"),
)
RACI_ROLES = (
    ("responsible", "Responsible"),
    ("accountable", "Accountable"),
    ("consulted", "Consulted"),
    ("informed", "Informed"),
)


def _can_edit(user: TenantUser) -> bool:
    return user.role.name in {"Administrador", "Superadmin"}


def _is_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "") or "application/json" in (request.headers.get("content-type") or "")


def _response(request: Request, payload: dict, *, redirect: str = "/departments", status_code: int = 303):
    if _is_json(request):
        return JSONResponse(payload, status_code=status_code if status_code >= 400 else 200)
    return RedirectResponse(redirect, status_code=303)


async def _form_data(request: Request) -> dict[str, str]:
    if "application/json" in (request.headers.get("content-type") or ""):
        payload = await request.json()
        return {key: str(value) for key, value in payload.items()} if isinstance(payload, dict) else {}
    form = await request.form()
    return {key: str(value) for key, value in form.multi_items() if not hasattr(value, "filename")}


def _truthy(value: str | None, *, default: bool = False) -> bool:
    return str(value).lower() in {"1", "true", "on", "yes", "si", "sí"} if value is not None else default


def _valid_email(value: str | None) -> bool:
    if not value:
        return True
    address = parseaddr(value)[1].strip()
    return bool(address and "@" in address and "." in address.rsplit("@", 1)[-1])


def _redirect(path: str, *, message: str | None = None, error: str | None = None) -> str:
    query = {key: value for key, value in (("message", message), ("error", error)) if value}
    return f"{path}?{urlencode(query)}" if query else path


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, IntegrityError):
        return "Ya existe un registro con esos datos en este tenant."
    return str(exc) or "No se han podido guardar los cambios."


def _tenant_users(db: Session, company_id: int) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(User.company_id == company_id, User.is_active.is_(True))
            .order_by(User.name, User.email, User.id)
        )
    )


def _summary(db: Session, department) -> dict:
    knowledge = list_department_knowledge(db, department.company_id, department.id)
    members = list_department_members(db, department.company_id, department.id)
    return {
        "id": department.id,
        "name": department.name,
        "description": department.description,
        "destination_email": department.destination_email,
        "active": department.active,
        "knowledge_count": len(knowledge),
        "member_count": len(members),
    }


def _detail_context(request: Request, db: Session, user: TenantUser, department):
    knowledge = list_department_knowledge(db, user.company_id, department.id)
    members = list_department_members(db, user.company_id, department.id)
    raci = list_raci_assignments(db, user.company_id, department.id)
    member_user_ids = {member.user_id for member in members}
    available_users = [item for item in _tenant_users(db, user.company_id) if item.id not in member_user_ids]
    return {
        "request": request,
        "user": user,
        "title": department.name,
        "department": department,
        "knowledge": knowledge,
        "members": members,
        "raci": raci,
        "available_users": available_users,
        "all_users": _tenant_users(db, user.company_id),
        "knowledge_types": KNOWLEDGE_TYPES,
        "raci_roles": RACI_ROLES,
        "can_edit": _can_edit(user),
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    }


@router.get("")
def departments_page(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    departments = list_departments(db, user.company_id)
    items = [_summary(db, department) for department in departments]
    if _is_json(request):
        return JSONResponse({"ok": True, "items": items})
    return templates.TemplateResponse(
        "departments/index.html",
        {
            "request": request,
            "user": user,
            "title": "Departamentos",
            "departments": departments,
            "department_items": items,
            "can_edit": _can_edit(user),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("")
async def create_department_route(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    data = await _form_data(request)
    name = data.get("name", "").strip()
    destination_email = data.get("destination_email", "").strip() or None
    if not name:
        return _response(request, {"ok": False, "message": "El nombre del departamento es obligatorio."}, redirect=_redirect("/departments", error="El nombre del departamento es obligatorio."), status_code=400)
    if not _valid_email(destination_email):
        message = "Indica una dirección de destino válida, por ejemplo logistica@empresa.com."
        return _response(request, {"ok": False, "message": message}, redirect=_redirect("/departments", error=message), status_code=400)
    try:
        department = create_department(
            db,
            user.company_id,
            name=name,
            description=data.get("description", "").strip() or None,
            destination_email=destination_email,
            active=_truthy(data.get("active"), default=True),
        )
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        message = _friendly_error(exc)
        return _response(request, {"ok": False, "message": message}, redirect=_redirect("/departments", error=message), status_code=409 if isinstance(exc, IntegrityError) else 400)
    return _response(request, {"ok": True, "department": _summary(db, department)}, redirect=f"/departments/{department.id}")


@router.get("/{department_id}")
def department_detail(department_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    department = get_department(db, user.company_id, department_id)
    if department is None:
        if _is_json(request):
            return JSONResponse({"ok": False, "message": "Departamento no encontrado."}, status_code=404)
        raise HTTPException(status_code=404, detail="Departamento no encontrado.")
    context = _detail_context(request, db, user, department)
    if _is_json(request):
        return JSONResponse(
            {
                "ok": True,
                "department": _summary(db, department),
                "knowledge": [{"id": item.id, "title": item.title, "content": item.content, "knowledge_type": item.knowledge_type, "active": item.active} for item in context["knowledge"]],
                "members": [{"id": item.id, "user_id": item.user_id, "role": item.role, "active": item.active} for item in context["members"]],
                "raci": [{"id": item.id, "scope": item.scope, "role": item.raci_role, "user_id": item.user_id, "active": item.active} for item in context["raci"]],
            }
        )
    return templates.TemplateResponse("departments/detail.html", context)


@router.post("/{department_id}")
async def update_department_route(department_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    data = await _form_data(request)
    destination_email = data.get("destination_email", "").strip() or None
    if not data.get("name", "").strip():
        message = "El nombre del departamento es obligatorio."
        return _response(request, {"ok": False, "message": message}, redirect=_redirect(f"/departments/{department_id}", error=message), status_code=400)
    if not _valid_email(destination_email):
        message = "Indica una dirección de destino válida, por ejemplo logistica@empresa.com."
        return _response(request, {"ok": False, "message": message}, redirect=_redirect(f"/departments/{department_id}", error=message), status_code=400)
    try:
        department = update_department(
            db,
            user.company_id,
            department_id,
            name=data["name"],
            description=data.get("description", "").strip() or None,
            destination_email=destination_email,
            active=_truthy(data.get("active")),
        )
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        message = _friendly_error(exc)
        return _response(request, {"ok": False, "message": message}, redirect=_redirect(f"/departments/{department_id}", error=message), status_code=409 if isinstance(exc, IntegrityError) else 400)
    if department is None:
        return _response(request, {"ok": False, "message": "Departamento no encontrado."}, status_code=404)
    return _response(request, {"ok": True, "department": _summary(db, department)}, redirect=f"/departments/{department.id}")


@router.post("/{department_id}/toggle")
def toggle_department(department_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    department = get_department(db, user.company_id, department_id)
    if department is None:
        return _response(request, {"ok": False, "message": "Departamento no encontrado."}, status_code=404)
    updated = deactivate_department(db, user.company_id, department_id) if department.active else activate_department(db, user.company_id, department_id)
    return _response(request, {"ok": True, "active": updated.active}, redirect=_redirect(f"/departments/{department_id}", message="Estado actualizado."))


@router.post("/{department_id}/delete")
def delete_department_route(department_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    try:
        deleted = delete_department_safely(db, user.company_id, department_id)
    except DepartmentInUseError:
        message = "No se puede borrar: el departamento tiene dependencias de conocimiento, miembros o asignaciones RACI. Desactívalo si ya no debe utilizarse."
        return _response(request, {"ok": False, "message": message, "can_deactivate": True}, redirect=_redirect(f"/departments/{department_id}", error=message), status_code=409)
    if deleted is None:
        return _response(request, {"ok": False, "message": "Departamento no encontrado."}, status_code=404)
    return _response(request, {"ok": True}, redirect=_redirect("/departments", message="Departamento eliminado."))


@router.post("/{department_id}/knowledge")
async def create_knowledge_route(department_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    data = await _form_data(request)
    if not data.get("title", "").strip() or not data.get("content", "").strip():
        message = "El conocimiento necesita un título y un contenido descriptivo."
        return _response(request, {"ok": False, "message": message}, redirect=_redirect(f"/departments/{department_id}", error=message), status_code=400)
    try:
        item = create_department_knowledge(db, user.company_id, department_id, title=data.get("title", ""), content=data.get("content", ""), knowledge_type=data.get("knowledge_type", ""), active=_truthy(data.get("active"), default=True))
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        message = "El conocimiento necesita título, contenido y un tipo válido." if isinstance(exc, ValueError) else _friendly_error(exc)
        return _response(request, {"ok": False, "message": message}, redirect=_redirect(f"/departments/{department_id}", error=message), status_code=409 if isinstance(exc, IntegrityError) else 400)
    return _response(request, {"ok": True, "knowledge_id": item.id}, redirect=_redirect(f"/departments/{department_id}", message="Conocimiento añadido."))


@router.post("/{department_id}/knowledge/{knowledge_id}")
async def update_knowledge_route(department_id: int, knowledge_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    data = await _form_data(request)
    item = next((candidate for candidate in list_department_knowledge(db, user.company_id, department_id) if candidate.id == knowledge_id), None)
    if item is None:
        return _response(request, {"ok": False, "message": "Conocimiento no encontrado."}, status_code=404)
    if not data.get("title", "").strip() or not data.get("content", "").strip():
        message = "El conocimiento necesita un título y un contenido descriptivo."
        return _response(request, {"ok": False, "message": message}, redirect=_redirect(f"/departments/{department_id}", error=message), status_code=400)
    try:
        item = update_department_knowledge(db, user.company_id, knowledge_id, title=data.get("title", ""), content=data.get("content", ""), knowledge_type=data.get("knowledge_type", ""), active=_truthy(data.get("active")))
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        message = "Selecciona un tipo de conocimiento válido." if isinstance(exc, ValueError) else _friendly_error(exc)
        return _response(request, {"ok": False, "message": message}, redirect=_redirect(f"/departments/{department_id}", error=message), status_code=409 if isinstance(exc, IntegrityError) else 400)
    return _response(request, {"ok": True, "knowledge_id": item.id}, redirect=_redirect(f"/departments/{department_id}", message="Conocimiento actualizado."))


@router.post("/{department_id}/knowledge/{knowledge_id}/toggle")
def toggle_knowledge_route(department_id: int, knowledge_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    item = next((candidate for candidate in list_department_knowledge(db, user.company_id, department_id) if candidate.id == knowledge_id), None)
    if item is None:
        return _response(request, {"ok": False, "message": "Conocimiento no encontrado."}, status_code=404)
    updated = set_department_knowledge_active(db, user.company_id, knowledge_id, not item.active)
    return _response(request, {"ok": True, "active": updated.active}, redirect=_redirect(f"/departments/{department_id}", message="Estado actualizado."))


@router.post("/{department_id}/knowledge/{knowledge_id}/delete")
def delete_knowledge_route(department_id: int, knowledge_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    item = next((candidate for candidate in list_department_knowledge(db, user.company_id, department_id) if candidate.id == knowledge_id), None)
    if item is None or delete_department_knowledge(db, user.company_id, knowledge_id) is None:
        return _response(request, {"ok": False, "message": "Conocimiento no encontrado."}, status_code=404)
    return _response(request, {"ok": True}, redirect=_redirect(f"/departments/{department_id}", message="Conocimiento eliminado."))


@router.post("/{department_id}/members")
async def add_member_route(department_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    data = await _form_data(request)
    try:
        member = add_department_member(db, user.company_id, department_id, user_id=int(data.get("user_id", "0")), role=data.get("role", "").strip() or None, active=True)
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        message = "Selecciona un usuario válido de este tenant." if isinstance(exc, ValueError) else _friendly_error(exc)
        return _response(request, {"ok": False, "message": message}, redirect=_redirect(f"/departments/{department_id}", error=message), status_code=409 if isinstance(exc, IntegrityError) else 400)
    return _response(request, {"ok": True, "member_id": member.id}, redirect=_redirect(f"/departments/{department_id}", message="Miembro añadido."))


@router.post("/{department_id}/members/{member_id}/toggle")
def toggle_member_route(department_id: int, member_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    member = next((candidate for candidate in list_department_members(db, user.company_id, department_id) if candidate.id == member_id), None)
    if member is None:
        return _response(request, {"ok": False, "message": "Miembro no encontrado."}, status_code=404)
    updated = update_department_member(db, user.company_id, member_id, active=not member.active)
    return _response(request, {"ok": True, "active": updated.active}, redirect=_redirect(f"/departments/{department_id}", message="Estado actualizado."))


@router.post("/{department_id}/members/{member_id}/remove")
def remove_member_route(department_id: int, member_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    member = next((candidate for candidate in list_department_members(db, user.company_id, department_id) if candidate.id == member_id), None)
    if member is None or remove_department_member(db, user.company_id, member_id) is None:
        return _response(request, {"ok": False, "message": "Miembro no encontrado."}, status_code=404)
    return _response(request, {"ok": True}, redirect=_redirect(f"/departments/{department_id}", message="Miembro retirado."))


@router.post("/{department_id}/raci")
async def create_raci_route(department_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    data = await _form_data(request)
    try:
        user_value = data.get("user_id", "").strip()
        assignment = create_raci_assignment(db, user.company_id, department_id, scope=data.get("scope", "").strip() or "department", raci_role=data.get("raci_role", ""), user_id=int(user_value) if user_value else None, active=True)
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        message = "Indica un ámbito y un rol RACI válidos, y selecciona un usuario del tenant." if isinstance(exc, ValueError) else _friendly_error(exc)
        return _response(request, {"ok": False, "message": message}, redirect=_redirect(f"/departments/{department_id}", error=message), status_code=409 if isinstance(exc, IntegrityError) else 400)
    return _response(request, {"ok": True, "raci_id": assignment.id}, redirect=_redirect(f"/departments/{department_id}", message="Asignación RACI añadida."))


@router.post("/{department_id}/raci/{assignment_id}")
async def update_raci_route(department_id: int, assignment_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    data = await _form_data(request)
    user_value = data.get("user_id", "").strip()
    assignment = next((candidate for candidate in list_raci_assignments(db, user.company_id, department_id) if candidate.id == assignment_id), None)
    if assignment is None:
        return _response(request, {"ok": False, "message": "Asignación RACI no encontrada."}, status_code=404)
    try:
        assignment = update_raci_assignment(db, user.company_id, assignment_id, scope=data.get("scope", "").strip() or "department", raci_role=data.get("raci_role", ""), user_id=int(user_value) if user_value else None, active=_truthy(data.get("active")))
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        message = "Selecciona un rol RACI y un usuario válidos de este tenant." if isinstance(exc, ValueError) else _friendly_error(exc)
        return _response(request, {"ok": False, "message": message}, redirect=_redirect(f"/departments/{department_id}", error=message), status_code=409 if isinstance(exc, IntegrityError) else 400)
    return _response(request, {"ok": True, "raci_id": assignment.id}, redirect=_redirect(f"/departments/{department_id}", message="Asignación RACI actualizada."))


@router.post("/{department_id}/raci/{assignment_id}/toggle")
def toggle_raci_route(department_id: int, assignment_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    assignment = next((candidate for candidate in list_raci_assignments(db, user.company_id, department_id) if candidate.id == assignment_id), None)
    if assignment is None:
        return _response(request, {"ok": False, "message": "Asignación RACI no encontrada."}, status_code=404)
    updated = update_raci_assignment(db, user.company_id, assignment_id, active=not assignment.active)
    return _response(request, {"ok": True, "active": updated.active}, redirect=_redirect(f"/departments/{department_id}", message="Estado actualizado."))


@router.post("/{department_id}/raci/{assignment_id}/delete")
def delete_raci_route(department_id: int, assignment_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not _can_edit(user):
        return _response(request, {"ok": False, "message": "No tienes permisos para configurar departamentos."}, status_code=403)
    assignment = next((candidate for candidate in list_raci_assignments(db, user.company_id, department_id) if candidate.id == assignment_id), None)
    if assignment is None or delete_raci_assignment(db, user.company_id, assignment_id) is None:
        return _response(request, {"ok": False, "message": "Asignación RACI no encontrada."}, status_code=404)
    return _response(request, {"ok": True}, redirect=_redirect(f"/departments/{department_id}", message="Asignación RACI eliminada."))
