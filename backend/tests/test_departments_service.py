from __future__ import annotations

import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Company, Role, User
from app.departments.service import (
    DepartmentInUseError,
    activate_department,
    add_department_member,
    build_department_routing_context,
    create_department,
    create_department_knowledge,
    create_raci_assignment,
    deactivate_department,
    delete_department_knowledge,
    delete_department_safely,
    delete_raci_assignment,
    list_department_members,
    list_departments,
    list_raci_assignments,
    remove_department_member,
    update_department,
)


class DepartmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add_all([Company(id=1, name="Tenant A"), Company(id=2, name="Tenant B")])
            db.add_all(
                [
                    Role(id=1, company_id=1, name="Admin"),
                    Role(id=2, company_id=2, name="Admin"),
                    User(id=1, company_id=1, role_id=1, email="ana@a.test", name="Ana", password_hash="x"),
                    User(id=2, company_id=2, role_id=2, email="bruno@b.test", name="Bruno", password_hash="x"),
                ]
            )
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def test_crud_activation_and_tenant_isolation(self):
        with self.session_factory() as db:
            department = create_department(
                db,
                1,
                name=" Logística ",
                description="Pedidos y transporte",
                destination_email="logistica@a.test",
            )
            other_tenant = create_department(db, 2, name="Privado")
            self.assertEqual(department.name, "Logística")
            self.assertEqual([item.id for item in list_departments(db, 1)], [department.id])
            self.assertEqual([item.id for item in list_departments(db, 2)], [other_tenant.id])
            self.assertIsNone(update_department(db, 2, department.id, name="No tocar"))

            deactivate_department(db, 1, department.id)
            self.assertEqual(list_departments(db, 1, active_only=True), [])
            activate_department(db, 1, department.id)
            update_department(db, 1, department.id, description="Operativa")
            self.assertEqual(department.description, "Operativa")

    def test_context_contains_only_active_tenant_data_grouped_for_routing(self):
        with self.session_factory() as db:
            active = create_department(db, 1, name="Logística", destination_email="logistica@a.test")
            inactive = create_department(db, 1, name="Antiguo", active=False)
            foreign = create_department(db, 2, name="Logística", destination_email="logistica@b.test")

            active_rule = create_department_knowledge(
                db,
                1,
                active.id,
                title="Responsabilidad principal",
                content="Gestionar transporte",
                knowledge_type="responsibility",
            )
            create_department_knowledge(
                db,
                1,
                active.id,
                title="Borrada",
                content="No usar",
                knowledge_type="exclusion",
                active=False,
            )
            create_department_knowledge(
                db,
                1,
                inactive.id,
                title="Regla antigua",
                content="No debe aparecer",
                knowledge_type="guideline",
            )
            create_department_knowledge(
                db,
                2,
                foreign.id,
                title="Regla ajena",
                content="Otro tenant",
                knowledge_type="example",
            )
            member = add_department_member(db, 1, active.id, user_id=1, role="Coordinadora")
            raci = create_raci_assignment(db, 1, active.id, user_id=1, scope="transporte", raci_role="responsible")

            context = build_department_routing_context(1, db)
            self.assertEqual(len(context["departments"]), 1)
            item = context["departments"][0]
            self.assertEqual(item["department_id"], active.id)
            self.assertEqual(item["destination_email"], "logistica@a.test")
            self.assertEqual(item["knowledge"]["responsibilities"][0]["knowledge_id"], active_rule.id)
            self.assertEqual(item["knowledge"]["exclusions"], [])
            self.assertEqual(item["members"][0]["member_id"], member.id)
            self.assertEqual(item["members"][0]["email"], "ana@a.test")
            self.assertEqual(item["raci"][0]["raci_id"], raci.id)
            self.assertNotIn("Regla ajena", str(context))
            self.assertNotIn("Antiguo", str(context))

    def test_child_operations_and_safe_delete_are_tenant_scoped(self):
        with self.session_factory() as db:
            department = create_department(db, 1, name="Operaciones")
            knowledge = create_department_knowledge(
                db,
                1,
                department.id,
                title="Regla",
                content="Contenido",
                knowledge_type="guidelines",
            )
            member = add_department_member(db, 1, department.id, user_id=1)
            raci = create_raci_assignment(db, 1, department.id, user_id=1, raci_role="accountable")

            self.assertIsNone(delete_department_knowledge(db, 2, knowledge.id))
            self.assertIsNone(remove_department_member(db, 2, member.id))
            self.assertIsNone(delete_raci_assignment(db, 2, raci.id))
            with self.assertRaises(DepartmentInUseError):
                delete_department_safely(db, 1, department.id)

            delete_department_knowledge(db, 1, knowledge.id)
            remove_department_member(db, 1, member.id)
            delete_raci_assignment(db, 1, raci.id)
            self.assertTrue(delete_department_safely(db, 1, department.id))
            self.assertIsNone(db.scalar(select(type(department)).where(type(department).id == department.id)))

    def test_cross_tenant_users_cannot_be_assigned(self):
        with self.session_factory() as db:
            department = create_department(db, 1, name="Compras")
            with self.assertRaisesRegex(ValueError, "User not found"):
                add_department_member(db, 1, department.id, user_id=2)
            with self.assertRaisesRegex(ValueError, "User not found"):
                create_raci_assignment(db, 1, department.id, user_id=2, raci_role="informed")


if __name__ == "__main__":
    unittest.main()
