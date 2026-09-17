from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.encryption import decrypt_secret, encrypt_secret
from app.db.database import Base
from app.db.models import Company, Mailbox
from app.mailboxes.microsoft_oauth import (
    MICROSOFT_IMAP_SCOPE,
    MICROSOFT_OFFLINE_SCOPE,
    MicrosoftOAuthTokens,
    authenticate_microsoft_imap,
    build_microsoft_authorization_url,
    build_xoauth2_payload,
    microsoft_oauth_redirect_uri,
    microsoft_oauth_scopes,
    new_oauth_state,
)
from app.mailboxes.routes import microsoft_oauth_callback, microsoft_oauth_start
from app.master.service import TenantRole, TenantUser
from app.settings.email_config import email_config_status
from app.settings.integrations import test_imap_connection
from app.core.templating import templates


class FakeRequest:
    def __init__(self, session=None):
        self.session = session if session is not None else {}

    def url_for(self, name: str) -> str:
        return "http://localhost:8000/settings/mailboxes/oauth/microsoft/callback"


class FakeIMAPClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def authenticate(self, mechanism, callback):
        self.calls.append(("authenticate", mechanism, callback(b"challenge")))
        return "OK", [b"authenticated"]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"8"]

    def logout(self):
        self.calls.append(("logout",))


def admin_user(company_id: int = 1, user_id: int = 7, role: str = "Administrador") -> TenantUser:
    return TenantUser(
        id=user_id,
        email="admin@example.com",
        name="Admin",
        is_active=True,
        company_id=company_id,
        company_name="Tenant",
        company_slug="tenant",
        role=TenantRole(role),
        membership_id=11,
    )


class MicrosoftOAuthMailboxTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.settings = SimpleNamespace(
            microsoft_oauth_client_id="client-id",
            microsoft_oauth_client_secret="client-secret",
            microsoft_oauth_tenant_id="common",
            microsoft_oauth_redirect_uri="",
            microsoft_oauth_scopes=f"{MICROSOFT_IMAP_SCOPE} {MICROSOFT_OFFLINE_SCOPE} openid email",
            microsoft_oauth_timeout_seconds=5,
            app_url="http://localhost:8000",
        )

    def tearDown(self):
        self.engine.dispose()

    def add_mailbox(self, db, **kwargs):
        db.add(Company(id=1, name="Tenant", active=True))
        mailbox_data = {
            "company_id": 1,
            "name": "Outlook",
            "email_address": "quibac@example.com",
            "provider": "microsoft365",
            "connection_method": "oauth2",
            "imap_host": "outlook.office365.com",
            "imap_port": 993,
            "imap_security": "ssl_tls",
            "imap_username": "quibac@example.com",
            "enabled": False,
            "auto_sync_enabled": False,
            "smtp_enabled": False,
        }
        mailbox_data.update(kwargs)
        mailbox = Mailbox(**mailbox_data)
        db.add(mailbox)
        db.commit()
        db.refresh(mailbox)
        return mailbox

    def test_authorization_url_uses_imap_offline_scope_and_stable_callback(self):
        url = build_microsoft_authorization_url(
            state="state-value",
            redirect_uri="https://pilot.example.com/settings/mailboxes/oauth/microsoft/callback",
            login_hint="quibac@example.com",
            settings=self.settings,
        )
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["client_id"], ["client-id"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["redirect_uri"], ["https://pilot.example.com/settings/mailboxes/oauth/microsoft/callback"])
        self.assertIn(MICROSOFT_IMAP_SCOPE, query["scope"][0])
        self.assertIn(MICROSOFT_OFFLINE_SCOPE, query["scope"][0])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertNotIn("client-secret", url)

    def test_redirect_uri_uses_app_url(self):
        self.assertEqual(
            microsoft_oauth_redirect_uri(FakeRequest(), settings=self.settings),
            "http://localhost:8000/settings/mailboxes/oauth/microsoft/callback",
        )
        self.assertEqual(microsoft_oauth_scopes(self.settings)[0], MICROSOFT_IMAP_SCOPE)

    def test_callback_encrypts_refresh_token_and_keeps_mailbox_disabled(self):
        with self.Session() as db:
            mailbox = self.add_mailbox(db)
            state_data = new_oauth_state(company_id=1, mailbox_id=mailbox.id, user_id=7)
            request = FakeRequest({"microsoft_mailbox_oauth_state": state_data})
            with patch("app.mailboxes.routes.exchange_microsoft_authorization_code", return_value=MicrosoftOAuthTokens("access-token", "refresh-token")):
                response = microsoft_oauth_callback(request, code="authorization-code", state=state_data["state"], db=db, user=admin_user())
            self.assertEqual(response.status_code, 303)
            db.refresh(mailbox)
            self.assertEqual(decrypt_secret(mailbox.refresh_token_encrypted), "refresh-token")
            self.assertIsNone(mailbox.access_token_encrypted)
            self.assertIsNone(mailbox.imap_password_encrypted)
            self.assertFalse(mailbox.enabled)
            self.assertFalse(mailbox.auto_sync_enabled)
            self.assertFalse(mailbox.smtp_enabled)
            self.assertNotIn("refresh-token", response.headers["location"])

    def test_start_rejects_enabled_mailbox_and_non_admin(self):
        with self.Session() as db:
            mailbox = self.add_mailbox(db, enabled=True)
            response = microsoft_oauth_start(mailbox.id, FakeRequest(), db, admin_user(role="Supervisor"))
            self.assertEqual(response.status_code, 303)
            with patch("app.mailboxes.microsoft_oauth.get_settings", return_value=self.settings):
                response = microsoft_oauth_start(mailbox.id, FakeRequest(), db, admin_user())
            self.assertIn("Desactiva", response.headers["location"])

    def test_xoauth2_connection_is_readonly_and_does_not_use_password(self):
        with self.Session() as db:
            mailbox = self.add_mailbox(db, refresh_token_encrypted=encrypt_secret("refresh-token"))
            client = FakeIMAPClient()
            with patch("app.settings.integrations._imap_client", return_value=client), patch(
                "app.mailboxes.microsoft_oauth.refresh_microsoft_access_token", return_value="access-token"
            ):
                result = test_imap_connection(mailbox)
            self.assertTrue(result["ok"])
            self.assertEqual(result["found"], 8)
            self.assertEqual([call[0] for call in client.calls], ["authenticate", "select", "logout"])
            self.assertEqual(client.calls[0][1], "XOAUTH2")
            self.assertEqual(client.calls[1], ("select", "INBOX", True))

    def test_xoauth2_payload_contains_no_password(self):
        payload = build_xoauth2_payload("quibac@example.com", "access-token")
        self.assertEqual(payload, b"user=quibac@example.com\x01auth=Bearer access-token\x01\x01")
        self.assertNotIn(b"password", payload.lower())

    def test_email_status_treats_microsoft_oauth_as_imap_ready(self):
        settings = SimpleNamespace(
            connection_method="oauth2",
            provider="microsoft365",
            refresh_token_encrypted="ciphertext",
            imap_host="outlook.office365.com",
            imap_username="quibac@example.com",
            imap_password_encrypted=None,
            smtp_host=None,
            smtp_username=None,
            smtp_password_encrypted=None,
            from_email=None,
            smtp_enabled=False,
            smtp_provider="",
            last_imap_test_ok=None,
            last_imap_test_message=None,
            last_imap_test_at=None,
            last_smtp_test_ok=None,
            last_smtp_test_message=None,
            last_smtp_test_at=None,
            last_sync_ok=None,
            last_sync_message=None,
            last_sync_error=None,
            last_sync_at=None,
            last_sync_new=0,
            last_sync_duplicates=0,
        )
        self.assertTrue(email_config_status(settings)["imap_ready"])

    def test_mailbox_template_marks_connected_microsoft_oauth_credentials(self):
        template = templates.get_template("settings/mailboxes.html")
        request = SimpleNamespace(
            cookies={},
            url=SimpleNamespace(path="/settings/mailboxes"),
            state=SimpleNamespace(
                branding={
                    "favicon_url": "",
                    "show_logo_sidebar": False,
                    "logo_url": "",
                    "dark_logo_url": "",
                    "show_app_name_sidebar": True,
                    "app_name": "KIBAK",
                    "show_claim_sidebar": False,
                    "secondary_claim": "",
                    "company_name": "Tenant",
                },
                alert_center=SimpleNamespace(
                    has_critical=False,
                    high=False,
                    medium=False,
                    total=0,
                    critical=0,
                    low=0,
                    recent=[],
                ),
            ),
        )
        with patch.dict(templates.env.globals, {"branding_css_vars": lambda _branding: ""}):
            html = template.render(
                request=request,
                mailboxes=[
                    SimpleNamespace(
                        id=1,
                        name="Outlook",
                        email_address="quibac@example.com",
                        provider="microsoft365",
                        connection_method="oauth2",
                        refresh_token_encrypted="ciphertext",
                        imap_host="outlook.office365.com",
                        imap_username="quibac@example.com",
                        imap_password_encrypted=None,
                        enabled=False,
                        auto_sync_enabled=False,
                        last_imap_test_at=None,
                    )
                ],
                mailbox_states={},
                can_test=False,
                can_edit=False,
                app_settings=SimpleNamespace(is_pilot_runtime=True, app_slug="kibak", environment="staging"),
                user=SimpleNamespace(name="Admin", role=SimpleNamespace(name="Administrador")),
                message=None,
                error=None,
            )
        self.assertIn("<span>Credenciales</span><strong>Configuradas</strong>", html)

    def test_token_exchange_is_mockable_without_network_or_secret_logging(self):
        from app.mailboxes.microsoft_oauth import exchange_microsoft_authorization_code

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"access_token": "access-token", "refresh_token": "refresh-token"}).encode()

        with patch("app.mailboxes.microsoft_oauth.urllib.request.urlopen", return_value=Response()) as opener:
            tokens = exchange_microsoft_authorization_code("code", "http://localhost/callback", settings=self.settings)
        self.assertEqual(tokens, MicrosoftOAuthTokens("access-token", "refresh-token"))
        self.assertNotIn("client-secret", str(opener.call_args))


if __name__ == "__main__":
    unittest.main()
