from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.database import Base
from app.db.models import (
    Company,
    Communication,
    Department,
    Mailbox,
    PromptExecution,
    RoutingAction,
    RoutingEvaluationCase,
    RoutingEvaluationResult,
    RoutingEvaluationRun,
    RoutingEvaluationSet,
)
from app.routing.evaluations import (
    calculate_metrics,
    compare_runs,
    create_evaluation_case,
    create_evaluation_set,
    get_evaluation_set,
    playground_analysis,
    run_evaluation,
    seed_demo_evaluation_set,
    simulate_threshold,
    update_evaluation_case,
    update_evaluation_set,
)


class RoutingEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add_all(
                [
                    Company(id=1, name="Tenant A"),
                    Company(id=2, name="Tenant B"),
                    Mailbox(id=1, company_id=1, name="Entrada A", email_address="a@example.test"),
                    Mailbox(id=2, company_id=2, name="Entrada B", email_address="b@example.test"),
                ]
            )
            db.add_all(
                [
                    Department(id=1, company_id=1, name="Comercial"),
                    Department(id=2, company_id=1, name="Logística"),
                    Department(id=3, company_id=2, name="Privado"),
                ]
            )
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    @staticmethod
    def _provider(department_id: int = 1, confidence: float = 0.95):
        def provider(settings, messages, model):
            del settings, messages, model
            return {
                "ok": True,
                "content": json.dumps(
                    {
                        "proposed_department_id": department_id,
                        "category": "consulta",
                        "confidence": confidence,
                        "requires_review": False,
                        "reason": "La intención del caso coincide con el contexto.",
                        "alternative_department_id": None,
                        "ambiguity_reason": None,
                    }
                ),
            }

        return provider

    def test_demo_seed_is_idempotent_and_has_no_provider_side_effect(self):
        with self.session_factory() as db:
            first = seed_demo_evaluation_set(db, 1)
            second = seed_demo_evaluation_set(db, 1)
            self.assertEqual(first.id, second.id)
            self.assertEqual(db.scalar(select(func.count(RoutingEvaluationCase.id))), 40)
            self.assertEqual(db.scalar(select(func.count(Communication.id))), 0)

    def test_playground_is_dry_run_and_runtime_does_not_commit(self):
        with self.session_factory() as db:
            with patch.object(db, "commit") as commit:
                result = playground_analysis(
                    db,
                    1,
                    subject="Entrega retrasada",
                    body="El transporte no ha llegado.",
                    provider_call=self._provider(2),
                )
            commit.assert_not_called()
            self.assertEqual(result["proposal"]["proposed_department_id"], 2)
            execution = db.scalar(select(PromptExecution))
            self.assertEqual(execution.input_reference, "playground:transient")
            self.assertEqual(db.scalar(select(func.count(Communication.id))), 0)
            self.assertEqual(db.scalar(select(func.count(RoutingAction.id))), 0)

    def test_run_persists_prompt_snapshot_results_and_metrics_only(self):
        with self.session_factory() as db:
            evaluation_set = create_evaluation_set(db, 1, name="Smoke", commit=False)
            create_evaluation_case(
                db,
                1,
                evaluation_set.id,
                title="Correcto",
                subject="Consulta",
                body="Necesito una tarifa.",
                expected_department_id=1,
                expected_category="consulta",
                expected_requires_review=False,
                commit=False,
            )
            db.commit()
            run = run_evaluation(db, 1, evaluation_set.id, provider_call=self._provider(1), commit=True)
            metrics = json.loads(run.metrics_json)
            self.assertEqual(run.status, "completed")
            self.assertEqual(run.prompt_purpose, "communication_department_routing")
            self.assertEqual(run.prompt_version, 1)
            self.assertEqual(metrics["department_accuracy"], 1.0)
            self.assertEqual(db.scalar(select(func.count(PromptExecution.id))), 1)
            self.assertEqual(db.scalar(select(func.count(Communication.id))), 0)
            self.assertEqual(db.scalar(select(func.count(RoutingAction.id))), 0)

    def test_metrics_threshold_simulation_and_ab_compare(self):
        with self.session_factory() as db:
            results = [
                RoutingEvaluationResult(company_id=1, run_id=1, evaluation_case_id=1, expected_department_id=1, predicted_department_id=1, confidence=.95, requires_review=False, correct_department=True, auto_route_candidate=True, false_auto_route=False),
                RoutingEvaluationResult(company_id=1, run_id=1, evaluation_case_id=2, expected_department_id=2, predicted_department_id=1, confidence=.91, requires_review=False, correct_department=False, auto_route_candidate=True, false_auto_route=True, criticality="critical"),
            ]
            for result in results:
                db.add(result)
            db.flush()
            metrics = calculate_metrics(results)
            self.assertEqual(metrics["false_auto_route_count"], 1)
            self.assertEqual(metrics["critical_false_auto_routes"], 1)
            self.assertEqual(simulate_threshold(results, .93)["candidate_count"], 1)
            first = RoutingEvaluationRun(company_id=1, evaluation_set_id=1)
            second = RoutingEvaluationRun(company_id=1, evaluation_set_id=1)
            db.add_all([first, second])
            db.flush()
            before = RoutingEvaluationResult(company_id=1, run_id=first.id, evaluation_case_id=3, expected_department_id=1, predicted_department_id=2, confidence=.8, requires_review=True, correct_department=False)
            after = RoutingEvaluationResult(company_id=1, run_id=second.id, evaluation_case_id=3, expected_department_id=1, predicted_department_id=1, confidence=.95, requires_review=False, correct_department=True, auto_route_candidate=True)
            db.add_all([before, after])
            db.flush()
            first.results = [before]
            second.results = [after]
            comparison = compare_runs(first, second)
            self.assertEqual(comparison["improved"], 1)
            self.assertEqual(comparison["automation_changed"], 1)

    def test_set_lookup_is_tenant_scoped(self):
        with self.session_factory() as db:
            evaluation_set = create_evaluation_set(db, 1, name="Privado", commit=False)
            db.commit()
            self.assertIsNotNone(get_evaluation_set(db, 1, evaluation_set.id))
            self.assertIsNone(get_evaluation_set(db, 2, evaluation_set.id))

    def test_sets_and_cases_support_tenant_scoped_update_and_deactivate(self):
        with self.session_factory() as db:
            evaluation_set = create_evaluation_set(db, 1, name="Editable", commit=False)
            case = create_evaluation_case(
                db,
                1,
                evaluation_set.id,
                title="Caso original",
                subject="Asunto original",
                body="Contenido original",
                expected_department_id=1,
                commit=False,
            )
            db.commit()

            update_evaluation_set(db, 1, evaluation_set.id, name="Editado", description="Nueva descripción")
            update_evaluation_case(
                db,
                1,
                evaluation_set.id,
                case.id,
                title="Caso actualizado",
                subject="Asunto actualizado",
                body="Contenido actualizado",
                expected_department_id=2,
                expected_requires_review=False,
                criticality="high",
            )
            update_evaluation_case(db, 1, evaluation_set.id, case.id, active=False)
            update_evaluation_set(db, 1, evaluation_set.id, active=False)
            self.assertFalse(db.get(RoutingEvaluationSet, evaluation_set.id).active)
            self.assertFalse(db.get(RoutingEvaluationCase, case.id).active)
            self.assertEqual(db.get(RoutingEvaluationCase, case.id).expected_department_id, 2)
            with self.assertRaises(ValueError):
                run_evaluation(db, 1, evaluation_set.id)

    def test_case_rejects_expected_department_from_other_tenant(self):
        with self.session_factory() as db:
            evaluation_set = create_evaluation_set(db, 1, name="Tenant guard", commit=False)
            with self.assertRaisesRegex(ValueError, "no pertenece"):
                create_evaluation_case(
                    db,
                    1,
                    evaluation_set.id,
                    title="Invalido",
                    subject="Asunto",
                    body="Cuerpo",
                    expected_department_id=3,
                    commit=False,
                )


if __name__ == "__main__":
    unittest.main()
