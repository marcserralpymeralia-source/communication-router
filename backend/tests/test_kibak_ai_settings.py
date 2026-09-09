from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.encryption import decrypt_secret, encrypt_secret
from app.db.database import Base
from app.db.models import AuditLog, BackgroundJob, Company, LLMSettings, PromptExecution, PromptTemplate, PromptVersion
from app.master.models import CompanyMembership
from app.routing.policy import load_routing_policy
from app.settings.kibak_ai import KibakAIConfigError, credential_configured, save_configuration, validate_api_key
from tests.test_setup_onboarding import SetupFixture


class KibakAISettingsTests(unittest.TestCase):
    def setUp(self):
        self.previous_app_slug = os.environ.get("APP_SLUG")
        os.environ["APP_SLUG"] = "kibak"
        from app.core.config import get_settings

        get_settings.cache_clear()
        self.fixture = SetupFixture()
        self.client, self.cleanup_client = self.fixture.client()
        login = self.client.post(
            "/login",
            data={"email": "admin@setup.local", "password": "setup-password"},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 303)

    def tearDown(self):
        self.cleanup_client()
        self.fixture.cleanup()
        if self.previous_app_slug is None:
            os.environ.pop("APP_SLUG", None)
        else:
            os.environ["APP_SLUG"] = self.previous_app_slug
        from app.core.config import get_settings

        get_settings.cache_clear()

    def _csrf(self) -> str:
        response = self.client.get("/settings/ai")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        self.assertIsNotNone(match)
        return match.group(1)

    def _save(self, csrf_token: str, *, api_key: str = "", **overrides):
        payload = {
            "csrf_token": csrf_token,
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "base_url": "",
            "temperature": "0.1",
            "max_tokens": "1200",
            "timeout_seconds": "60",
            "retries": "2",
            "api_key": api_key,
        }
        payload.update(overrides)
        return self.client.post("/settings/ai/save", data=payload, follow_redirects=False)

    def test_get_is_kibak_scoped_and_never_renders_ciphertext(self):
        csrf_token = self._csrf()
        self.assertTrue(csrf_token)
        response = self.client.get("/settings/ai")
        self.assertIn("Inteligencia Artificial", response.text)
        self.assertIn("communication_department_routing", response.text)
        self.assertIn("No configurada", response.text)
        self.assertNotIn("api_key_encrypted", response.text)

    def test_save_replace_empty_preserve_and_privacy(self):
        fake_key = "sk-kibak-test-secret-do-not-log"
        replacement = "sk-kibak-replacement-secret"
        with self.fixture.TenantSession() as db:
            settings = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
            assert settings is not None
            settings.auto_routing_enabled = True
            settings.simulation_mode = True
            settings.auto_forwarding_enabled = False
            db.commit()

        with patch("app.settings.integrations.call_openai") as provider:
            response = self._save(self._csrf(), api_key=fake_key)
            provider.assert_not_called()
        self.assertEqual(response.status_code, 303)

        with self.fixture.TenantSession() as db:
            settings = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
            assert settings is not None
            self.assertNotEqual(settings.api_key_encrypted, fake_key)
            self.assertEqual(decrypt_secret(settings.api_key_encrypted), fake_key)
            policy = load_routing_policy(db, 1)
            self.assertTrue(policy.auto_routing_enabled)
            self.assertTrue(policy.simulation_mode)
            self.assertFalse(policy.auto_forwarding_enabled)
            self.assertEqual(db.scalar(select(PromptExecution.id)), None)
            self.assertEqual(db.scalar(select(BackgroundJob.id)), None)
            audit_messages = [row.message for row in db.scalars(select(AuditLog)).all()]
            self.assertNotIn(fake_key, " ".join(audit_messages))

        page = self.client.get("/settings/ai")
        self.assertNotIn(fake_key, page.text)
        response = self._save(self._csrf())
        self.assertEqual(response.status_code, 303)
        with self.fixture.TenantSession() as db:
            settings = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
            assert settings is not None
            self.assertEqual(decrypt_secret(settings.api_key_encrypted), fake_key)

        response = self._save(self._csrf(), api_key=replacement)
        self.assertEqual(response.status_code, 303)
        with self.fixture.TenantSession() as db:
            settings = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
            assert settings is not None
            self.assertEqual(decrypt_secret(settings.api_key_encrypted), replacement)

    def test_operator_cannot_change_configuration(self):
        with self.fixture.MasterSession() as db:
            membership = db.scalar(select(CompanyMembership).where(CompanyMembership.company_id == 1))
            assert membership is not None
            membership.role_key = "Operador"
            db.commit()
        self.client.post("/logout", follow_redirects=False)
        self.client.post("/login", data={"email": "admin@setup.local", "password": "setup-password"}, follow_redirects=False)
        response = self.client.post("/settings/ai/save", data={"api_key": "sk-kibak-test-secret"})
        self.assertEqual(response.status_code, 403)

    def test_delete_pauses_agent_without_changing_policy(self):
        with self.fixture.TenantSession() as db:
            settings = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
            assert settings is not None
            settings.agent_enabled = True
            settings.api_key_encrypted = encrypt_secret("sk-kibak-test-secret")
            settings.auto_routing_enabled = True
            settings.simulation_mode = True
            settings.auto_forwarding_enabled = False
            db.commit()
        response = self.client.post("/settings/ai/credential/delete", data={"csrf_token": self._csrf()}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        with self.fixture.TenantSession() as db:
            settings = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
            assert settings is not None
            self.assertIsNone(settings.api_key_encrypted)
            self.assertFalse(settings.agent_enabled)
            policy = load_routing_policy(db, 1)
            self.assertTrue(policy.auto_routing_enabled)
            self.assertTrue(policy.simulation_mode)
            self.assertFalse(policy.auto_forwarding_enabled)

    def test_activation_requires_credential_and_prompt(self):
        response = self.client.post(
            "/settings/ai/activate",
            data={"csrf_token": self._csrf()},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("credencial", response.json()["message"])

    def test_csrf_and_tenant_context_protect_save(self):
        other_key = "sk-kibak-other-secret"
        with self.fixture.TenantSession() as db:
            db.add(Company(id=2, name="Otra compañía"))
            db.add(LLMSettings(company_id=2, api_key_encrypted=encrypt_secret(other_key)))
            db.commit()
        response = self.client.post(
            "/settings/ai/save",
            data={"csrf_token": "invalid-token", "api_key": "sk-kibak-test-secret-do-not-log"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        with self.fixture.TenantSession() as db:
            self.assertIsNone(db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1)).api_key_encrypted)
            other = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 2))
            assert other is not None
            self.assertEqual(decrypt_secret(other.api_key_encrypted), other_key)

    def test_activation_requires_admin_and_available_versioned_prompt(self):
        with self.fixture.TenantSession() as db:
            llm = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
            assert llm is not None
            llm.api_key_encrypted = encrypt_secret("sk-kibak-test-secret")
            llm.agent_enabled = False
            template = PromptTemplate(company_id=1, name="Routing", purpose="communication_department_routing")
            db.add(template)
            db.flush()
            version = PromptVersion(company_id=1, template_id=template.id, version=1, content="routing")
            db.add(version)
            db.flush()
            template.active_version_id = version.id
            db.commit()
        with patch("app.settings.integrations.call_openai") as provider:
            response = self.client.post(
                "/settings/ai/activate",
                data={"csrf_token": self._csrf()},
                headers={"Accept": "application/json"},
            )
            provider.assert_not_called()
        self.assertEqual(response.status_code, 200)
        with self.fixture.TenantSession() as db:
            settings = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
            assert settings is not None
            self.assertTrue(settings.agent_enabled)


class KibakAICredentialValidationTests(unittest.TestCase):
    def test_openai_key_validation_does_not_log_or_transform_input(self):
        value = "sk-kibak-test-secret-do-not-log"
        self.assertEqual(validate_api_key(value, "openai"), value)
        with self.assertRaises(KibakAIConfigError):
            validate_api_key("not-a-provider-key", "openai")

    def test_encryption_failure_is_controlled_and_does_not_include_key(self):
        fake_key = "sk-kibak-test-secret-do-not-log"
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as db:
                db.add(Company(id=1, name="KIBAK"))
                db.commit()
                with patch("app.settings.kibak_ai.encrypt_secret", side_effect=RuntimeError(fake_key)):
                    with self.assertRaises(KibakAIConfigError) as error:
                        save_configuration(
                            db=db,
                            company_id=1,
                            provider="openai",
                            model="gpt-5.6-luna",
                            base_url="",
                            temperature="0.1",
                            max_tokens="1200",
                            timeout_seconds="60",
                            retries="2",
                            api_key=fake_key,
                        )
        finally:
            engine.dispose()
        self.assertNotIn(fake_key, str(error.exception))

    def test_invalid_ciphertext_is_not_reported_as_configured(self):
        settings = LLMSettings(company_id=1, api_key_encrypted="not-a-fernet-token")
        self.assertFalse(credential_configured(settings))


if __name__ == "__main__":
    unittest.main()
