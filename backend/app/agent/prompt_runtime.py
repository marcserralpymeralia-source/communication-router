from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from time import perf_counter
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.model_catalog import DEFAULT_OPENAI_MODEL, LEGACY_OPENAI_MODEL_FALLBACK, resolve_openai_runtime_model
from app.db.models import PromptExecution, PromptTemplate, PromptVersion


PromptProvider = Callable[[Any, list[dict], str], dict]

ROUTING_PROMPT_PURPOSE = "communication_department_routing"
ROUTING_PROMPT_FALLBACK = (
    "Eres el agente de routing de comunicaciones de una empresa. Tu tarea exclusiva es "
    "proponer el departamento mas apropiado usando unicamente el contexto organizativo proporcionado.\n\n"
    "Interpreta la intencion semantica completa. No clasifiques por palabras clave aisladas. "
    "Considera responsabilidades, exclusiones, ejemplos, excepciones y asignaciones RACI del contexto. "
    "Una incidencia puede pertenecer a un departamento aunque su nombre no aparezca literalmente. "
    "Respeta las exclusiones y no inventes departamentos ni IDs.\n\n"
    "Solo puedes devolver IDs incluidos en el contexto. Si no existe evidencia suficiente, devuelve null. "
    "En casos ambiguos conserva una alternativa razonable, explica la ambiguedad y marca requires_review=true. "
    "La confianza debe expresar la evidencia disponible, no una certeza artificial.\n\n"
    "Devuelve exclusivamente un objeto JSON valido con exactamente estos campos: "
    "proposed_department_id, category, confidence, requires_review, reason, "
    "alternative_department_id y ambiguity_reason."
)


PROMPT_REGISTRY: dict[str, dict[str, Any]] = {
    "classification": {
        "name": "Clasificacion de entrada",
        "purpose": "classification",
        "default_model": "gpt-4.1-mini",
        "default_parameters": {"temperature": 0.1, "max_tokens": 1200},
        "expected_schema": {
            "type": "object",
            "required": ["tipo_correo", "confianza", "motivo"],
        },
        "fallback": "Clasifica la entrada como pedido, no_pedido, consulta, incidencia o dudoso. Responde solo JSON con tipo_correo, confianza y motivo.",
        "input_limit": 12000,
    },
    "whatsapp_conversation": {
        "name": "Estado conversacional de WhatsApp",
        "purpose": "whatsapp_conversation",
        "default_model": "gpt-4.1-mini",
        "default_parameters": {"temperature": 0.0, "max_tokens": 900},
        "expected_schema": {
            "type": "object",
            "required": [
                "intent",
                "state",
                "missing_or_uncertain",
                "reply_needed",
                "suggested_reply",
                "confidence",
            ],
        },
        "fallback": (
            "Analiza una conversacion de WhatsApp B2B entre CLIENTE y EMPRESA. "
            "Determina si el cliente esta realizando un pedido y si hay informacion "
            "suficiente para pedir su confirmacion. No inventes productos, cantidades, "
            "unidades ni clientes. Responde solo JSON con: "
            "intent (order, question u other), "
            "state (collecting, needs_clarification o ready_for_confirmation), "
            "missing_or_uncertain (lista de textos), "
            "reply_needed (boolean), suggested_reply (texto) y confidence (0 a 1). "
            "Usa ready_for_confirmation solo cuando el pedido expresado sea suficientemente "
            "claro para resumirlo y pedir confirmacion al cliente. "
            "Si falta o es ambigua informacion necesaria del pedido usa needs_clarification. "
            "Si el cliente todavia parece estar añadiendo contenido usa collecting."
        ),
        "input_limit": 12000,
    },
    "extraction": {
        "name": "Extraccion de pedido",
        "purpose": "extraction",
        "default_model": LEGACY_OPENAI_MODEL_FALLBACK,
        "default_parameters": {"temperature": 0.1, "max_tokens": 2400},
        "expected_schema": {
            "type": "object",
            "required": ["pedido"],
        },
        "fallback": "Extrae un pedido en JSON valido con cliente y pedido.lineas. Cada linea debe incluir texto_original, referencia_detectada, producto_detectado, cantidad, unidad y confianza_extraccion.",
        "input_limit": 16000,
    },
    ROUTING_PROMPT_PURPOSE: {
        "name": "Routing de comunicaciones",
        "purpose": ROUTING_PROMPT_PURPOSE,
        "default_model": DEFAULT_OPENAI_MODEL,
        "default_parameters": {"temperature": 0.1, "max_tokens": 1200},
        "expected_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "proposed_department_id",
                "category",
                "confidence",
                "requires_review",
                "reason",
                "alternative_department_id",
                "ambiguity_reason",
            ],
        },
        "fallback": ROUTING_PROMPT_FALLBACK,
        "input_limit": 30000,
    },
}

ALLOWED_CLASSIFICATION_TYPES = {"pedido", "no_pedido", "consulta", "incidencia", "dudoso"}


@dataclass(slots=True)
class PromptDefinition:
    template: PromptTemplate | None
    version: PromptVersion | None
    name: str
    purpose: str
    content: str
    default_model: str
    default_parameters: dict[str, Any]
    expected_schema: dict[str, Any]
    input_limit: int


@dataclass(slots=True)
class PromptValidationResult:
    status: str
    data: dict[str, Any] | None
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.status == "valid"


def resolve_prompt_definition(db: Session, company_id: int, purpose: str) -> PromptDefinition:
    spec = PROMPT_REGISTRY.get(purpose, {})
    fallback = str(spec.get("fallback") or "")
    template = db.scalar(select(PromptTemplate).where(PromptTemplate.company_id == company_id, PromptTemplate.purpose == purpose))
    version = db.get(PromptVersion, template.active_version_id) if template and template.active_version_id else None
    content = version.content if version and version.content else fallback
    return PromptDefinition(
        template=template,
        version=version,
        name=(template.name if template else str(spec.get("name") or purpose.title())),
        purpose=purpose,
        content=content,
        default_model=str(spec.get("default_model") or LEGACY_OPENAI_MODEL_FALLBACK),
        default_parameters=dict(spec.get("default_parameters") or {}),
        expected_schema=dict(spec.get("expected_schema") or {}),
        input_limit=int(spec.get("input_limit") or 12000),
    )


def ensure_prompt_template(
    db: Session,
    company_id: int,
    purpose: str,
    *,
    created_by_user_id: int | None = None,
) -> PromptDefinition:
    """Ensure a registry prompt has an editable initial version for a tenant."""

    definition = resolve_prompt_definition(db, company_id, purpose)
    if definition.template and definition.version:
        return definition

    spec = PROMPT_REGISTRY.get(purpose)
    if not spec:
        raise ValueError(f"Prompt no registrado: {purpose}")
    template = definition.template
    if template is None:
        template = PromptTemplate(
            company_id=company_id,
            name=str(spec.get("name") or purpose.title()),
            purpose=purpose,
        )
        db.add(template)
        db.flush()

    last_version = db.scalar(
        select(PromptVersion.version)
        .where(PromptVersion.template_id == template.id)
        .order_by(PromptVersion.version.desc())
    ) or 0
    version = PromptVersion(
        company_id=company_id,
        template_id=template.id,
        version=int(last_version) + 1,
        content=str(spec.get("fallback") or ""),
        created_by_user_id=created_by_user_id,
    )
    db.add(version)
    db.flush()
    template.active_version_id = version.id
    db.flush()
    return resolve_prompt_definition(db, company_id, purpose)


def prompt_registry_snapshot(db: Session, company_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(select(PromptTemplate).where(PromptTemplate.company_id == company_id).order_by(PromptTemplate.purpose)).all()
    snapshot: list[dict[str, Any]] = []
    for template in rows:
        version = db.get(PromptVersion, template.active_version_id) if template.active_version_id else None
        spec = PROMPT_REGISTRY.get(template.purpose, {})
        snapshot.append(
            {
                "template_id": template.id,
                "name": template.name,
                "purpose": template.purpose,
                "version": version.version if version else 0,
                "model": spec.get("default_model", LEGACY_OPENAI_MODEL_FALLBACK),
                "parameters": dict(spec.get("default_parameters") or {}),
                "expected_schema": dict(spec.get("expected_schema") or {}),
            }
        )
    return snapshot


def _extract_json_content(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if not text:
        raise ValueError("Respuesta vacia del proveedor IA.")
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {"value": parsed}
    raise ValueError("OpenAI ha devuelto una respuesta no valida: no es JSON.")


def validate_prompt_output(purpose: str, content: str) -> PromptValidationResult:
    try:
        data = _extract_json_content(content)
    except json.JSONDecodeError:
        return PromptValidationResult(status="invalid_json", data=None, errors=["La respuesta no es JSON valido."])
    except ValueError as exc:
        return PromptValidationResult(status="invalid_json", data=None, errors=[str(exc)])
    if purpose == "classification":
        tipo = str(data.get("tipo_correo") or data.get("type") or data.get("tipo") or "").strip().lower()
        confidence = data.get("confianza", data.get("confidence"))
        if not tipo:
            return PromptValidationResult(status="missing_fields", data=data, errors=["Falta tipo_correo."])
        if tipo not in ALLOWED_CLASSIFICATION_TYPES:
            return PromptValidationResult(status="unsupported_value", data=data, errors=[f"Tipo de correo no soportado: {tipo}."])
        if confidence is None:
            return PromptValidationResult(status="missing_fields", data=data, errors=["Falta confianza."])
        return PromptValidationResult(status="valid", data=data, errors=[])
    if purpose == "whatsapp_conversation":
        intent = str(data.get("intent") or "").strip().lower()
        state = str(data.get("state") or "").strip().lower()
        missing = data.get("missing_or_uncertain")
        reply_needed = data.get("reply_needed")
        suggested_reply = data.get("suggested_reply")
        confidence = data.get("confidence")

        if intent not in {"order", "question", "other"}:
            return PromptValidationResult(
                status="unsupported_value",
                data=data,
                errors=[f"Intent no soportado: {intent or 'vacio'}."],
            )
        if state not in {
            "collecting",
            "needs_clarification",
            "ready_for_confirmation",
        }:
            return PromptValidationResult(
                status="unsupported_value",
                data=data,
                errors=[f"Estado conversacional no soportado: {state or 'vacio'}."],
            )
        if not isinstance(missing, list) or not all(
            isinstance(item, str) for item in missing
        ):
            return PromptValidationResult(
                status="schema_error",
                data=data,
                errors=["missing_or_uncertain debe ser una lista de textos."],
            )
        if not isinstance(reply_needed, bool):
            return PromptValidationResult(
                status="schema_error",
                data=data,
                errors=["reply_needed debe ser boolean."],
            )
        if not isinstance(suggested_reply, str):
            return PromptValidationResult(
                status="schema_error",
                data=data,
                errors=["suggested_reply debe ser texto."],
            )
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            return PromptValidationResult(
                status="schema_error",
                data=data,
                errors=["confidence debe ser numerico."],
            )
        if not 0 <= float(confidence) <= 1:
            return PromptValidationResult(
                status="unsupported_value",
                data=data,
                errors=["confidence debe estar entre 0 y 1."],
            )
        if state in {"needs_clarification", "ready_for_confirmation"}:
            if not reply_needed or not suggested_reply.strip():
                return PromptValidationResult(
                    status="schema_error",
                    data=data,
                    errors=[
                        "El estado requiere una respuesta sugerida para el cliente."
                    ],
                )

        return PromptValidationResult(status="valid", data=data, errors=[])

    if purpose == "extraction":
        order = data.get("pedido") or data.get("order") or {}
        if not isinstance(order, dict):
            return PromptValidationResult(status="schema_error", data=data, errors=["La extraccion no contiene pedido en formato objeto."])
        lines = order.get("lineas") or order.get("lines") or data.get("lineas") or []
        if not isinstance(lines, list) or not lines:
            return PromptValidationResult(status="missing_fields", data=data, errors=["La extraccion no contiene lineas de pedido."])
        for index, line in enumerate(lines, start=1):
            if not isinstance(line, dict):
                return PromptValidationResult(status="schema_error", data=data, errors=[f"La linea {index} no es un objeto valido."])
        return PromptValidationResult(status="valid", data=data, errors=[])
    if purpose == ROUTING_PROMPT_PURPOSE:
        expected_fields = {
            "proposed_department_id",
            "category",
            "confidence",
            "requires_review",
            "reason",
            "alternative_department_id",
            "ambiguity_reason",
        }
        extra_fields = sorted(set(data) - expected_fields)
        missing_fields = sorted(expected_fields - set(data))
        if extra_fields or missing_fields:
            details = []
            if missing_fields:
                details.append(f"faltan: {', '.join(missing_fields)}")
            if extra_fields:
                details.append(f"sobran: {', '.join(extra_fields)}")
            return PromptValidationResult(status="schema_error", data=data, errors=[f"Esquema de routing invalido ({'; '.join(details)})."])

        for field in ("proposed_department_id", "alternative_department_id"):
            value = data[field]
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                return PromptValidationResult(status="schema_error", data=data, errors=[f"{field} debe ser entero o null."])
        if not isinstance(data["category"], str) or not data["category"].strip():
            return PromptValidationResult(status="schema_error", data=data, errors=["category debe ser texto no vacio."])
        confidence = data["confidence"]
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
            return PromptValidationResult(status="schema_error", data=data, errors=["confidence debe ser numerico entre 0 y 1."])
        if not isinstance(data["requires_review"], bool):
            return PromptValidationResult(status="schema_error", data=data, errors=["requires_review debe ser boolean."])
        if not isinstance(data["reason"], str) or not data["reason"].strip():
            return PromptValidationResult(status="schema_error", data=data, errors=["reason debe ser texto no vacio."])
        if data["ambiguity_reason"] is not None and not isinstance(data["ambiguity_reason"], str):
            return PromptValidationResult(status="schema_error", data=data, errors=["ambiguity_reason debe ser texto o null."])
        return PromptValidationResult(status="valid", data=data, errors=[])
    return PromptValidationResult(status="valid", data=data, errors=[])


def _safe_excerpt(text: str | None, limit: int = 1200) -> str | None:
    if not text:
        return None
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _safe_provider_message(message: Any) -> str:
    """Keep provider diagnostics useful without copying credentials into audit data."""

    text = str(message or "Error llamando al proveedor IA.")[:500]
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(api[-_ ]?key\s*[:=]\s*)\S+", r"\1[redacted]", text)
    return re.sub(r"(?i)\bsk-[A-Za-z0-9_-]+", "sk-[redacted]", text)


def _safe_provider_diagnostics(value: Any) -> dict[str, Any]:
    """Keep only structured provider metadata; never persist bodies or credentials."""

    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("category", "exception_class", "phase", "provider_code", "provider_param", "request_id"):
        item = value.get(key)
        if item is None:
            continue
        text = str(item).strip()
        if not text or len(text) > 160 or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", text):
            continue
        result[key] = text
    status_code = value.get("status_code")
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        result["status_code"] = status_code
    return result


def persist_prompt_execution(db: Session, execution: PromptExecution, *, commit: bool = False) -> PromptExecution:
    """Persist one execution without taking ownership of the caller transaction by default."""

    db.add(execution)
    db.flush()
    if commit:
        db.commit()
    return execution


def _provider_exception_status(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "provider_error"


def _provider_exception_diagnostics(exc: Exception) -> dict[str, str]:
    return {
        "exception_class": exc.__class__.__name__,
        "phase": "provider_call",
    }


def run_prompt_execution(
    db: Session,
    company_id: int,
    purpose: str,
    settings,
    text: str,
    *,
    provider_call: PromptProvider,
    input_reference: str | None = None,
    user_id: int | None = None,
    prompt_override: str | None = None,
    prompt_name_override: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    definition = resolve_prompt_definition(db, company_id, purpose)
    prompt_text = prompt_override or definition.content
    prompt_name = prompt_name_override or definition.name
    model_setting = "extraction_model" if purpose == "extraction" else "classification_model"
    configured_model = getattr(settings, model_setting, None)
    if purpose == "extraction":
        model = resolve_openai_runtime_model(configured_model, fallback=definition.default_model)
    else:
        model = configured_model or definition.default_model
    parameters = dict(definition.default_parameters)
    for key in ("temperature", "max_tokens", "timeout_seconds", "retries"):
        if hasattr(settings, key):
            parameters[key] = getattr(settings, key)
    messages = [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": text[: definition.input_limit]},
    ]
    started_at = datetime.now(timezone.utc)
    start = perf_counter()
    try:
        response = provider_call(settings, messages, model)
    except Exception as exc:  # Provider boundaries must become auditable results.
        response = {
            "ok": False,
            "error_type": _provider_exception_status(exc),
            "message": _safe_provider_message(exc),
            "diagnostics": _provider_exception_diagnostics(exc),
        }
    if not isinstance(response, dict):
        response = {
            "ok": False,
            "error_type": "provider_error",
            "message": "El proveedor IA devolvio una respuesta invalida.",
        }
    duration_ms = int((perf_counter() - start) * 1000)
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    response_content = response.get("content", "")
    if not isinstance(response_content, str):
        response_content = json.dumps(response_content, ensure_ascii=False, default=str) if response_content else ""
    if response.get("ok"):
        validation = validate_prompt_output(purpose, response_content)
    else:
        safe_message = _safe_provider_message(response.get("message"))
        response["message"] = safe_message
        validation = PromptValidationResult(
            status=str(response.get("error_type") or "provider_error"),
            data=None,
            errors=[safe_message],
        )
    provider_diagnostics = _safe_provider_diagnostics(response.get("diagnostics"))
    finished_at = datetime.now(timezone.utc)
    response_excerpt = _safe_excerpt(response_content)
    response_hash = sha256(response_content.encode("utf-8")).hexdigest() if response_content else None
    execution = PromptExecution(
        company_id=company_id,
        prompt_template_id=definition.template.id if definition.template else None,
        prompt_name=prompt_name,
        prompt_purpose=purpose,
        prompt_version=definition.version.version if definition.version else 0,
        model=model,
        parameters_json=json.dumps(parameters, ensure_ascii=False),
        input_reference=input_reference,
        output_status=validation.status,
        validation_errors_json=(
            json.dumps(
                {"errors": validation.errors, "diagnostics": provider_diagnostics},
                ensure_ascii=False,
            )
            if provider_diagnostics
            else (json.dumps(validation.errors, ensure_ascii=False) if validation.errors else None)
        ),
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        estimated_cost=usage.get("estimated_cost"),
        response_hash=response_hash,
        response_excerpt=response_excerpt,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
    )
    persist_prompt_execution(db, execution, commit=commit)
    result = dict(response)
    result.update(
        {
            "prompt_execution_id": execution.id,
            "prompt_name": prompt_name,
            "prompt_version": execution.prompt_version,
            "prompt_template_id": execution.prompt_template_id,
            "prompt_purpose": purpose,
            "model": model,
            "parameters": parameters,
            "validation_status": validation.status,
            "validation_errors": validation.errors,
            "validation_ok": validation.ok,
            "provider_diagnostics": provider_diagnostics,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "input_reference": input_reference,
        }
    )
    if user_id is not None:
        result["user_id"] = user_id
    if validation.ok:
        result["validated_content"] = validation.data
    return result
