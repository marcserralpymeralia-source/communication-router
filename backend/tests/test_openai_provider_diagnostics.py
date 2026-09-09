from __future__ import annotations

import io
import json
import socket
import unittest
import urllib.error
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Company, LLMSettings, PromptExecution
from app.routing.runtime import RoutingLLMRuntime
from app.routing.service import RoutingValidationError
from app.settings.integrations import call_openai


class _Response:
    def __init__(self, payload: str):
        self.payload = payload.encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class OpenAIProviderDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.settings = LLMSettings(
            company_id=1,
            provider="openai",
            base_url="https://api.openai.com/v1",
            api_key_encrypted="ciphertext",
            classification_model="gpt-5.6-luna",
            temperature=0.1,
            max_tokens=4000,
            timeout_seconds=7,
            retries=0,
        )

    def _call(self, opener, model="gpt-5.6-luna"):
        with patch("app.settings.integrations.decrypt_secret", return_value="fake-test-key"):
            with patch("app.settings.integrations.urllib.request.urlopen", side_effect=opener) as urlopen:
                return call_openai(
                    self.settings,
                    [{"role": "user", "content": "texto ficticio"}],
                    model,
                ), urlopen

    def _http_error(self, status, *, code="provider_code", param="model"):
        body = json.dumps({"error": {"code": code, "param": param, "message": "secret body must not persist"}})
        return urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions",
            status,
            "provider error",
            {"x-request-id": "req-test-123"},
            io.BytesIO(body.encode()),
        )

    def test_chat_completions_payload_uses_provider_base_url_without_key_in_url(self):
        opener = lambda _request, timeout: _Response(
            json.dumps(
                {
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 4},
                }
            )
        )
        result, urlopen = self._call(opener)

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/chat/completions")
        self.assertNotIn("fake-test-key", request.full_url)
        self.assertNotIn("fake-test-key", request.data.decode())
        self.assertNotIn("authorization", request.data.decode().lower())
        self.assertNotIn("api_key", payload)
        self.assertEqual(payload["model"], "gpt-5.6-luna")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "texto ficticio"}])
        self.assertEqual(payload["temperature"], 0.1)
        self.assertEqual(payload["max_completion_tokens"], 4000)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("max_output_tokens", payload)
        self.assertNotIn("response_format", payload)
        self.assertTrue(result["ok"])

    def test_legacy_chat_model_keeps_max_tokens_compatibility(self):
        opener = lambda _request, timeout: _Response(
            json.dumps({"choices": [{"message": {"content": "{}"}}]})
        )
        result, urlopen = self._call(opener, model="gpt-4.1")

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertTrue(result["ok"])
        self.assertEqual(payload["max_tokens"], 4000)
        self.assertNotIn("max_completion_tokens", payload)

    def test_http_statuses_are_classified_without_provider_body(self):
        expected = {
            400: "invalid_request",
            401: "authentication_error",
            403: "permission_error",
            404: "model_not_available",
            429: "rate_limit",
        }
        for status, category in expected.items():
            with self.subTest(status=status):
                result, _ = self._call(lambda _request, **_kwargs: (_ for _ in ()).throw(self._http_error(status)))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_type"], category)
                self.assertEqual(result["diagnostics"]["status_code"], status)
                self.assertEqual(result["diagnostics"]["request_id"], "req-test-123")
                self.assertEqual(result["diagnostics"]["provider_code"], "provider_code")
                self.assertNotIn("secret body", json.dumps(result))

    def test_unsupported_parameter_is_invalid_request(self):
        error = self._http_error(400, code="unsupported_parameter", param="max_tokens")
        result, http = self._call(lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
        self.assertEqual(http.call_count, 1)
        self.assertEqual(result["error_type"], "invalid_request")
        for key, value in {
            "status_code": 400,
            "provider_code": "unsupported_parameter",
            "provider_param": "max_tokens",
            "phase": "http_response",
            "exception_class": "HTTPError",
        }.items():
            self.assertEqual(result["diagnostics"][key], value)
        self.assertNotIn("secret body", json.dumps(result))

    def test_timeout_and_response_parsing_errors_are_classified(self):
        timeout_result, _ = self._call(lambda _request, **_kwargs: (_ for _ in ()).throw(socket.timeout("secret timeout")))
        self.assertEqual(timeout_result["error_type"], "timeout")
        self.assertEqual(timeout_result["diagnostics"]["phase"], "transport")

        invalid_json_result, _ = self._call(lambda _request, **_kwargs: _Response("not-json"))
        self.assertEqual(invalid_json_result["error_type"], "invalid_json")
        self.assertEqual(invalid_json_result["diagnostics"]["phase"], "response_parse")

        malformed_result, _ = self._call(lambda _request, **_kwargs: _Response(json.dumps({"choices": []})))
        self.assertEqual(malformed_result["error_type"], "provider_error")
        self.assertEqual(malformed_result["diagnostics"]["phase"], "response_parse")


class FailedPromptExecutionAuditTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            db.add(Company(id=1, name="Tenant A"))
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def test_provider_failure_is_audited_and_details_are_sanitized(self):
        def provider(settings, messages, model):
            del settings, messages, model
            return {
                "ok": False,
                "error_type": "authentication_error",
                "message": "Authorization: Bearer sk-live-secret",
                "diagnostics": {
                    "category": "authentication_error",
                    "exception_class": "HTTPError",
                    "phase": "http_response",
                    "status_code": 401,
                    "provider_code": "invalid_api_key",
                    "provider_param": None,
                    "request_id": "req-test-123",
                },
            }

        with self.session_factory() as db:
            runtime = RoutingLLMRuntime(db, 1, provider_call=provider)
            with self.assertRaises(RoutingValidationError) as raised:
                runtime.complete(system_prompt="ignored", user_prompt="contexto", output_schema={})

            execution = db.scalar(select(PromptExecution))
            audit = json.loads(execution.validation_errors_json)
            self.assertEqual(execution.output_status, "authentication_error")
            self.assertEqual(audit["diagnostics"]["status_code"], 401)
            self.assertEqual(audit["diagnostics"]["request_id"], "req-test-123")
            self.assertEqual(raised.exception.error_type, "authentication_error")
            self.assertEqual(raised.exception.details["provider_code"], "invalid_api_key")
            self.assertNotIn("sk-live-secret", execution.validation_errors_json)
            self.assertNotIn("sk-live-secret", execution.response_excerpt or "")
            execution_id = execution.id
            db.commit()

        with self.session_factory() as db:
            execution = db.get(PromptExecution, execution_id)
            self.assertEqual(execution.output_status, "authentication_error")
            self.assertNotIn("sk-live-secret", execution.validation_errors_json)


if __name__ == "__main__":
    unittest.main()
