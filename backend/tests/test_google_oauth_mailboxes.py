from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.encryption import decrypt_secret, encrypt_secret
from app.db.database import Base
from app.db.models import AuditLog, Company, Mailbox, User
from app.mailboxes.google_oauth import (
    GOOGLE_IMAP_SCOPE,
    GOOGLE_IDENTITY_SCOPE,
    GoogleOAuthError,
    GoogleOAuthTokens,
    authenticate_google_imap,
    build_google_authorization_url,
    build_xoauth2_payload,
    mailbox_oauth_status,
    new_oauth_state,
    refresh_google_access_token,
    google_oauth_redirect_uri,
)
from app.mailboxes.routes import google_oauth_callback, google_oauth_start
from app.master.service import TenantRole, TenantUser
from app.settings.integrations import test_imap_connection


class FakeRequest:
    def __init__(self, session=None):
        self.session = session if session is not None else {}

    def url_for(self, name: str) -> str:
        self.assert_name = name
        return "http://localhost:8000/settings/mailboxes/oauth/google/callback"


class FakeIMAPClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def authenticate(self, mechanism, callback):
        self.calls.append(("authenticate", mechanism, callback(b"challenge")))
        return "OK", [b"authenticated"]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"12"]

    def logout(self):
        self.calls.append(("logout",))


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


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


class GoogleOAuthMailboxTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.settings = SimpleNamespace(
            google_oauth_client_id="client-id",
            google_oauth_client_secret="client-secret",
            google_oauth_redirect_uri="",
            google_oauth_scopes=f"{GOOGLE_IMAP_SCOPE} {GOOGLE_IDENTITY_SCOPE}",
            google_oauth_timeout_seconds=5,
        )

    def tearDown(self):
        self.engine.dispose()

    def add_mailbox(self, db, **kwargs):
        db.add(Company(id=1, name="Tenant", active=True))
        mailbox_data = {
            "company_id": 1,
            "name": "Gmail",
            "email_address": "quibac@example.com",
            "provider": "gmail",
            "connection_method": "oauth2",
            "imap_host": "imap.gmail.com",
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

    def test_start_builds_tenant_bound_state_and_google_url_without_network(self):
        with self.Session() as db:
            mailbox = self.add_mailbox(db)
            request = FakeRequest()
            with patch("app.mailboxes.google_oauth.get_settings", return_value=self.settings):
                response = google_oauth_start(mailbox.id, request, db, admin_user())
            self.assertEqual(response.status_code, 307)
            location = response.headers["location"]
            query = parse_qs(urlparse(location).query)
            self.assertEqual(query["client_id"], ["client-id"])
            self.assertEqual(query["redirect_uri"], ["http://localhost:8000/settings/mailboxes/oauth/google/callback"])
            self.assertIn(GOOGLE_IMAP_SCOPE, query["scope"][0])
            self.assertEqual(request.session["google_mailbox_oauth_state"]["company_id"], 1)
            self.assertEqual(request.session["google_mailbox_oauth_state"]["mailbox_id"], mailbox.id)
            self.assertNotIn("client-secret", location)

    def test_staging_redirect_uses_public_app_url_behind_proxy(self):
        settings = SimpleNamespace(
            environment="staging",
            app_url="https://pilot.example.com/",
            google_oauth_redirect_uri="",
        )
        self.assertEqual(
            google_oauth_redirect_uri(FakeRequest(), settings=settings),
            "https://pilot.example.com/settings/mailboxes/oauth/google/callback",
        )

    def test_start_rejects_non_admin_and_enabled_mailbox(self):
        with self.Session() as db:
            mailbox = self.add_mailbox(db, enabled=True)
            response = google_oauth_start(mailbox.id, FakeRequest(), db, admin_user(role="Supervisor"))
            self.assertEqual(response.status_code, 303)
            with patch("app.mailboxes.google_oauth.get_settings", return_value=self.settings):
                response = google_oauth_start(mailbox.id, FakeRequest(), db, admin_user())
            self.assertIn("Desactiva", response.headers["location"])

    def test_callback_persists_only_encrypted_refresh_token_and_keeps_mailbox_disabled(self):
        with self.Session() as db:
            mailbox = self.add_mailbox(db, imap_password_encrypted=encrypt_secret("old-password"))
            state_data = new_oauth_state(company_id=1, mailbox_id=mailbox.id, user_id=7)
            request = FakeRequest({"google_mailbox_oauth_state": state_data})
            state = request.session["google_mailbox_oauth_state"]["state"]
            with patch("app.mailboxes.routes.exchange_google_authorization_code", return_value=GoogleOAuthTokens("access-token", "refresh-token")), patch(
                "app.mailboxes.routes.fetch_google_account_email", return_value="quibac@example.com"
            ), patch("app.mailboxes.google_oauth.get_settings", return_value=self.settings):
                response = google_oauth_callback(request, code="authorization-code", state=state, db=db, user=admin_user())
            self.assertEqual(response.status_code, 303)
            db.refresh(mailbox)
            self.assertEqual(mailbox.connection_method, "oauth2")
            self.assertEqual(mailbox.provider, "gmail")
            self.assertEqual(mailbox.connected_email, "quibac@example.com")
            self.assertEqual(decrypt_secret(mailbox.refresh_token_encrypted), "refresh-token")
            self.assertIsNone(mailbox.access_token_encrypted)
            self.assertIsNone(mailbox.imap_password_encrypted)
            self.assertFalse(mailbox.enabled)
            self.assertFalse(mailbox.auto_sync_enabled)
            self.assertFalse(mailbox.smtp_enabled)
            self.assertEqual(mailbox_oauth_status(mailbox), "connected")
            self.assertNotIn("refresh-token", response.headers["location"])
            self.assertIsNone(request.session.get("google_mailbox_oauth_state"))
            audit = db.scalar(select(AuditLog).where(AuditLog.action == "settings.mailbox.oauth.google.connected"))
            self.assertIsNotNone(audit)
            self.assertNotIn("access-token", audit.message)
            self.assertNotIn("refresh-token", audit.message)

    def test_callback_state_is_single_use_and_cross_tenant_is_rejected(self):
        with self.Session() as db:
            mailbox = self.add_mailbox(db)
            state_data = new_oauth_state(company_id=2, mailbox_id=mailbox.id, user_id=7)
            request = FakeRequest({"google_mailbox_oauth_state": state_data})
            with patch("app.mailboxes.routes.exchange_google_authorization_code") as exchange:
                response = google_oauth_callback(request, code="code", state=state_data["state"], db=db, user=admin_user())
            self.assertEqual(response.status_code, 303)
            exchange.assert_not_called()
            self.assertIsNone(request.session.get("google_mailbox_oauth_state"))

    def test_callback_rejects_expired_state(self):
        with self.Session() as db:
            mailbox = self.add_mailbox(db)
            state_data = new_oauth_state(company_id=1, mailbox_id=mailbox.id, user_id=7)
            state_data["expires_at"] = 1
            request = FakeRequest({"google_mailbox_oauth_state": state_data})
            with patch("app.mailboxes.routes.exchange_google_authorization_code") as exchange:
                response = google_oauth_callback(request, code="code", state=state_data["state"], db=db, user=admin_user())
            self.assertEqual(response.status_code, 303)
            exchange.assert_not_called()

    def test_xoauth2_authentication_selects_readonly_and_does_not_use_password(self):
        with self.Session() as db:
            mailbox = self.add_mailbox(db, refresh_token_encrypted=encrypt_secret("refresh-token"))
            client = FakeIMAPClient()
            with patch("app.settings.integrations._imap_client", return_value=client), patch(
                "app.settings.integrations.authenticate_google_imap", wraps=authenticate_google_imap
            ) as authenticate, patch("app.mailboxes.google_oauth.refresh_google_access_token", return_value="access-token"):
                result = test_imap_connection(mailbox)
            self.assertTrue(result["ok"])
            self.assertEqual(result["found"], 12)
            self.assertEqual([call[0] for call in client.calls], ["authenticate", "select", "logout"])
            self.assertEqual(client.calls[0][1], "XOAUTH2")
            self.assertEqual(client.calls[1], ("select", "INBOX", True))
            authenticate.assert_called_once()

    def test_xoauth2_payload_contains_no_password_auth_and_refresh_errors_are_controlled(self):
        payload = build_xoauth2_payload("quibac@example.com", "access-token")
        self.assertEqual(payload, b"user=quibac@example.com\x01auth=Bearer access-token\x01\x01")
        self.assertNotIn(b"password", payload.lower())
        with patch("app.mailboxes.google_oauth._post_form", return_value={"access_token": "access-token"}) as post:
            token = refresh_google_access_token(encrypt_secret("refresh-token"), settings=self.settings)
        self.assertEqual(token, "access-token")
        self.assertNotIn("refresh-token", post.call_args.kwargs)
        with self.assertRaises(GoogleOAuthError) as missing:
            refresh_google_access_token(None, settings=self.settings)
        self.assertEqual(missing.exception.error_type, "oauth_authorization_required")

    def test_token_exchange_is_server_side_and_requires_refresh_token(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeHTTPResponse({"access_token": "access-token", "refresh_token": "refresh-token"})

        from app.mailboxes.google_oauth import exchange_google_authorization_code

        tokens = exchange_google_authorization_code(
            "authorization-code",
            "http://localhost/callback",
            settings=self.settings,
            opener=opener,
        )
        self.assertEqual(tokens, GoogleOAuthTokens("access-token", "refresh-token"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0].full_url, "https://oauth2.googleapis.com/token")
        self.assertEqual(requests[0][1], 5)
        self.assertIn(b"grant_type=authorization_code", requests[0][0].data)

    def test_provider_timeout_and_revocation_are_controlled(self):
        def timeout_opener(_request, timeout):
            self.assertEqual(timeout, 5)
            raise TimeoutError("provider timeout")

        from app.mailboxes.google_oauth import exchange_google_authorization_code

        with self.assertRaisesRegex(GoogleOAuthError, "No se pudo contactar") as timeout_error:
            exchange_google_authorization_code(
                "authorization-code",
                "http://localhost/callback",
                settings=self.settings,
                opener=timeout_opener,
            )
        self.assertEqual(timeout_error.exception.error_type, "provider_error")

        def revoked_post(*_args, **_kwargs):
            raise GoogleOAuthError("oauth_revoked", "safe revocation message")

        with patch("app.mailboxes.google_oauth._post_form", side_effect=revoked_post):
            with self.assertRaises(GoogleOAuthError) as revoked:
                refresh_google_access_token(encrypt_secret("refresh-token"), settings=self.settings)
        self.assertEqual(revoked.exception.error_type, "oauth_revoked")

    def test_callback_rejects_google_identity_mismatch_without_persisting_tokens(self):
        with self.Session() as db:
            mailbox = self.add_mailbox(db)
            state_data = new_oauth_state(company_id=1, mailbox_id=mailbox.id, user_id=7)
            request = FakeRequest({"google_mailbox_oauth_state": state_data})
            with patch("app.mailboxes.routes.exchange_google_authorization_code", return_value=GoogleOAuthTokens("access-token", "refresh-token")), patch(
                "app.mailboxes.routes.fetch_google_account_email", return_value="different@example.com"
            ):
                response = google_oauth_callback(request, code="code", state=state_data["state"], db=db, user=admin_user())
            self.assertEqual(response.status_code, 303)
            db.refresh(mailbox)
            self.assertIsNone(mailbox.refresh_token_encrypted)
            self.assertIsNone(mailbox.access_token_encrypted)


if __name__ == "__main__":
    unittest.main()
