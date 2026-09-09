from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.model_catalog import DEFAULT_OPENAI_MODEL
from app.agent.prompt_runtime import ROUTING_PROMPT_PURPOSE
from app.core.encryption import decrypt_secret, encrypt_secret
from app.db.models import LLMSettings, PromptTemplate, PromptVersion


KIBAK_AI_ADMIN_ROLES = {"Administrador", "Superadmin"}
KIBAK_AI_PROVIDERS = {
    "openai": "OpenAI",
    "openai_compatible": "Compatible OpenAI",
    "azure_openai": "Azure OpenAI",
}
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
KIBAK_AI_CSRF_SESSION_KEY = "kibak_ai_csrf"


class KibakAIConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class KibakAIPromptInfo:
    name: str
    purpose: str
    version: int | None
    available: bool


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(KIBAK_AI_CSRF_SESSION_KEY)
    if not isinstance(token, str) or len(token) < 20:
        token = secrets.token_urlsafe(32)
        request.session[KIBAK_AI_CSRF_SESSION_KEY] = token
    return token


def validate_csrf_token(request: Request, token: str) -> None:
    expected = request.session.get(KIBAK_AI_CSRF_SESSION_KEY)
    if not isinstance(expected, str) or not token or not hmac.compare_digest(expected, token):
        raise KibakAIConfigError("La sesión del formulario ha caducado. Recarga la pantalla e inténtalo de nuevo.")


def get_llm_settings(db: Session, company_id: int) -> LLMSettings | None:
    return db.scalar(select(LLMSettings).where(LLMSettings.company_id == company_id))


def get_routing_prompt_info(db: Session, company_id: int) -> KibakAIPromptInfo:
    template = db.scalar(
        select(PromptTemplate)
        .where(PromptTemplate.company_id == company_id, PromptTemplate.purpose == ROUTING_PROMPT_PURPOSE)
        .limit(1)
    )
    if not template:
        return KibakAIPromptInfo("Routing de comunicaciones", ROUTING_PROMPT_PURPOSE, None, False)
    version = db.get(PromptVersion, template.active_version_id) if template.active_version_id else None
    return KibakAIPromptInfo(template.name, template.purpose, version.version if version else None, version is not None)


def credential_configured(settings: LLMSettings | None) -> bool:
    return bool(settings and decrypt_secret(settings.api_key_encrypted))


def validate_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    if normalized not in KIBAK_AI_PROVIDERS:
        raise KibakAIConfigError("Selecciona un proveedor IA compatible.")
    return normalized


def validate_model(model: str) -> str:
    normalized = (model or "").strip()
    if not normalized or len(normalized) > 100 or any(ord(char) < 32 for char in normalized):
        raise KibakAIConfigError("Indica un modelo válido.")
    return normalized


def validate_api_key(value: str, provider: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    if len(normalized) < 10 or len(normalized) > 500 or any(ord(char) < 32 for char in normalized):
        raise KibakAIConfigError("La credencial no tiene un formato válido.")
    if provider == "openai" and not (normalized.startswith("sk-") or normalized.startswith("test-")):
        raise KibakAIConfigError("La credencial de OpenAI no tiene un formato válido.")
    return normalized


def validate_base_url(value: str) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or len(normalized) > 500:
        raise KibakAIConfigError("La URL base debe ser HTTP(S) y no puede contener credenciales.")
    return normalized.rstrip("/")


def _number(value: str, *, field: str, integer: bool, minimum: float, maximum: float):
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise KibakAIConfigError(f"{field} no es válido.") from exc
    if not minimum <= parsed <= maximum:
        raise KibakAIConfigError(f"{field} debe estar entre {minimum:g} y {maximum:g}.")
    return parsed


def save_configuration(
    db: Session,
    company_id: int,
    *,
    provider: str,
    model: str,
    base_url: str,
    temperature: str,
    max_tokens: str,
    timeout_seconds: str,
    retries: str,
    api_key: str,
) -> LLMSettings:
    provider = validate_provider(provider)
    model = validate_model(model)
    base_url = validate_base_url(base_url)
    if base_url is None and provider == "openai":
        base_url = DEFAULT_OPENAI_BASE_URL
    temperature_value = _number(temperature, field="La temperatura", integer=False, minimum=0, maximum=2)
    max_tokens_value = _number(max_tokens, field="El máximo de tokens", integer=True, minimum=1, maximum=32000)
    timeout_value = _number(timeout_seconds, field="El timeout", integer=True, minimum=1, maximum=600)
    retries_value = _number(retries, field="Los reintentos", integer=True, minimum=0, maximum=5)
    normalized_key = validate_api_key(api_key, provider)

    settings = get_llm_settings(db, company_id)
    if settings is None:
        settings = LLMSettings(company_id=company_id, classification_model=DEFAULT_OPENAI_MODEL)
        db.add(settings)
        db.flush()
    settings.provider = provider
    settings.classification_model = model
    settings.base_url = base_url
    settings.temperature = temperature_value
    settings.max_tokens = max_tokens_value
    settings.timeout_seconds = timeout_value
    settings.retries = retries_value
    if normalized_key:
        try:
            settings.api_key_encrypted = encrypt_secret(normalized_key)
        except Exception as exc:  # Encryption failures must not expose provider data.
            raise KibakAIConfigError("No se ha podido proteger la credencial.") from exc
    return settings


def delete_credential(db: Session, company_id: int) -> LLMSettings | None:
    settings = get_llm_settings(db, company_id)
    if settings is None:
        return None
    settings.api_key_encrypted = None
    settings.agent_enabled = False
    settings.last_test_ok = None
    settings.last_test_message = None
    settings.last_error = None
    settings.last_response_ms = None
    return settings


def validate_activation(db: Session, company_id: int) -> LLMSettings:
    settings = get_llm_settings(db, company_id)
    prompt = get_routing_prompt_info(db, company_id)
    if settings is None or settings.provider not in KIBAK_AI_PROVIDERS:
        raise KibakAIConfigError("Configura un proveedor IA compatible antes de activar el agente.")
    if not credential_configured(settings):
        raise KibakAIConfigError("Configura una credencial antes de activar el agente.")
    if not settings.classification_model or not prompt.available:
        raise KibakAIConfigError("El modelo o el prompt de routing no están disponibles.")
    return settings
