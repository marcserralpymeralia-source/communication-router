from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("APP_ENV", "test")

from sqlalchemy import create_engine, event, select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db.models import (  # noqa: E402
    Company,
    Department,
    DepartmentKnowledge,
    DepartmentMember,
    RaciAssignment,
    Role,
    User,
)
from app.departments.service import build_department_routing_context  # noqa: E402


class DepartmentIsolationAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{self.tmpdir.name}/departments.db",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.db: Session = self.SessionLocal()
        self.company_a = Company(id=1, name="Tenant A")
        self.company_b = Company(id=2, name="Tenant B")
        self.role_a = Role(id=1, company_id=1, name="Admin A")
        self.role_b = Role(id=2, company_id=2, name="Admin B")
        self.user_a = User(
            id=1,
            company_id=1,
            role_id=1,
            email="a@example.com",
            name="User A",
            password_hash="hash",
        )
        self.user_b = User(
            id=2,
            company_id=2,
            role_id=2,
            email="b@example.com",
            name="User B",
            password_hash="hash",
        )
        self.db.add_all([self.company_a, self.company_b, self.role_a, self.role_b, self.user_a, self.user_b])
        self.db.flush()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _departments(self) -> tuple[Department, Department]:
        department_a = Department(company_id=1, name="Operaciones")
        department_b = Department(company_id=2, name="Operaciones")
        self.db.add_all([department_a, department_b])
        self.db.flush()
        return department_a, department_b

    def test_tenant_a_query_does_not_return_tenant_b_departments(self):
        department_a, department_b = self._departments()

        result = self.db.scalars(select(Department).where(Department.company_id == 1)).all()

        self.assertEqual([department_a.id], [department.id for department in result])
        self.assertNotIn(department_b.id, [department.id for department in result])

    def test_department_names_are_unique_per_tenant_only(self):
        self._departments()
        self.db.commit()

        duplicate_in_a = Department(company_id=1, name="Operaciones")
        self.db.add(duplicate_in_a)
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

        departments = self.db.scalars(select(Department).where(Department.name == "Operaciones")).all()
        self.assertEqual(2, len(departments))
        self.assertEqual({1, 2}, {department.company_id for department in departments})

    def test_department_knowledge_isolated_through_department_tenant(self):
        department_a, department_b = self._departments()
        knowledge_a = DepartmentKnowledge(
            department_id=department_a.id,
            title="Regla A",
            content="Solo Tenant A",
            knowledge_type="guideline",
        )
        knowledge_b = DepartmentKnowledge(
            department_id=department_b.id,
            title="Regla B",
            content="Solo Tenant B",
            knowledge_type="guideline",
        )
        self.db.add_all([knowledge_a, knowledge_b])
        self.db.flush()

        result = self.db.scalars(
            select(DepartmentKnowledge)
            .join(Department)
            .where(Department.company_id == 1)
        ).all()

        self.assertEqual([knowledge_a.id], [item.id for item in result])
        self.assertNotIn(knowledge_b.id, [item.id for item in result])

    def test_user_from_another_tenant_cannot_be_added_as_department_member(self):
        department_a, _department_b = self._departments()
        self.db.add(DepartmentMember(company_id=1, department_id=department_a.id, user_id=self.user_b.id))

        with self.assertRaises(IntegrityError):
            self.db.commit()

    def test_raci_assignment_cannot_cross_department_tenants(self):
        department_a, _department_b = self._departments()
        self.db.add(
            RaciAssignment(
                company_id=1,
                department_id=department_a.id,
                user_id=self.user_a.id,
                scope="department",
                raci_role="responsible",
            )
        )
        self.db.commit()

        self.db.add(
            RaciAssignment(
                company_id=1,
                department_id=department_a.id,
                user_id=self.user_b.id,
                scope="department",
                raci_role="accountable",
            )
        )
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_deleting_department_with_dependencies_is_not_destructive(self):
        department_a, _department_b = self._departments()
        knowledge = DepartmentKnowledge(
            department_id=department_a.id,
            title="Regla A",
            content="Conservar",
            knowledge_type="guideline",
        )
        member = DepartmentMember(company_id=1, department_id=department_a.id, user_id=self.user_a.id)
        raci = RaciAssignment(
            company_id=1,
            department_id=department_a.id,
            user_id=self.user_a.id,
            scope="department",
            raci_role="responsible",
        )
        self.db.add_all([knowledge, member, raci])
        self.db.commit()

        self.db.delete(department_a)
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

        self.assertIsNotNone(self.db.get(DepartmentKnowledge, knowledge.id))
        self.assertIsNotNone(self.db.get(DepartmentMember, member.id))
        self.assertIsNotNone(self.db.get(RaciAssignment, raci.id))

    def test_destination_email_rejects_invalid_values(self):
        with self.assertRaises((IntegrityError, ValueError)):
            self.db.add(Department(company_id=1, name="Operaciones", destination_email="not-an-email"))
            self.db.commit()
        self.db.rollback()

    def test_destination_email_accepts_valid_value(self):
        department = Department(company_id=1, name="Operaciones", destination_email="ops@example.com")
        self.db.add(department)
        self.db.commit()

        self.assertEqual(department.destination_email, "ops@example.com")

    def test_inactive_department_is_excluded_from_routing_context(self):
        active, inactive = self._departments()
        inactive.active = False
        self.db.commit()

        context = build_department_routing_context(1, self.db)

        self.assertEqual([active.id], [item["department_id"] for item in context["departments"]])
        self.assertNotIn(inactive.id, [item["department_id"] for item in context["departments"]])

    def test_inactive_knowledge_is_excluded_from_routing_context(self):
        department_a, _department_b = self._departments()
        self.db.add_all(
            [
                DepartmentKnowledge(
                    department_id=department_a.id,
                    title="Activa",
                    content="Incluir",
                    knowledge_type="guideline",
                    active=True,
                ),
                DepartmentKnowledge(
                    department_id=department_a.id,
                    title="Inactiva",
                    content="Excluir",
                    knowledge_type="guideline",
                    active=False,
                ),
            ]
        )
        self.db.commit()

        context = build_department_routing_context(1, self.db)
        knowledge = context["departments"][0]["knowledge"]["guidelines"]

        self.assertEqual(["Activa"], [item["title"] for item in knowledge])

    def test_context_builder_returns_only_valid_tenant_data(self):
        department_a, department_b = self._departments()
        self.db.add_all(
            [
                DepartmentKnowledge(
                    department_id=department_a.id,
                    title="Regla A",
                    content="Tenant A",
                    knowledge_type="responsibility",
                ),
                DepartmentKnowledge(
                    department_id=department_b.id,
                    title="Regla B",
                    content="Tenant B",
                    knowledge_type="responsibility",
                ),
                DepartmentMember(company_id=1, department_id=department_a.id, user_id=self.user_a.id),
                DepartmentMember(company_id=2, department_id=department_b.id, user_id=self.user_b.id),
                RaciAssignment(
                    company_id=1,
                    department_id=department_a.id,
                    user_id=self.user_a.id,
                    scope="department",
                    raci_role="responsible",
                ),
                RaciAssignment(
                    company_id=2,
                    department_id=department_b.id,
                    user_id=self.user_b.id,
                    scope="department",
                    raci_role="responsible",
                ),
            ]
        )
        self.db.commit()

        context = build_department_routing_context(1, self.db)

        self.assertEqual([department_a.id], [item["department_id"] for item in context["departments"]])
        self.assertNotIn("Tenant B", str(context))


if __name__ == "__main__":
    unittest.main()
