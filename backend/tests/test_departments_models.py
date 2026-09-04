from __future__ import annotations

import unittest

from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    Company,
    Department,
    DepartmentKnowledge,
    DepartmentMember,
    RaciAssignment,
    Role,
    User,
)
from app.migrations.registry import _apply_tenant_departments


def sqlite_engine():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


class DepartmentModelTests(unittest.TestCase):
    def setUp(self):
        self.engine = sqlite_engine()
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _seed_users(self, db):
        companies = [Company(id=1, name="Tenant A"), Company(id=2, name="Tenant B")]
        roles = [Role(id=1, company_id=1, name="Admin"), Role(id=2, company_id=2, name="Admin")]
        users = [
            User(id=1, company_id=1, role_id=1, email="a@example.com", name="A", password_hash="hash"),
            User(id=2, company_id=2, role_id=2, email="b@example.com", name="B", password_hash="hash"),
        ]
        db.add_all([*companies, *roles, *users])
        db.commit()

    def test_models_define_expected_columns_and_constraints(self):
        self.assertEqual(
            set(Department.__table__.columns.keys()),
            {"id", "company_id", "name", "description", "destination_email", "active", "created_at", "updated_at"},
        )
        self.assertEqual(
            set(DepartmentKnowledge.__table__.columns.keys()),
            {"id", "department_id", "title", "content", "knowledge_type", "active", "created_at", "updated_at"},
        )
        self.assertEqual(
            set(DepartmentMember.__table__.columns.keys()),
            {"id", "company_id", "department_id", "user_id", "active", "role", "created_at"},
        )
        self.assertEqual(
            set(RaciAssignment.__table__.columns.keys()),
            {"id", "company_id", "department_id", "user_id", "scope", "raci_role", "active", "created_at"},
        )

        department_unique_columns = {
            tuple(constraint.columns.keys())
            for constraint in Department.__table__.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        self.assertIn(("company_id", "name"), department_unique_columns)
        self.assertIn(("company_id", "id"), department_unique_columns)

        knowledge_checks = {
            str(constraint.sqltext)
            for constraint in DepartmentKnowledge.__table__.constraints
            if isinstance(constraint, CheckConstraint)
        }
        self.assertTrue(any("responsibility" in check and "guideline" in check for check in knowledge_checks))

        raci_checks = {
            str(constraint.sqltext)
            for constraint in RaciAssignment.__table__.constraints
            if isinstance(constraint, CheckConstraint)
        }
        self.assertTrue(any("responsible" in check and "informed" in check for check in raci_checks))

    def test_relationships_defaults_and_duplicate_constraints(self):
        with self.session_factory() as db:
            self._seed_users(db)
            department = Department(company_id=1, name="Ventas", active=True)
            db.add(department)
            db.commit()
            db.refresh(department)

            knowledge = DepartmentKnowledge(
                department_id=department.id,
                title="Responsabilidad principal",
                content="Gestionar pedidos.",
                knowledge_type="responsibility",
            )
            member = DepartmentMember(company_id=1, department_id=department.id, user_id=1, role="coordinador")
            db.add_all([knowledge, member])
            db.commit()

            db.refresh(department)
            self.assertEqual(department.knowledge_items[0].title, "Responsabilidad principal")
            self.assertEqual(department.members[0].user_id, 1)
            self.assertTrue(knowledge.active)
            self.assertTrue(member.active)
            self.assertIsNotNone(knowledge.created_at)
            self.assertIsNotNone(member.created_at)

            db.add(Department(company_id=1, name="Ventas"))
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

            db.add(
                DepartmentKnowledge(
                    department_id=department.id,
                    title="Tipo invalido",
                    content="No valido.",
                    knowledge_type="invalid",
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

            db.add(DepartmentMember(company_id=1, department_id=department.id, user_id=1))
            with self.assertRaises(IntegrityError):
                db.commit()

    def test_raci_composite_foreign_keys_keep_tenant_isolation(self):
        with self.session_factory() as db:
            self._seed_users(db)
            department_a = Department(company_id=1, name="Ventas")
            department_b = Department(company_id=2, name="Ventas")
            db.add_all([department_a, department_b])
            db.commit()

            assignment = RaciAssignment(
                company_id=1,
                department_id=department_a.id,
                user_id=1,
                scope="orders",
                raci_role="responsible",
            )
            db.add(assignment)
            db.commit()
            self.assertEqual(assignment.department.id, department_a.id)
            self.assertEqual(assignment.user.id, 1)

            db.add(
                RaciAssignment(
                    company_id=1,
                    department_id=department_a.id,
                    user_id=2,
                    scope="orders",
                    raci_role="consulted",
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

            db.add(
                RaciAssignment(
                    company_id=1,
                    department_id=department_a.id,
                    user_id=None,
                    scope="orders",
                    raci_role="informed",
                )
            )
            db.commit()
            db.add(
                RaciAssignment(
                    company_id=1,
                    department_id=department_a.id,
                    user_id=None,
                    scope="orders",
                    raci_role="informed",
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

            db.add(
                RaciAssignment(
                    company_id=1,
                    department_id=department_b.id,
                    user_id=None,
                    scope="orders",
                    raci_role="informed",
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()

    def test_migration_creates_schema_and_is_idempotent(self):
        engine = sqlite_engine()
        try:
            Company.__table__.create(engine)
            Role.__table__.create(engine)
            User.__table__.create(engine)

            dry_run_actions = _apply_tenant_departments(engine, dry_run=True)
            self.assertIn("CREATE TABLE departments (...)", dry_run_actions)
            self.assertNotIn("departments", inspect(engine).get_table_names())

            actions = _apply_tenant_departments(engine, dry_run=False)
            self.assertTrue(actions)
            self.assertTrue(
                {"departments", "department_knowledge", "department_members", "raci_assignments"}.issubset(
                    inspect(engine).get_table_names()
                )
            )
            self.assertIn("uq_departments_company_name", {index["name"] for index in inspect(engine).get_indexes("departments")})
            self.assertIn("uq_departments_company_id", {index["name"] for index in inspect(engine).get_indexes("departments")})
            self.assertIn("uq_users_company_id", {index["name"] for index in inspect(engine).get_indexes("users")})
            self.assertIn(
                "uq_raci_assignments_null_user",
                {index["name"] for index in inspect(engine).get_indexes("raci_assignments")},
            )

            self.assertEqual(_apply_tenant_departments(engine, dry_run=False), [])
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
