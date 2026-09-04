from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Company, LLMSettings, PromptExecution, PromptTemplate, PromptVersion
from app.routing.runtime import RoutingLLMRuntime
from app.routing.service import RoutingValidationError


class RoutingLLMRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add(Company(id=1, name="Tenant A"))
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    @staticmethod
    def _content(department_id=10):
        return json.dumps(
            {
                "proposed_department_id": department_id,
                "category": "consulta",
                "confidence": 0.94,
                "requires_review": False,
                "reason": "La intencion semantica corresponde al departamento.",
                "alternative_department_id": None,
                "ambiguity_reason": None,
            }
        )

    def test_loads_versioned_prompt_and_tenant_configuration_without_commit(self):
        calls = []

        def provider(settings, messages, model):
            calls.append((settings, messages, model))
            return {
                "ok": True,
                "content": self._content(),
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            }

        with self.session_factory() as db:
            db.add(
                LLMSettings(
                    company_id=1,
                    classification_model="tenant-routing-model",
                    temperature=0.23,
                    max_tokens=777,
                    timeout_seconds=9,
                    retries=1,
                )
            )
            db.flush()
            runtime = RoutingLLMRuntime(
                db,
                1,
                provider_call=provider,
                user_id=101,
                communication_id=55,
            )

            with patch.object(db, "commit") as commit:
                result = runtime.complete(system_prompt="legacy", user_prompt="contexto", output_schema={})

            commit.assert_not_called()
            self.assertEqual(result["validated_content"]["proposed_department_id"], 10)
            self.assertEqual(calls[0][2], "tenant-routing-model")
            self.assertIn("responsabilidades", calls[0][1][0]["content"])
            self.assertIn("RACI", calls[0][1][0]["content"])
            self.assertEqual(calls[0][1][1]["content"], "contexto")
            self.assertEqual(result["prompt_purpose"], "communication_department_routing")
            self.assertEqual(result["prompt_version"], 1)
            self.assertEqual(result["input_reference"], "communication:55")
            self.assertEqual(result["parameters"]["temperature"], 0.23)
            self.assertEqual(result["parameters"]["max_tokens"], 777)

            execution = db.scalar(select(PromptExecution))
            template = db.scalar(select(PromptTemplate))
            version = db.scalar(select(PromptVersion))
            self.assertIsNotNone(execution.started_at)
            self.assertIsNotNone(execution.finished_at)
            self.assertEqual(execution.prompt_template_id, template.id)
            self.assertEqual(execution.prompt_version, version.version)
            self.assertEqual(execution.output_status, "valid")
            self.assertEqual(execution.model, "tenant-routing-model")
            self.assertEqual(execution.input_reference, "communication:55")
            self.assertEqual(execution.input_tokens, 12)
            self.assertEqual(execution.output_tokens, 8)

    def test_active_prompt_version_can_change_without_service_code_change(self):
        prompts = []

        def provider(settings, messages, model):
            del settings, model
            prompts.append(messages[0]["content"])
            return {"ok": True, "content": self._content()}

        with self.session_factory() as db:
            runtime = RoutingLLMRuntime(db, 1, provider_call=provider)
            runtime.complete(system_prompt="ignored", user_prompt="first", output_schema={})
            template = db.scalar(select(PromptTemplate))
            first_version = db.get(PromptVersion, template.active_version_id)
            second_version = PromptVersion(
                company_id=1,
                template_id=template.id,
                version=first_version.version + 1,
                content="Prompt tenant version 2",
            )
            db.add(second_version)
            db.flush()
            template.active_version_id = second_version.id
            db.flush()
            runtime.complete(system_prompt="ignored", user_prompt="second", output_schema={})

            self.assertEqual(prompts[1], "Prompt tenant version 2")
            executions = db.scalars(select(PromptExecution).order_by(PromptExecution.id)).all()
            self.assertEqual([execution.prompt_version for execution in executions], [1, 2])

    def test_timeout_is_audited_and_does_not_store_provider_secret(self):
        def provider(settings, messages, model):
            del settings, messages, model
            raise TimeoutError("api_key=sk-live-secret timeout")

        with self.session_factory() as db:
            runtime = RoutingLLMRuntime(db, 1, provider_call=provider)
            with self.assertRaises(RoutingValidationError):
                runtime.complete(system_prompt="ignored", user_prompt="contexto", output_schema={})

            execution = db.scalar(select(PromptExecution))
            self.assertEqual(execution.output_status, "timeout")
            self.assertNotIn("sk-live-secret", execution.validation_errors_json or "")
            self.assertNotIn("sk-live-secret", execution.response_excerpt or "")

    def test_malformed_output_is_audited_as_invalid_json(self):
        def provider(settings, messages, model):
            del settings, messages, model
            return {"ok": True, "content": "this is not json"}

        with self.session_factory() as db:
            runtime = RoutingLLMRuntime(db, 1, provider_call=provider)
            with self.assertRaises(RoutingValidationError):
                runtime.complete(system_prompt="ignored", user_prompt="contexto", output_schema={})

            execution = db.scalar(select(PromptExecution))
            self.assertEqual(execution.output_status, "invalid_json")
            self.assertIn("JSON", execution.validation_errors_json or "")

    def test_disabled_tenant_does_not_call_provider(self):
        called = False

        def provider(settings, messages, model):
            nonlocal called
            called = True
            return {"ok": True, "content": self._content()}

        with self.session_factory() as db:
            db.add(LLMSettings(company_id=1, agent_enabled=False))
            db.flush()
            runtime = RoutingLLMRuntime(db, 1, provider_call=provider)
            with self.assertRaises(RoutingValidationError):
                runtime.complete(system_prompt="ignored", user_prompt="contexto", output_schema={})

            self.assertFalse(called)
            execution = db.scalar(select(PromptExecution))
            self.assertEqual(execution.output_status, "disabled")

    def test_missing_real_configuration_is_controlled(self):
        with patch("app.settings.integrations.call_openai", return_value={
            "ok": False,
            "error_type": "invalid_configuration",
            "message": "Falta API key de OpenAI.",
        }):
            with self.session_factory() as db:
                runtime = RoutingLLMRuntime(db, 1)
                with self.assertRaises(RoutingValidationError):
                    runtime.complete(system_prompt="ignored", user_prompt="contexto", output_schema={})

                execution = db.scalar(select(PromptExecution))
                settings = db.scalar(select(LLMSettings))
                self.assertEqual(execution.output_status, "invalid_configuration")
                self.assertIsNone(settings.api_key_encrypted)


if __name__ == "__main__":
    unittest.main()
