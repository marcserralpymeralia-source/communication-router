from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Company, Department, DepartmentMember, Role, User
from app.departments.routes import (
    add_member_route,
    create_department_route,
    create_knowledge_route,
    create_raci_route,
    department_detail,
    departments_page,
    delete_department_route,
    delete_knowledge_route,
    delete_raci_route,
    remove_member_route,
    toggle_department,
    toggle_knowledge_route,
    toggle_member_route,
    toggle_raci_route,
    update_department_route,
    update_knowledge_route,
    update_raci_route,
)
from app.departments.service import create_department, create_department_knowledge
from app.master.service import TenantRole, TenantUser


class FakeRequest:
    def __init__(self, data: dict | None = None, *, accept: str = "application/json"):
        self.data = data or {}
        self.headers = {"accept": accept, "content-type": "application/x-www-form-urlencoded"}
        self.query_params = {}
        self.url = SimpleNamespace(path="/departments")

    async def form(self):
        return SimpleNamespace(multi_items=lambda: list(self.data.items()))


def response_json(response):
    return json.loads(response.body.decode())


class DepartmentRouteTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add_all([Company(id=1, name="Tenant A"), Company(id=2, name="Tenant B")])
            db.add_all([Role(id=1, company_id=1, name="Admin"), Role(id=2, company_id=2, name="Admin")])
            db.add_all(
                [
                    User(id=1, company_id=1, role_id=1, email="ana@a.test", name="Ana", password_hash="x"),
                    User(id=2, company_id=2, role_id=2, email="bruno@b.test", name="Bruno", password_hash="x"),
                ]
            )
            db.commit()
        self.admin = TenantUser(1, "ana@a.test", "Ana", True, 1, "Tenant A", "tenant-a", TenantRole("Administrador"), 1)

    def tearDown(self):
        self.engine.dispose()

    def test_list_is_tenant_scoped_and_detail_rejects_foreign_department(self):
        with self.session_factory() as db:
            own = create_department(db, 1, name="Logística", destination_email="logistica@a.test")
            foreign = create_department(db, 2, name="Privado")
            response = departments_page(FakeRequest(), db, self.admin)
            payload = response_json(response)
            self.assertEqual([item["id"] for item in payload["items"]], [own.id])
            self.assertEqual(response_json(department_detail(foreign.id, FakeRequest(), db, self.admin))["ok"], False)

    def test_create_edit_invalid_email_and_activation(self):
        with self.session_factory() as db:
            invalid = asyncio.run(create_department_route(FakeRequest({"name": "Ventas", "destination_email": "no-es-email"}), db, self.admin))
            self.assertEqual(invalid.status_code, 400)
            self.assertIn("válida", response_json(invalid)["message"])
            invalid_html = asyncio.run(create_department_route(FakeRequest({"name": "Ventas", "destination_email": "no-es-email"}, accept="text/html"), db, self.admin))
            self.assertEqual(invalid_html.status_code, 303)
            self.assertIn("error=", invalid_html.headers["location"])

            created = asyncio.run(create_department_route(FakeRequest({"name": "Logística", "description": "Entrega", "destination_email": "logistica@a.test", "active": "on"}), db, self.admin))
            department_id = response_json(created)["department"]["id"]
            updated = asyncio.run(update_department_route(department_id, FakeRequest({"name": "Operaciones", "description": "Transporte", "destination_email": "ops@a.test", "active": "on"}), db, self.admin))
            self.assertEqual(response_json(updated)["department"]["name"], "Operaciones")
            toggle_department(department_id, FakeRequest(), db, self.admin)
            self.assertFalse(db.get(Department, department_id).active)

    def test_knowledge_crud_is_tenant_scoped(self):
        with self.session_factory() as db:
            department = create_department(db, 1, name="Logística")
            foreign = create_department(db, 2, name="Privado")
            item = create_department_knowledge(db, 1, department.id, title="Entrega", content="Mercancía no recibida", knowledge_type="example")
            created = asyncio.run(create_knowledge_route(department.id, FakeRequest({"title": "Responsabilidad", "content": "Gestiona incidencias", "knowledge_type": "responsibility", "active": "on"}), db, self.admin))
            self.assertEqual(created.status_code, 200)
            asyncio.run(update_knowledge_route(department.id, item.id, FakeRequest({"title": "Ejemplo actualizado", "content": "Contenido", "knowledge_type": "example", "active": "on"}), db, self.admin))
            toggle_knowledge_route(department.id, item.id, FakeRequest(), db, self.admin)
            self.assertFalse(item.active)
            self.assertEqual(delete_knowledge_route(foreign.id, item.id, FakeRequest(), db, self.admin).status_code, 404)
            self.assertEqual(delete_knowledge_route(department.id, item.id, FakeRequest(), db, self.admin).status_code, 200)

    def test_members_are_tenant_safe_and_can_be_toggled_or_removed(self):
        with self.session_factory() as db:
            department = create_department(db, 1, name="Logística")
            other_department = create_department(db, 1, name="Otra área")
            foreign_user = asyncio.run(add_member_route(department.id, FakeRequest({"user_id": "2"}), db, self.admin))
            self.assertEqual(foreign_user.status_code, 400)
            member = asyncio.run(add_member_route(department.id, FakeRequest({"user_id": "1", "role": "Coordinación"}), db, self.admin))
            member_id = response_json(member)["member_id"]
            toggle_member_route(department.id, member_id, FakeRequest(), db, self.admin)
            self.assertFalse(db.scalar(select(DepartmentMember.active).where(DepartmentMember.id == member_id)))
            self.assertEqual(remove_member_route(other_department.id, member_id, FakeRequest(), db, self.admin).status_code, 404)
            self.assertEqual(remove_member_route(department.id, member_id, FakeRequest(), db, self.admin).status_code, 200)
            self.assertEqual(remove_member_route(department.id, member_id, FakeRequest(), db, self.admin).status_code, 404)

    def test_raci_crud_and_protected_delete(self):
        with self.session_factory() as db:
            department = create_department(db, 1, name="Logística")
            created = asyncio.run(create_raci_route(department.id, FakeRequest({"scope": "delivery_incident", "raci_role": "responsible", "user_id": "1"}), db, self.admin))
            raci_id = response_json(created)["raci_id"]
            updated = asyncio.run(update_raci_route(department.id, raci_id, FakeRequest({"scope": "delivery_incident", "raci_role": "accountable", "user_id": "" , "active": "on"}), db, self.admin))
            self.assertEqual(updated.status_code, 200)
            toggle_raci_route(department.id, raci_id, FakeRequest(), db, self.admin)
            self.assertEqual(delete_raci_route(department.id, raci_id, FakeRequest(), db, self.admin).status_code, 200)

            protected = create_department(db, 1, name="Protegido")
            create_department_knowledge(db, 1, protected.id, title="Regla", content="Contenido", knowledge_type="guideline")
            response = delete_department_route(protected.id, FakeRequest(), db, self.admin)
            self.assertEqual(response.status_code, 409)
            self.assertIn("dependencias", response_json(response)["message"])


if __name__ == "__main__":
    unittest.main()
