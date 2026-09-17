"""Google OAuth helpers for tenant-scoped IMAP mailboxes."""

from __future__ import annotations

import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.core.config import get_settings
from app.core.encryption import decrypt_secret, encrypt_secret


logger = logging.getLogger(__name__)

GOOGLE_OAUTH_PROVIDER = "google"
GOOGLE_IMAP_SCOPE = "https://mail.google.com/"
GOOGLE_IDENTITY_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_OAUTH_STATE_SESSION_KEY = "google_mailbox_oauth_state"
GOOGLE_OAUTH_STATE_TTL_SECONDS = 600


class GoogleOAuthError(RuntimeError):
    """Controlled OAuth failure without provider payloads or secrets."""

    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GoogleOAuthTokens:
    access_token: str
    refresh_token: str | None = None


def google_oauth_scopes(settings=None) -> tuple[str, ...]:
    configured = str(getattr(settings or get_settings(), "google_oauth_scopes", "") or "")
    values = [item.strip() for item in configured.replace(",", " ").split() if item.strip()]
    scopes = list(dict.fromkeys(values or [GOOGLE_IMAP_SCOPE, GOOGLE_IDENTITY_SCOPE]))
    if GOOGLE_IMAP_SCOPE not in scopes:
        scopes.insert(0, GOOGLE_IMAP_SCOPE)
    if GOOGLE_IDENTITY_SCOPE not in scopes:
        scopes.append(GOOGLE_IDENTITY_SCOPE)
    return tuple(scopes)


def google_oauth_redirect_uri(request, settings=None) -> str:
    runtime_settings = settings or get_settings()
    configured = str(getattr(runtime_settings, "google_oauth_redirect_uri", "") or "").strip()
    if configured:
        return configured
    if str(getattr(runtime_settings, "environment", "") or "").strip().lower() in {"staging", "production"}:
        app_url = str(getattr(runtime_settings, "app_url", "") or "").strip().rstrip("/")
        if app_url:
            return f"{app_url}/settings/mailboxes/oauth/google/callback"
    return str(request.url_for("google_mailbox_oauth_callback"))


def google_oauth_configured(settings=None) -> bool:
    runtime_settings = settings or get_settings()
    return bool(
        str(getattr(runtime_settings, "google_oauth_client_id", "") or "").strip()
        and str(getattr(runtime_settings, "google_oauth_client_secret", "") or "").strip()
    )


def build_google_authorization_url(*, state: str, redirect_uri: str, login_hint: str | None = None, settings=None) -> str:
    runtime_settings = settings or get_settings()
    client_id = str(getattr(runtime_settings, "google_oauth_client_id", "") or "").strip()
    if not client_id:
        raise GoogleOAuthError("oauth_not_configured", "La conexión Google OAuth no está configurada en KIBAK.")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(google_oauth_scopes(runtime_settings)),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urllib.parse.urlencode(params)}"


def _json_response(response) -> dict[str, Any]:
    raw = response.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoogleOAuthError("provider_error", "Google devolvió una respuesta no válida.") from exc
    return payload if isinstance(payload, dict) else {}


def _provider_error(error: Exception, *, phase: str) -> GoogleOAuthError:
    if isinstance(error, GoogleOAuthError):
        return error
    if isinstance(error, urllib.error.HTTPError):
        try:
            payload = _json_response(error)
        except GoogleOAuthError:
            payload = {}
        provider_code = str(payload.get("error") or "").strip().lower()
        if provider_code == "invalid_grant":
            return GoogleOAuthError("oauth_revoked", "La autorización de Google ha caducado o ha sido revocada.")
        return GoogleOAuthError("oauth_refresh_failed" if phase == "refresh" else "provider_error", "Google no pudo completar la autorización.")
    if isinstance(error, (TimeoutError, urllib.error.URLError)):
        return GoogleOAuthError("provider_error", "No se pudo contactar con Google.")
    return GoogleOAuthError("provider_error", "Google no pudo completar la autorización.")


def _post_form(
    url: str,
    values: dict[str, str],
    *,
    timeout: int,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(values).encode("utf-8"),
        headers={"Accept": "application/json"},
        method="POST",
    )
    try:
        open_fn = opener or urllib.request.urlopen
        with open_fn(request, timeout=timeout) as response:
            return _json_response(response)
    except Exception as exc:  # noqa: BLE001
        raise _provider_error(exc, phase="exchange") from exc


def exchange_google_authorization_code(code: str, redirect_uri: str, *, settings=None, opener=None) -> GoogleOAuthTokens:
    runtime_settings = settings or get_settings()
    client_id = str(getattr(runtime_settings, "google_oauth_client_id", "") or "").strip()
    client_secret = str(getattr(runtime_settings, "google_oauth_client_secret", "") or "").strip()
    if not client_id or not client_secret:
        raise GoogleOAuthError("oauth_not_configured", "La conexión Google OAuth no está configurada en KIBAK.")
    if not code.strip():
        raise GoogleOAuthError("oauth_authorization_required", "Google no devolvió un código de autorización.")
    payload = _post_form(
        GOOGLE_TOKEN_ENDPOINT,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=max(int(getattr(runtime_settings, "google_oauth_timeout_seconds", 20) or 20), 1),
        opener=opener,
    )
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip() or None
    if not access_token:
        raise GoogleOAuthError("provider_error", "Google no devolvió un token de acceso válido.")
    if not refresh_token:
        raise GoogleOAuthError("oauth_authorization_required", "Google no devolvió un refresh token. Vuelve a autorizar la cuenta.")
    return GoogleOAuthTokens(access_token=access_token, refresh_token=refresh_token)


def fetch_google_account_email(access_token: str, *, settings=None, opener=None) -> str:
    runtime_settings = settings or get_settings()
    request = urllib.request.Request(
        GOOGLE_USERINFO_ENDPOINT,
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        open_fn = opener or urllib.request.urlopen
        with open_fn(request, timeout=max(int(getattr(runtime_settings, "google_oauth_timeout_seconds", 20) or 20), 1)) as response:
            payload = _json_response(response)
    except Exception as exc:  # noqa: BLE001
        raise _provider_error(exc, phase="identity") from exc
    email = str(payload.get("email") or "").strip().lower()
    if "@" not in email:
        raise GoogleOAuthError("provider_error", "Google no devolvió una identidad de cuenta válida.")
    return email


def refresh_google_access_token(refresh_token_encrypted: str | None, *, settings=None, opener=None) -> str:
    runtime_settings = settings or get_settings()
    refresh_token = decrypt_secret(refresh_token_encrypted)
    if not refresh_token:
        raise GoogleOAuthError("oauth_authorization_required", "El buzón todavía no está conectado con Google.")
    client_id = str(getattr(runtime_settings, "google_oauth_client_id", "") or "").strip()
    client_secret = str(getattr(runtime_settings, "google_oauth_client_secret", "") or "").strip()
    if not client_id or not client_secret:
        raise GoogleOAuthError("oauth_not_configured", "La conexión Google OAuth no está configurada en KIBAK.")
    try:
        payload = _post_form(
            GOOGLE_TOKEN_ENDPOINT,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=max(int(getattr(runtime_settings, "google_oauth_timeout_seconds", 20) or 20), 1),
            opener=opener,
        )
    except GoogleOAuthError as exc:
        if exc.error_type == "oauth_revoked":
            raise
        raise GoogleOAuthError("oauth_refresh_failed", str(exc)) from exc
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise GoogleOAuthError("oauth_refresh_failed", "Google no devolvió un token de acceso válido.")
    return access_token


def build_xoauth2_payload(username: str, access_token: str) -> bytes:
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


def authenticate_google_imap(client, *, username: str, refresh_token_encrypted: str | None, settings=None) -> tuple[str, list[bytes]]:
    access_token = refresh_google_access_token(refresh_token_encrypted, settings=settings)
    try:
        status, data = client.authenticate("XOAUTH2", lambda _challenge: build_xoauth2_payload(username, access_token))
    except Exception as exc:  # noqa: BLE001
        raise GoogleOAuthError("imap_authentication_failed", "Google rechazó la autenticación IMAP OAuth.") from exc
    if status != "OK":
        raise GoogleOAuthError("imap_authentication_failed", "Google rechazó la autenticación IMAP OAuth.")
    return status, data


def mailbox_oauth_provider(mailbox) -> str | None:
    if (getattr(mailbox, "connection_method", "password") or "password").strip().lower() != "oauth2":
        return None
    provider = (getattr(mailbox, "provider", "") or "").strip().lower()
    if provider == "gmail":
        return GOOGLE_OAUTH_PROVIDER
    if provider == "microsoft365":
        return "microsoft365"
    return None


def mailbox_oauth_status(mailbox) -> str:
    if mailbox_oauth_provider(mailbox) not in {GOOGLE_OAUTH_PROVIDER, "microsoft365"}:
        return "not_connected"
    if decrypt_secret(getattr(mailbox, "refresh_token_encrypted", None)):
        return "connected"
    return "not_connected"


def new_oauth_state(*, company_id: int, mailbox_id: int, user_id: int) -> dict[str, Any]:
    return {
        "state": secrets.token_urlsafe(32),
        "company_id": int(company_id),
        "mailbox_id": int(mailbox_id),
        "user_id": int(user_id),
        "expires_at": int(datetime.now(timezone.utc).timestamp()) + GOOGLE_OAUTH_STATE_TTL_SECONDS,
    }


def encrypt_refresh_token(refresh_token: str) -> str:
    return encrypt_secret(refresh_token) or ""
