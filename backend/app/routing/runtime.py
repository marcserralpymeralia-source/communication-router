from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.prompt_runtime import (
    ROUTING_PROMPT_PURPOSE,
    ensure_prompt_template,
    run_prompt_execution,
)
from app.db.models import LLMSettings


RoutingProvider = Callable[[Any, list[dict], str], dict]


def _routing_error(message: str, *, retryable: bool = False, error_type: str | None = None):
    # Import lazily so the service can expose the runtime without a module cycle.
    from app.routing.service import RoutingValidationError

    return RoutingValidationError(message, retryable=retryable, error_type=error_type)


class RoutingLLMRuntime:
    """Tenant-scoped routing adapter backed by the versioned prompt runtime."""

    def __init__(
        self,
        db: Session,
        company_id: int,
        *,
        provider_call: RoutingProvider | None = None,
        user_id: int | None = None,
        communication_id: int | None = None,
    ) -> None:
        self.db = db
        self.company_id = company_id
        self.provider_call = provider_call
        self.user_id = user_id
        self.communication_id = communication_id

    def _settings(self) -> LLMSettings:
        settings = self.db.scalar(select(LLMSettings).where(LLMSettings.company_id == self.company_id))
        if settings is None:
            settings = LLMSettings(company_id=self.company_id)
            self.db.add(settings)
            self.db.flush()
        return settings

    def _provider(self) -> RoutingProvider:
        if self.provider_call is not None:
            return self.provider_call
        from app.settings.integrations import call_openai

        return call_openai

    def complete(self, *, system_prompt: str, user_prompt: str, output_schema: dict[str, Any]) -> Any:
        del system_prompt, output_schema
        settings = self._settings()
        ensure_prompt_template(
            self.db,
            self.company_id,
            ROUTING_PROMPT_PURPOSE,
            created_by_user_id=self.user_id,
        )

        def provider_call(current_settings, messages, model):  # noqa: ANN001
            if not getattr(current_settings, "agent_enabled", True):
                return {
                    "ok": False,
                    "error_type": "disabled",
                    "message": "El runtime de IA esta deshabilitado para este tenant.",
                }
            return self._provider()(current_settings, messages, model)

        result = run_prompt_execution(
            self.db,
            self.company_id,
            ROUTING_PROMPT_PURPOSE,
            settings,
            user_prompt,
            provider_call=provider_call,
            input_reference=f"communication:{self.communication_id}" if self.communication_id is not None else None,
            user_id=self.user_id,
            commit=False,
        )
        if not result.get("ok"):
            error_type = str(result.get("error_type") or "provider_error")
            raise _routing_error(
                result.get("message") or "No se pudo analizar la comunicación.",
                retryable=error_type in {"timeout", "provider_error", "temporarily_unavailable", "rate_limit"},
                error_type=error_type,
            )
        if not result.get("validation_ok"):
            errors = "; ".join(result.get("validation_errors") or [])
            raise _routing_error(
                f"Respuesta de routing no valida: {errors or 'esquema no valido'}.",
                error_type=str(result.get("validation_status") or "schema_error"),
            )
        return result
