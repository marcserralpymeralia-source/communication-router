from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Company, Department, DepartmentKnowledge
from app.routing.learning import (
    build_department_reconciliation,
    build_evaluation_record,
    compare_case_sources,
    load_routing_cases,
    review_knowledge_proposals,
    plan_knowledge_import,
    reconstruct_human_destination,
    validate_knowledge_proposals,
)


class RoutingLearningTests(unittest.TestCase):
    def test_human_destination_rules_preserve_audit_inputs(self):
        self.assertEqual(reconstruct_human_destination("legacy@example.com", "OK"), "legacy@example.com")
        self.assertEqual(
            reconstruct_human_destination(
                "legacy@example.com",
                "NO_FORWARD",
                no_forward_destination="mantenimiento@ingesco.test",
            ),
            "mantenimiento@ingesco.test",
        )
        self.assertEqual(reconstruct_human_destination("legacy@example.com", "Mantenimiento@Example.com"), "mantenimiento@example.com")
        self.assertEqual(
            reconstruct_human_destination("operaciones@example.com", "també a prevencion@example.com"),
            "operaciones@example.com; prevencion@example.com",
        )
        self.assertEqual(
            reconstruct_human_destination("comercial@example.com", "operaciones@example.com + facturacion@example.com"),
            "operaciones@example.com; facturacion@example.com",
        )

    def test_normalization_does_not_merge_different_domains(self):
        self.assertNotEqual(
            reconstruct_human_destination("legacy@example.com", "mantenimientointegral@quibac.test"),
            reconstruct_human_destination("legacy@example.com", "mantenimientointegral@ingesco.test"),
        )

    def test_original_csv_parser_supports_multiline_body_and_keeps_raw_values(self):
        content = (
            "Adreça client/proveedor,Assumpte correu,Text del correu rebut,Reenviar a ,correccion,motiu\n"
            "sender@example.com,Asunto,\"Linea 1\nLinea 2\",legacy@example.com,NO_FORWARD,\"Revisión\"\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.csv"
            path.write_text(content, encoding="utf-8")
            case = load_routing_cases(path)[0]
        self.assertEqual(case.legacy_prediction_raw, "legacy@example.com")
        self.assertEqual(case.human_correction_raw, "NO_FORWARD")
        self.assertEqual(case.final_human_destination, "")
        self.assertIn("Linea 2", case.body)

    def test_source_comparison_reports_only_normalization_differences(self):
        original = load_routing_cases(self._csv_path("legacy@example.com", "NO_FORWARD"))
        normalized = load_routing_cases(self._csv_path("legacy@example.com", "NO_FORWARD"))
        self.assertEqual(compare_case_sources(original, normalized), [])

    def test_evaluation_record_marks_multiple_destinations_for_review(self):
        case = load_routing_cases(self._csv_path("legacy@example.com", "OK"))[0]
        record = build_evaluation_record(case)
        self.assertEqual(record["expected_destination"], "legacy@example.com")
        self.assertFalse(record["expected_requires_review"])

    def test_dry_run_is_tenant_scoped_and_does_not_modify_db(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        try:
            with factory() as db:
                db.add_all([
                    Company(id=1, name="Tenant A"),
                    Company(id=2, name="Tenant B"),
                    Department(id=10, company_id=1, name="Mantenimiento"),
                    Department(id=20, company_id=2, name="Mantenimiento"),
                    DepartmentKnowledge(department_id=10, title="Responsabilidad", content="Antiguo", knowledge_type="responsibility"),
                ])
                db.commit()
                proposals = [{
                    "department": "Mantenimiento",
                    "type": "RESPONSIBILITY",
                    "title": "Responsabilidad",
                    "content": "Nuevo",
                    "priority": "HIGH",
                }]
                plan = plan_knowledge_import(db, 1, proposals)
                self.assertEqual(plan[0]["operation"], "UPDATE")
                self.assertEqual(db.scalar(select(func.count(DepartmentKnowledge.id))), 1)
                self.assertEqual(db.get(DepartmentKnowledge, 1).content, "Antiguo")
        finally:
            engine.dispose()

    def test_proposal_validation_rejects_unknown_supporting_case(self):
        with self.assertRaisesRegex(ValueError, "Unknown supporting case IDs"):
            validate_knowledge_proposals([{
                "department": "Mantenimiento",
                "type": "RESPONSIBILITY",
                "title": "Responsabilidad",
                "content": "Contenido",
                "priority": "HIGH",
                "supporting_case_ids": ["999"],
            }], load_routing_cases(self._csv_path("legacy@example.com", "OK")))

    def test_reconciliation_uses_exact_destination_and_keeps_domain_variant_ambiguous(self):
        cases = load_routing_cases(self._csv_path("mantenimiento@ingesco.test", "OK"))
        cases.append(load_routing_cases(self._csv_path("legacy@example.com", "mantenimientointegral@ingesco.test"))[0])
        reconciliation = build_department_reconciliation(cases, [
            {"id": 9, "name": "Mantenimiento", "destination_email": "mantenimiento@ingesco.test", "active": True},
            {"id": 18, "name": "Mantenimiento Integral", "destination_email": "mantenimientointegral@quibac.test", "active": True},
        ])
        by_destination = {item["observed_destination"]: item for item in reconciliation["departments"]}
        self.assertEqual(by_destination["mantenimiento@ingesco.test"]["department_id"], 9)
        self.assertEqual(by_destination["mantenimientointegral@ingesco.test"]["status"], "AMBIGUOUS")
        self.assertIsNone(by_destination["mantenimientointegral@ingesco.test"]["department_id"])

    def test_reviewed_proposals_have_deterministic_evidence_and_status(self):
        cases = load_routing_cases(self._csv_path("mantenimiento@ingesco.test", "OK"))
        proposals = [{
            "department": "Mantenimiento",
            "destination_email": "mantenimiento@ingesco.test",
            "type": "RESPONSIBILITY",
            "title": "Gestión operativa de mantenimiento",
            "content": "Gestiona actuaciones",
            "priority": "HIGH",
        }]
        reviewed = review_knowledge_proposals(proposals, cases, [{
            "id": 9,
            "name": "Mantenimiento",
            "destination_email": "mantenimiento@ingesco.test",
            "active": True,
        }])
        self.assertEqual(reviewed[0]["status"], "REVIEW")
        self.assertEqual(reviewed[0]["department_id"], 9)
        self.assertEqual(reviewed[0]["support_count"], 1)
        self.assertEqual(reviewed[0]["contradiction_count"], 0)

    def test_review_proposal_is_never_importable(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        try:
            with factory() as db:
                db.add_all([Company(id=1, name="Tenant A"), Department(id=10, company_id=1, name="Mantenimiento")])
                db.commit()
                plan = plan_knowledge_import(db, 1, [{
                    "department": "Mantenimiento",
                    "department_id": 10,
                    "type": "RESPONSIBILITY",
                    "title": "Revisar",
                    "content": "Pendiente",
                    "priority": "HIGH",
                    "status": "REVIEW",
                }])
                self.assertEqual(plan[0]["operation"], "REVIEW")
        finally:
            engine.dispose()

    @staticmethod
    def _csv_path(legacy: str, correction: str) -> Path:
        directory = tempfile.mkdtemp()
        path = Path(directory) / "cases.csv"
        path.write_text(
            "case_id,sender_or_source,subject,body,agent_proposal_raw,agent_proposal_normalized,human_correction_raw,final_destination_normalized,human_reason,status\n"
            f"1,sender@example.com,Asunto,Cuerpo,{legacy},{legacy},{correction},,,human_corrected\n",
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()
