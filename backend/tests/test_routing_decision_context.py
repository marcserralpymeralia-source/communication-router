from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Communication, Company, Department, DepartmentKnowledge
from app.departments.service import create_department_knowledge
from app.routing.decision_context import (
    build_department_routing_profile,
    build_routing_decision_context,
)


class RoutingDecisionContextTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add_all([Company(id=1, name="Tenant A"), Company(id=2, name="Tenant B")])
            db.add_all([
                Department(id=10, company_id=1, name="Comercial", description="Presupuestos y precios"),
                Department(id=11, company_id=1, name="Logistica", description="Entregas y transporte"),
                Department(id=12, company_id=1, name="Administracion", description="Facturas y pagos"),
                Department(id=13, company_id=1, name="Archivado", active=False),
                Department(id=20, company_id=2, name="Privado", description="No visible en Tenant A"),
            ])
            db.add_all([
                DepartmentKnowledge(department_id=10, title="Presupuestos", content="Gestionar precios y ofertas", knowledge_type="responsibility", priority="NORMAL"),
                DepartmentKnowledge(department_id=10, title="Frontera con logística", content="Si la acción es reclamar una entrega, corresponde a logística", knowledge_type="guideline", related_department_id=11, priority="CRITICAL"),
                DepartmentKnowledge(department_id=11, title="Entrega", content="Gestionar retrasos y transporte", knowledge_type="responsibility"),
                DepartmentKnowledge(department_id=20, title="Privado", content="No debe filtrarse a otro tenant", knowledge_type="responsibility"),
            ])
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    @staticmethod
    def _communication(company_id=1, subject="Presupuesto", body="Necesitamos precios y condiciones"):
        return Communication(company_id=company_id, subject=subject, body_text=body)

    def test_candidates_and_evidence_are_tenant_scoped_and_inactive_excluded(self):
        with self.session_factory() as db:
            context = build_routing_decision_context(db, 1, self._communication())
            candidate_ids = {item["department_id"] for item in context["departments"]}
            evidence_ids = {item["department_id"] for item in context["knowledge"]}
            self.assertIn(10, candidate_ids)
            self.assertNotIn(13, candidate_ids)
            self.assertNotIn(20, candidate_ids)
            self.assertNotIn(20, evidence_ids)

    def test_retrieval_limits_candidates_and_knowledge(self):
        with self.session_factory() as db:
            for index in range(8):
                db.add(DepartmentKnowledge(
                    department_id=10,
                    title=f"Regla {index}",
                    content=f"Regla comercial {index} presupuesto",
                    knowledge_type="example" if index else "exception",
                    priority="LOW" if index else "CRITICAL",
                ))
            db.commit()
            context = build_routing_decision_context(db, 1, self._communication())
            self.assertLessEqual(len(context["departments"]), 3)
            self.assertLessEqual(len(context["knowledge"]), 10)
            per_department = {}
            for item in context["knowledge"]:
                per_department[item["department_id"]] = per_department.get(item["department_id"], 0) + 1
            self.assertTrue(all(count <= 4 for count in per_department.values()))
            self.assertLess(context["retrieval"]["evidence_ids"].index("K2"), len(context["knowledge"]))

    def test_related_cross_tenant_department_is_rejected(self):
        with self.session_factory() as db:
            with self.assertRaisesRegex(ValueError, "Related department not found for tenant"):
                create_department_knowledge(
                    db,
                    1,
                    10,
                    title="Regla cruzada",
                    content="No puede apuntar al tenant B",
                    knowledge_type="guideline",
                    related_department_id=20,
                )

    def test_boundary_rule_between_selected_candidates_is_explicit(self):
        with self.session_factory() as db:
            context = build_routing_decision_context(
                db,
                1,
                self._communication(subject="Entrega", body="Hay un retraso en el transporte"),
            )
            self.assertTrue(context["boundary_rules"])
            self.assertEqual(context["boundary_rules"][0]["evidence_id"], "K2")

    def test_profile_changes_with_semantic_knowledge_but_not_members_or_raci(self):
        with self.session_factory() as db:
            department = db.get(Department, 10)
            items = list(db.query(DepartmentKnowledge).filter_by(department_id=10))
            first = build_department_routing_profile(department, items)
            department.description = "Nuevo alcance de contratos y presupuestos"
            second = build_department_routing_profile(department, items)
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
