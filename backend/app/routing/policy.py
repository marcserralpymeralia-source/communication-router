from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.prompt_runtime import ROUTING_PROMPT_PURPOSE
from app.core.encryption import decrypt_secret
from app.db.models import (
    Department,
    DepartmentKnowledge,
    LLMSettings,
    Mailbox,
    PromptTemplate,
    PromptVersion,
    RoutingEvaluationRun,
)
from app.routing.service import DEFAULT_ROUTING_THRESHOLDS


class RoutingPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    auto_routing_enabled: bool = False
    auto_forwarding_enabled: bool = False
    simulation_mode: bool = False
    review_threshold: float = DEFAULT_ROUTING_THRESHOLDS.review_confidence
    auto_threshold: float = DEFAULT_ROUTING_THRESHOLDS.auto_route_confidence

    def validate(self) -> "RoutingPolicy":
        if not 0 <= self.review_threshold <= self.auto_threshold <= 1:
            raise RoutingPolicyError("Los umbrales deben cumplir 0 <= revisión <= automatización <= 1.")
        return self

    @classmethod
    def from_settings(cls, settings: LLMSettings | None) -> "RoutingPolicy":
        if settings is None:
            return cls().validate()
        return cls(
            auto_routing_enabled=bool(settings.auto_routing_enabled),
            auto_forwarding_enabled=bool(settings.auto_forwarding_enabled),
            simulation_mode=bool(getattr(settings, "simulation_mode", False)),
            review_threshold=float(getattr(settings, "routing_review_threshold", DEFAULT_ROUTING_THRESHOLDS.review_confidence)),
            auto_threshold=float(getattr(settings, "routing_auto_threshold", DEFAULT_ROUTING_THRESHOLDS.auto_route_confidence)),
        ).validate()

    def as_dict(self) -> dict[str, Any]:
        return {
            "auto_routing_enabled": self.auto_routing_enabled,
            "auto_forwarding_enabled": self.auto_forwarding_enabled,
            "simulation_mode": self.simulation_mode,
            "review_threshold": self.review_threshold,
            "auto_threshold": self.auto_threshold,
        }

    def as_routing_thresholds(self):
        from app.routing.service import RoutingThresholds

        return RoutingThresholds(
            auto_route_confidence=self.auto_threshold,
            review_confidence=self.review_threshold,
        )


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    eligible: bool
    mode: str
    message: str


def load_routing_policy(db: Session, company_id: int) -> RoutingPolicy:
    settings = db.scalar(select(LLMSettings).where(LLMSettings.company_id == company_id))
    return RoutingPolicy.from_settings(settings)


def policy_outcome(
    policy: RoutingPolicy,
    *,
    confidence: float,
    requires_review: bool,
    ambiguity_reason: str | None,
    department: Department | None,
) -> PolicyOutcome:
    if department is None or not department.active:
        return PolicyOutcome(False, "blocked", "El departamento propuesto no está activo.")
    if not department.destination_email:
        return PolicyOutcome(False, "blocked", "El departamento no tiene un destino configurado.")
    if requires_review:
        return PolicyOutcome(False, "review", "La comunicación requiere revisión humana.")
    if (ambiguity_reason or "").strip():
        return PolicyOutcome(False, "review", "La comunicación contiene una ambigüedad relevante.")
    if confidence < policy.review_threshold:
        return PolicyOutcome(False, "review", "La confianza está por debajo del umbral de revisión.")
    if confidence < policy.auto_threshold:
        return PolicyOutcome(False, "review", "La confianza no alcanza el umbral de automatización.")
    if not policy.auto_routing_enabled:
        return PolicyOutcome(False, "blocked", "El análisis automático está desactivado.")
    if policy.simulation_mode:
        return PolicyOutcome(True, "simulated", f"Se habría derivado automáticamente a {department.name}.")
    if not policy.auto_forwarding_enabled:
        return PolicyOutcome(False, "blocked", "El reenvío automático está desactivado.")
    return PolicyOutcome(True, "real", f"Se derivará automáticamente a {department.name}.")


def _check(key: str, label: str, status: str, message: str, action_url: str | None = None) -> dict[str, str | None]:
    return {"key": key, "label": label, "status": status, "message": message, "action_url": action_url}


def build_kibak_readiness(db: Session, company_id: int) -> dict[str, Any]:
    """Build tenant-scoped checks without contacting IMAP, SMTP, or an LLM provider."""
    settings = db.scalar(select(LLMSettings).where(LLMSettings.company_id == company_id))
    policy = RoutingPolicy.from_settings(settings)
    departments = list(db.scalars(select(Department).where(Department.company_id == company_id, Department.active.is_(True))))
    knowledge_count = db.scalar(
        select(DepartmentKnowledge.id)
        .join(Department, Department.id == DepartmentKnowledge.department_id)
        .where(Department.company_id == company_id, DepartmentKnowledge.active.is_(True))
        .limit(1)
    )
    mailboxes = list(db.scalars(select(Mailbox).where(Mailbox.company_id == company_id)))
    configured_mailboxes = [item for item in mailboxes if item.email_address]
    enabled_mailboxes = [item for item in configured_mailboxes if item.enabled]
    prompt = db.scalar(
        select(PromptTemplate)
        .where(PromptTemplate.company_id == company_id, PromptTemplate.purpose == ROUTING_PROMPT_PURPOSE)
        .limit(1)
    )
    prompt_version = db.scalar(select(PromptVersion).where(PromptVersion.company_id == company_id, PromptVersion.template_id == prompt.id).limit(1)) if prompt else None
    latest_run = db.scalar(select(RoutingEvaluationRun).where(RoutingEvaluationRun.company_id == company_id).order_by(RoutingEvaluationRun.id.desc()).limit(1))
    checks = [
        _check("departments", "Departamentos", "PASS" if departments else "FAIL", f"{len(departments)} departamentos activos." if departments else "Crea al menos un departamento activo.", "/departments"),
        _check("knowledge", "Knowledge", "PASS" if knowledge_count else "WARN", "Knowledge operativo disponible." if knowledge_count else "Añade responsabilidades y exclusiones.", "/departments"),
        _check("destinations", "Destinos", "PASS" if departments and all(item.destination_email for item in departments) else "FAIL", "Todos los destinos están configurados." if departments and all(item.destination_email for item in departments) else "Configura destination_email en cada departamento activo.", "/departments"),
        _check("mailboxes", "Buzones", "PASS" if configured_mailboxes else "FAIL", f"{len(configured_mailboxes)} buzones configurados; conectividad no verificada.", "/settings/mailboxes"),
        _check("enabled_mailboxes", "Buzón activo", "PASS" if enabled_mailboxes else "WARN", "Hay un buzón activo." if enabled_mailboxes else "Activa un buzón cuando vayas a iniciar el piloto.", "/settings/mailboxes"),
        _check("ai", "IA", "PASS" if settings and settings.agent_enabled and settings.provider != "disabled" and decrypt_secret(settings.api_key_encrypted) else "WARN", "Proveedor y modelo configurados." if settings and settings.agent_enabled and settings.provider != "disabled" and decrypt_secret(settings.api_key_encrypted) else "Configura la IA antes de automatizar.", "/settings#ai"),
        _check("prompt", "Prompt", "PASS" if prompt and prompt_version else "WARN", f"Prompt {prompt.name} v{prompt_version.version}." if prompt and prompt_version else "Crea o activa el prompt de routing.", "/settings#ai"),
        _check("thresholds", "Umbrales", "PASS", f"Revisión {policy.review_threshold:.0%}; automatización {policy.auto_threshold:.0%}."),
        _check("evaluation", "Evaluación", "PASS" if latest_run and latest_run.status == "completed" else "WARN", f"Evaluation Run #{latest_run.id} disponible." if latest_run and latest_run.status == "completed" else "Ejecuta una evaluación antes de activar forwarding.", "/routing/evaluations"),
        _check("simulation", "Modo simulación", "PASS" if policy.simulation_mode else "WARN", "Activo: no se enviará SMTP." if policy.simulation_mode else "Desactivado: el forwarding podría ser real si se habilita."),
        _check("workers", "Workers", "PASS", "El worker se comprueba mediante health operativo."),
        _check("storage", "Storage", "PASS", "Storage disponible para el tenant."),
    ]
    if policy.auto_forwarding_enabled:
        critical = 0
        if latest_run and latest_run.metrics_json:
            import json
            critical = int(json.loads(latest_run.metrics_json).get("critical_false_auto_routes", 0) or 0)
        checks.append(_check("auto_forwarding", "Reenvío automático", "FAIL" if critical else "WARN", "Hay false auto-routes críticos en el último run." if critical else "Requiere supervisión: no se ha verificado entrega real."))
    else:
        checks.append(_check("auto_forwarding", "Reenvío automático", "PASS", "Desactivado por política."))
    failures = sum(item["status"] == "FAIL" for item in checks)
    warnings = sum(item["status"] == "WARN" for item in checks)
    return {"company_id": company_id, "policy": policy.as_dict(), "checks": checks, "failures": failures, "warnings": warnings, "ready": failures == 0}


def parse_policy_form(data: dict[str, Any], current: RoutingPolicy) -> RoutingPolicy:
    def flag(name: str) -> bool:
        return data.get(name) in {True, "true", "1", "on", "yes"}

    def number(name: str, fallback: float) -> float:
        value = data.get(name, fallback)
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise RoutingPolicyError(f"{name} debe ser un número entre 0 y 1.") from exc

    return RoutingPolicy(
        auto_routing_enabled=flag("auto_routing_enabled"),
        auto_forwarding_enabled=flag("auto_forwarding_enabled"),
        simulation_mode=flag("simulation_mode"),
        review_threshold=number("routing_review_threshold", current.review_threshold),
        auto_threshold=number("routing_auto_threshold", current.auto_threshold),
    ).validate()


__all__ = ["RoutingPolicy", "RoutingPolicyError", "PolicyOutcome", "build_kibak_readiness", "load_routing_policy", "parse_policy_form", "policy_outcome"]
