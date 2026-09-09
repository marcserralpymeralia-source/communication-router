from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.prompt_runtime import ROUTING_PROMPT_FALLBACK
from app.communications.service import recipient_values
from app.db.models import Communication, Department, Mailbox, RoutingCorrection, RoutingDecision
from app.departments.service import build_department_routing_context
from app.routing.runtime import RoutingLLMRuntime


ROUTING_SYSTEM_PROMPT = ROUTING_PROMPT_FALLBACK

ROUTING_OUTPUT_SCHEMA: dict[str, Any] = {
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
    "properties": {
        "proposed_department_id": {"type": ["integer", "null"]},
        "category": {"type": "string", "minLength": 1, "maxLength": 100},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "requires_review": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
        "alternative_department_id": {"type": ["integer", "null"]},
        "ambiguity_reason": {"type": ["string", "null"], "maxLength": 1000},
    },
}


class RoutingAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    extracted_text: str | None = Field(default=None, max_length=12000)


class RoutingCommunication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str | None = Field(default=None, max_length=500)
    body_text: str | None = Field(default=None, max_length=30000)
    sender: dict[str, str | None]
    recipients: dict[str, list[str]]
    mailbox: str | None = Field(default=None, max_length=255)
    received_at: datetime | None = None
    attachments: list[RoutingAttachment] = Field(default_factory=list)


class RoutingProposal(BaseModel):
    """Validated proposal returned by the routing service."""

    model_config = ConfigDict(extra="forbid")

    proposed_department_id: StrictInt | None
    category: StrictStr = Field(min_length=1, max_length=100)
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    requires_review: StrictBool
    reason: StrictStr = Field(min_length=1, max_length=2000)
    alternative_department_id: StrictInt | None = None
    ambiguity_reason: StrictStr | None = Field(default=None, max_length=1000)


class RoutingRuntime(Protocol):
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_schema: dict[str, Any],
    ) -> Any:
        """Return a JSON object or a JSON string from the configured LLM runtime."""


class RoutingValidationError(ValueError):
    """Raised when an LLM response cannot be safely used for routing."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        error_type: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.error_type = error_type
        self.details = details or {}


ContextBuilder = Callable[[int, Session], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RoutingThresholds:
    """Single policy object for deciding whether an agent proposal can be routed."""

    auto_route_confidence: float = 0.90
    review_confidence: float = 0.70


DEFAULT_ROUTING_THRESHOLDS = RoutingThresholds()
ROUTING_DECISION_STATUSES = {"pending_review", "routed", "confirmed", "corrected", "superseded"}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Value is not JSON serializable: {type(value).__name__}")


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def _communication_payload(db: Session, company_id: int, communication: Communication) -> RoutingCommunication:
    if communication.company_id != company_id:
        raise RoutingValidationError("La comunicacion no pertenece al tenant indicado.")

    mailbox = db.scalar(
        select(Mailbox).where(
            Mailbox.id == communication.mailbox_id,
            Mailbox.company_id == company_id,
        )
    )
    if mailbox is None:
        raise RoutingValidationError("El buzon receptor no pertenece al tenant indicado.")

    attachments = [
        RoutingAttachment(
            filename=attachment.filename,
            extracted_text=_truncate(attachment.extracted_text, 12000),
        )
        for attachment in (communication.attachments or [])
    ]
    return RoutingCommunication(
        subject=_truncate(communication.subject, 500),
        body_text=_truncate(communication.body_text, 30000),
        sender={"email": communication.sender_email, "name": communication.sender_name},
        recipients={
            "to": recipient_values(communication.to_recipients),
            "cc": recipient_values(communication.cc_recipients),
            "bcc": recipient_values(communication.bcc_recipients),
        },
        mailbox=mailbox.email_address,
        received_at=communication.received_at,
        attachments=attachments,
    )


def _invoke_runtime(runtime: RoutingRuntime | Callable[..., Any], user_prompt: str) -> Any:
    if callable(runtime):
        return runtime(
            system_prompt=ROUTING_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            output_schema=ROUTING_OUTPUT_SCHEMA,
        )
    complete = getattr(runtime, "complete", None)
    if not callable(complete):
        raise RoutingValidationError("El runtime de routing no expone una operacion complete.")
    return complete(
        system_prompt=ROUTING_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output_schema=ROUTING_OUTPUT_SCHEMA,
    )


def _response_data(raw_response: Any) -> dict[str, Any]:
    if isinstance(raw_response, dict):
        if "validated_content" in raw_response:
            raw_response = raw_response["validated_content"]
        elif "content" in raw_response and len(raw_response) <= 3:
            raw_response = raw_response["content"]
    if isinstance(raw_response, str):
        try:
            raw_response = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise RoutingValidationError("La respuesta del runtime no es JSON valido.") from exc
    if not isinstance(raw_response, dict):
        raise RoutingValidationError("La respuesta del runtime debe ser un objeto JSON.")
    return raw_response


def _active_department_ids(db: Session, company_id: int, context: dict[str, Any]) -> set[int]:
    departments = context.get("departments") if isinstance(context, dict) else None
    if not isinstance(departments, list):
        raise RoutingValidationError("El contexto organizativo no contiene departments.")

    context_ids: set[int] = set()
    for department in departments:
        if not isinstance(department, dict):
            raise RoutingValidationError("El contexto contiene un departamento invalido.")
        department_id = department.get("department_id", department.get("id"))
        if not isinstance(department_id, int) or isinstance(department_id, bool):
            raise RoutingValidationError("El contexto contiene un department_id invalido.")
        if department.get("active") is False:
            continue
        context_ids.add(department_id)

    if not context_ids:
        return set()
    return set(
        db.scalars(
            select(Department.id).where(
                Department.id.in_(context_ids),
                Department.company_id == company_id,
                Department.active.is_(True),
            )
        ).all()
    )


def _validate_proposal(db: Session, company_id: int, context: dict[str, Any], raw_response: Any) -> RoutingProposal:
    try:
        proposal = RoutingProposal.model_validate(_response_data(raw_response))
    except (ValidationError, RoutingValidationError) as exc:
        detail = str(exc)
        raise RoutingValidationError(f"Respuesta de routing no valida: {detail}") from exc

    active_ids = _active_department_ids(db, company_id, context)
    if proposal.proposed_department_id is not None and proposal.proposed_department_id not in active_ids:
        raise RoutingValidationError("proposed_department_id no pertenece a un departamento activo del tenant.")
    if proposal.alternative_department_id is not None and proposal.alternative_department_id not in active_ids:
        raise RoutingValidationError("alternative_department_id no pertenece a un departamento activo del tenant.")
    if proposal.alternative_department_id == proposal.proposed_department_id and proposal.alternative_department_id is not None:
        raise RoutingValidationError("La alternativa no puede ser el mismo departamento propuesto.")
    if proposal.proposed_department_id is None and not proposal.requires_review:
        return proposal.model_copy(update={"requires_review": True})
    return proposal


class RoutingService:
    """Propose a tenant-owned active department for one communication."""

    def __init__(self, runtime: RoutingRuntime | Callable[..., Any], *, context_builder: ContextBuilder = build_department_routing_context) -> None:
        self.runtime = runtime
        self.context_builder = context_builder

    def classify_communication(
        self,
        db: Session,
        company_id: int,
        communication: Communication,
        *,
        routing_context: dict[str, Any] | None = None,
    ) -> RoutingProposal:
        payload = _communication_payload(db, company_id, communication)
        context = routing_context if routing_context is not None else self.context_builder(company_id, db)
        user_prompt = json.dumps(
            {
                "communication": payload.model_dump(mode="json"),
                "organization": context,
            },
            ensure_ascii=False,
            default=_json_default,
        )
        response = _invoke_runtime(self.runtime, user_prompt)
        return _validate_proposal(db, company_id, context, response)


def classify_communication(
    db: Session,
    company_id: int,
    communication: Communication,
    runtime: RoutingRuntime | Callable[..., Any],
    *,
    routing_context: dict[str, Any] | None = None,
    context_builder: ContextBuilder = build_department_routing_context,
) -> RoutingProposal:
    """Classify one Communication without persisting a RoutingDecision."""

    return RoutingService(runtime, context_builder=context_builder).classify_communication(
        db,
        company_id,
        communication,
        routing_context=routing_context,
    )


ConfiguredRoutingRuntime = RoutingLLMRuntime


def configured_routing_runtime(
    db: Session,
    company_id: int,
    *,
    user_id: int | None = None,
    communication_id: int | None = None,
) -> RoutingLLMRuntime:
    return RoutingLLMRuntime(
        db,
        company_id,
        user_id=user_id,
        communication_id=communication_id,
    )


def _communication_for_routing(db: Session, company_id: int, communication_id: int) -> Communication:
    communication = db.scalar(
        select(Communication).where(
            Communication.id == communication_id,
            Communication.company_id == company_id,
        )
    )
    if communication is None:
        raise RoutingValidationError("La comunicación no existe en el tenant indicado.")
    return communication


def current_routing_decision(db: Session, company_id: int, communication_id: int) -> RoutingDecision | None:
    return db.scalar(
        select(RoutingDecision)
        .where(
            RoutingDecision.company_id == company_id,
            RoutingDecision.communication_id == communication_id,
            RoutingDecision.status != "superseded",
        )
        .order_by(RoutingDecision.analysis_number.desc(), RoutingDecision.id.desc())
    )


def list_routing_decisions(db: Session, company_id: int, communication_id: int) -> list[RoutingDecision]:
    return db.scalars(
        select(RoutingDecision)
        .where(
            RoutingDecision.company_id == company_id,
            RoutingDecision.communication_id == communication_id,
        )
        .order_by(RoutingDecision.analysis_number.desc(), RoutingDecision.id.desc())
    ).all()


def _routing_status_for(proposal: RoutingProposal, thresholds: RoutingThresholds) -> str:
    if (
        proposal.proposed_department_id is None
        or proposal.requires_review
        or proposal.confidence < thresholds.review_confidence
        or (proposal.ambiguity_reason or "").strip()
    ):
        return "pending_review"
    if proposal.confidence >= thresholds.auto_route_confidence:
        return "routed"
    return "pending_review"


def analyze_communication(
    db: Session,
    company_id: int,
    communication_id: int,
    runtime: RoutingRuntime | Callable[..., Any],
    *,
    user_id: int | None = None,
    source: str = "agent",
    thresholds: RoutingThresholds | None = None,
    routing_context: dict[str, Any] | None = None,
) -> RoutingDecision:
    """Classify and persist a new decision, preserving all previous analyses."""

    communication = _communication_for_routing(db, company_id, communication_id)
    if thresholds is None:
        from app.routing.policy import load_routing_policy

        thresholds = load_routing_policy(db, company_id).as_routing_thresholds()
    proposal = classify_communication(
        db,
        company_id,
        communication,
        runtime,
        routing_context=routing_context,
    )
    previous_decisions = db.scalars(
        select(RoutingDecision).where(
            RoutingDecision.company_id == company_id,
            RoutingDecision.communication_id == communication_id,
        )
    ).all()
    for previous in previous_decisions:
        if previous.status != "superseded":
            previous.status = "superseded"
            previous.updated_at = datetime.now(timezone.utc)
    analysis_number = max((item.analysis_number or 0 for item in previous_decisions), default=0) + 1
    now = datetime.now(timezone.utc)
    decision_status = _routing_status_for(proposal, thresholds)
    decision = RoutingDecision(
        company_id=company_id,
        communication_id=communication_id,
        department_id=proposal.proposed_department_id,
        alternative_department_id=proposal.alternative_department_id,
        category=proposal.category,
        confidence=proposal.confidence,
        requires_review=proposal.requires_review,
        reason=proposal.reason,
        ambiguity_reason=proposal.ambiguity_reason,
        status=decision_status,
        source=source,
        analysis_number=analysis_number,
        created_at=now,
        updated_at=now,
    )
    if decision_status == "routed":
        decision.final_department_id = proposal.proposed_department_id
        decision.final_category = proposal.category
    db.add(decision)
    communication.routing_status = decision.status if decision.status in {"pending_review", "routed"} else "pending_review"
    communication.updated_at = now
    db.flush()
    return decision


def _active_department(db: Session, company_id: int, department_id: int) -> Department:
    department = db.scalar(
        select(Department).where(
            Department.id == department_id,
            Department.company_id == company_id,
            Department.active.is_(True),
        )
    )
    if department is None:
        raise RoutingValidationError("El departamento elegido no pertenece a un departamento activo del tenant.")
    return department


def _reviewable_decision(
    db: Session,
    company_id: int,
    communication_id: int,
    decision_id: int | None,
) -> tuple[Communication, RoutingDecision]:
    communication = _communication_for_routing(db, company_id, communication_id)
    decision = current_routing_decision(db, company_id, communication_id)
    if decision is None or (decision_id is not None and decision.id != decision_id):
        raise RoutingValidationError("La decisión indicada ya no es la vigente para esta comunicación.")
    return communication, decision


def confirm_routing_decision(
    db: Session,
    company_id: int,
    communication_id: int,
    reviewer_id: int,
    *,
    decision_id: int | None = None,
) -> RoutingDecision:
    communication, decision = _reviewable_decision(db, company_id, communication_id, decision_id)
    if decision.department_id is None:
        raise RoutingValidationError("No se puede confirmar una decisión sin departamento propuesto.")
    _active_department(db, company_id, decision.department_id)
    now = datetime.now(timezone.utc)
    decision.final_department_id = decision.department_id
    decision.final_category = decision.category
    decision.reviewed_by_user_id = reviewer_id
    decision.reviewed_at = now
    decision.updated_at = now
    decision.status = "confirmed"
    communication.routing_status = "routed"
    communication.updated_at = now
    db.flush()
    return decision


def correct_routing_decision(
    db: Session,
    company_id: int,
    communication_id: int,
    reviewer_id: int,
    corrected_department_id: int,
    *,
    reason: str,
    corrected_category: str | None = None,
    decision_id: int | None = None,
) -> RoutingDecision:
    communication, decision = _reviewable_decision(db, company_id, communication_id, decision_id)
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise RoutingValidationError("La corrección debe incluir un motivo.")
    _active_department(db, company_id, corrected_department_id)
    now = datetime.now(timezone.utc)
    correction = RoutingCorrection(
        company_id=company_id,
        routing_decision_id=decision.id,
        communication_id=communication_id,
        original_department_id=decision.department_id,
        corrected_department_id=corrected_department_id,
        original_category=decision.category,
        corrected_category=corrected_category or decision.category,
        reason=clean_reason,
        corrected_by_user_id=reviewer_id,
        created_at=now,
    )
    db.add(correction)
    decision.final_department_id = corrected_department_id
    decision.final_category = corrected_category or decision.category
    decision.reviewed_by_user_id = reviewer_id
    decision.reviewed_at = now
    decision.updated_at = now
    decision.status = "corrected"
    communication.routing_status = "routed"
    communication.updated_at = now
    db.flush()
    return decision


def serialize_routing_decision(decision: RoutingDecision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "id": decision.id,
        "communication_id": decision.communication_id,
        "department_id": decision.department_id,
        "alternative_department_id": decision.alternative_department_id,
        "final_department_id": decision.final_department_id,
        "category": decision.category,
        "final_category": decision.final_category,
        "confidence": decision.confidence,
        "requires_review": decision.requires_review,
        "reason": decision.reason,
        "ambiguity_reason": decision.ambiguity_reason,
        "status": decision.status,
        "source": decision.source,
        "analysis_number": decision.analysis_number,
        "reviewed_by_user_id": decision.reviewed_by_user_id,
        "reviewed_at": decision.reviewed_at.isoformat() if decision.reviewed_at else None,
        "created_at": decision.created_at.isoformat() if decision.created_at else None,
    }
