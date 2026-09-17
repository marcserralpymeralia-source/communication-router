"""Microsoft OAuth helpers for tenant-scoped IMAP mailboxes."""

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
from app.core.encryption import encrypt_secret, decrypt_secret


logger = logging.getLogger(__name__)

MICROSOFT_OAUTH_PROVIDER = "microsoft365"
MICROSOFT_IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
MICROSOFT_OFFLINE_SCOPE = "offline_access"
MICROSOFT_AUTHORIZATION_BASE = "https://login.microsoftonline.com"
MICROSOFT_OAUTH_STATE_SESSION_KEY = "microsoft_mailbox_oauth_state"
MICROSOFT_OAUTH_STATE_TTL_SECONDS = 600


class MicrosoftOAuthError(RuntimeError):
    """Controlled OAuth failure without provider payloads or secrets."""

    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class MicrosoftOAuthTokens:
    access_token: str
    refresh_token: str | None = None


def microsoft_oauth_scopes(settings=None) -> tuple[str, ...]:
    configured = str(getattr(settings or get_settings(), "microsoft_oauth_scopes", "") or "")
    values = [item.strip() for item in configured.replace(",", " ").split() if item.strip()]
    scopes = list(dict.fromkeys(values or [MICROSOFT_IMAP_SCOPE, MICROSOFT_OFFLINE_SCOPE, "openid", "email"]))
    if MICROSOFT_IMAP_SCOPE not in scopes:
        scopes.insert(0, MICROSOFT_IMAP_SCOPE)
    if MICROSOFT_OFFLINE_SCOPE not in scopes:
        scopes.append(MICROSOFT_OFFLINE_SCOPE)
    return tuple(scopes)


def _tenant_id(settings=None) -> str:
    return str(getattr(settings or get_settings(), "microsoft_oauth_tenant_id", "common") or "common").strip() or "common"


def microsoft_oauth_redirect_uri(request, settings=None) -> str:
    runtime_settings = settings or get_settings()
    configured = str(getattr(runtime_settings, "microsoft_oauth_redirect_uri", "") or "").strip()
    if configured:
        return configured
    app_url = str(getattr(runtime_settings, "app_url", "") or "").strip().rstrip("/")
    if app_url:
        return f"{app_url}/settings/mailboxes/oauth/microsoft/callback"
    return str(request.url_for("microsoft_mailbox_oauth_callback"))


def microsoft_oauth_configured(settings=None) -> bool:
    runtime_settings = settings or get_settings()
    return bool(
        str(getattr(runtime_settings, "microsoft_oauth_client_id", "") or "").strip()
        and str(getattr(runtime_settings, "microsoft_oauth_client_secret", "") or "").strip()
    )


def _authorization_endpoint(settings=None) -> str:
    return f"{MICROSOFT_AUTHORIZATION_BASE}/{_tenant_id(settings)}/oauth2/v2.0/authorize"


def _token_endpoint(settings=None) -> str:
    return f"{MICROSOFT_AUTHORIZATION_BASE}/{_tenant_id(settings)}/oauth2/v2.0/token"


def build_microsoft_authorization_url(*, state: str, redirect_uri: str, login_hint: str | None = None, settings=None) -> str:
    runtime_settings = settings or get_settings()
    client_id = str(getattr(runtime_settings, "microsoft_oauth_client_id", "") or "").strip()
    if not client_id:
        raise MicrosoftOAuthError("oauth_not_configured", "La conexión Microsoft OAuth no está configurada en KIBAK.")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "response_mode": "query",
        "scope": " ".join(microsoft_oauth_scopes(runtime_settings)),
        "state": state,
        "prompt": "consent",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{_authorization_endpoint(runtime_settings)}?{urllib.parse.urlencode(params)}"


def _json_response(response) -> dict[str, Any]:
    raw = response.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MicrosoftOAuthError("provider_error", "Microsoft devolvió una respuesta no válida.") from exc
    return payload if isinstance(payload, dict) else {}


def _provider_error(error: Exception, *, phase: str) -> MicrosoftOAuthError:
    if isinstance(error, MicrosoftOAuthError):
        return error
    if isinstance(error, urllib.error.HTTPError):
        try:
            payload = _json_response(error)
        except MicrosoftOAuthError:
            payload = {}
        if str(payload.get("error") or "").strip().lower() == "invalid_grant":
            return MicrosoftOAuthError("oauth_revoked", "La autorización de Microsoft ha caducado o ha sido revocada.")
        return MicrosoftOAuthError("oauth_refresh_failed" if phase == "refresh" else "provider_error", "Microsoft no pudo completar la autorización.")
    if isinstance(error, (TimeoutError, urllib.error.URLError)):
        return MicrosoftOAuthError("provider_error", "No se pudo contactar con Microsoft.")
    return MicrosoftOAuthError("provider_error", "Microsoft no pudo completar la autorización.")


def _post_form(url: str, values: dict[str, str], *, timeout: int, opener: Callable[..., Any] | None = None) -> dict[str, Any]:
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


def exchange_microsoft_authorization_code(code: str, redirect_uri: str, *, settings=None, opener=None) -> MicrosoftOAuthTokens:
    runtime_settings = settings or get_settings()
    client_id = str(getattr(runtime_settings, "microsoft_oauth_client_id", "") or "").strip()
    client_secret = str(getattr(runtime_settings, "microsoft_oauth_client_secret", "") or "").strip()
    if not client_id or not client_secret:
        raise MicrosoftOAuthError("oauth_not_configured", "La conexión Microsoft OAuth no está configurada en KIBAK.")
    if not code.strip():
        raise MicrosoftOAuthError("oauth_authorization_required", "Microsoft no devolvió un código de autorización.")
    payload = _post_form(
        _token_endpoint(runtime_settings),
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "scope": " ".join(microsoft_oauth_scopes(runtime_settings)),
        },
        timeout=max(int(getattr(runtime_settings, "microsoft_oauth_timeout_seconds", 20) or 20), 1),
        opener=opener,
    )
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip() or None
    if not access_token:
        raise MicrosoftOAuthError("provider_error", "Microsoft no devolvió un token de acceso válido.")
    if not refresh_token:
        raise MicrosoftOAuthError("oauth_authorization_required", "Microsoft no devolvió un refresh token. Vuelve a autorizar la cuenta.")
    return MicrosoftOAuthTokens(access_token=access_token, refresh_token=refresh_token)


def refresh_microsoft_access_token(refresh_token_encrypted: str | None, *, settings=None, opener=None) -> str:
    runtime_settings = settings or get_settings()
    refresh_token = decrypt_secret(refresh_token_encrypted)
    if not refresh_token:
        raise MicrosoftOAuthError("oauth_authorization_required", "El buzón todavía no está conectado con Microsoft.")
    client_id = str(getattr(runtime_settings, "microsoft_oauth_client_id", "") or "").strip()
    client_secret = str(getattr(runtime_settings, "microsoft_oauth_client_secret", "") or "").strip()
    if not client_id or not client_secret:
        raise MicrosoftOAuthError("oauth_not_configured", "La conexión Microsoft OAuth no está configurada en KIBAK.")
    try:
        payload = _post_form(
            _token_endpoint(runtime_settings),
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": " ".join(microsoft_oauth_scopes(runtime_settings)),
            },
            timeout=max(int(getattr(runtime_settings, "microsoft_oauth_timeout_seconds", 20) or 20), 1),
            opener=opener,
        )
    except MicrosoftOAuthError as exc:
        if exc.error_type == "oauth_revoked":
            raise
        raise MicrosoftOAuthError("oauth_refresh_failed", str(exc)) from exc
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise MicrosoftOAuthError("oauth_refresh_failed", "Microsoft no devolvió un token de acceso válido.")
    return access_token


def build_xoauth2_payload(username: str, access_token: str) -> bytes:
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


def authenticate_microsoft_imap(client, *, username: str, refresh_token_encrypted: str | None, settings=None) -> tuple[str, list[bytes]]:
    access_token = refresh_microsoft_access_token(refresh_token_encrypted, settings=settings)
    try:
        status, data = client.authenticate("XOAUTH2", lambda _challenge: build_xoauth2_payload(username, access_token))
    except Exception as exc:  # noqa: BLE001
        raise MicrosoftOAuthError("imap_authentication_failed", "Microsoft rechazó la autenticación IMAP OAuth.") from exc
    if status != "OK":
        raise MicrosoftOAuthError("imap_authentication_failed", "Microsoft rechazó la autenticación IMAP OAuth.")
    return status, data


def new_oauth_state(*, company_id: int, mailbox_id: int, user_id: int) -> dict[str, Any]:
    return {
        "state": secrets.token_urlsafe(32),
        "company_id": int(company_id),
        "mailbox_id": int(mailbox_id),
        "user_id": int(user_id),
        "expires_at": int(datetime.now(timezone.utc).timestamp()) + MICROSOFT_OAUTH_STATE_TTL_SECONDS,
    }


def encrypt_refresh_token(refresh_token: str) -> str:
    return encrypt_secret(refresh_token) or ""
