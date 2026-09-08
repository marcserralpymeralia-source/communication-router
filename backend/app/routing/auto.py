from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.encryption import decrypt_secret
from app.db.models import BackgroundJob, Communication, Department, LLMSettings, Mailbox, RoutingAction, RoutingDecision
from app.jobs.service import enqueue_job, job_payload
from app.routing.forwarding import (
    enqueue_forwarding_job,
    ensure_routing_action,
    _valid_email,
)
from app.routing.service import (
    RoutingValidationError,
    analyze_communication,
    configured_routing_runtime,
    current_routing_decision,
)
from app.routing.policy import load_routing_policy


AUTO_ROUTING_JOB_TYPE = "route_communication"
AUTO_ROUTING_DEDUPE_PREFIX = "communication:"


class AutomaticRoutingError(RoutingValidationError):
    """Controlled failure from the automatic routing worker."""


def _llm_settings(db: Session, company_id: int) -> LLMSettings | None:
    return db.scalar(select(LLMSettings).where(LLMSettings.company_id == company_id))


def auto_routing_available(settings: LLMSettings | None) -> bool:
    if settings is None:
        return False
    if not settings.auto_routing_enabled or not settings.agent_enabled:
        return False
    if settings.provider == "disabled" or settings.agent_mode == "desactivado":
        return False
    if not settings.can_classify_email:
        return False
    return bool(decrypt_secret(settings.api_key_encrypted))


def auto_forwarding_enabled(settings: LLMSettings | None) -> bool:
    return bool(settings is not None and getattr(settings, "auto_forwarding_enabled", False))


def _has_sent_forward(db: Session, company_id: int, communication_id: int) -> bool:
    return db.scalar(
        select(RoutingAction.id).where(
            RoutingAction.company_id == company_id,
            RoutingAction.communication_id == communication_id,
            RoutingAction.status == "sent",
        )
    ) is not None


def _cancel_unstarted_actions(db: Session, company_id: int, communication_id: int, department_id: int) -> None:
    actions = db.scalars(
        select(RoutingAction).where(
            RoutingAction.company_id == company_id,
            RoutingAction.communication_id == communication_id,
            RoutingAction.department_id != department_id,
            RoutingAction.status.in_(("pending", "failed")),
        )
    ).all()
    for action in actions:
        action.status = "cancelled"
        action.updated_at = datetime.now(timezone.utc)


def _force_review(db: Session, company_id: int, decision: RoutingDecision, reason: str | None = None) -> None:
    decision.requires_review = True
    decision.status = "pending_review"
    if reason:
        decision.ambiguity_reason = reason
    communication = db.scalar(
        select(Communication).where(
            Communication.id == decision.communication_id,
            Communication.company_id == company_id,
        )
    )
    if communication is not None:
        communication.routing_status = "pending_review"
        communication.updated_at = datetime.now(timezone.utc)


def enqueue_forwarding_for_decision(
    db: Session,
    *,
    company_id: int,
    decision: RoutingDecision,
    triggered_by_user_id: int | None = None,
    source: str = "automatic",
    human_confirmed: bool = False,
) -> BackgroundJob | None:
    """Apply deterministic forwarding policy and enqueue only an eligible action."""

    if decision.company_id != company_id:
        raise AutomaticRoutingError("La decisión no pertenece al tenant indicado.", error_type="tenant_mismatch")
    policy = load_routing_policy(db, company_id)
    if policy.simulation_mode:
        if source == "automatic" and not policy.auto_routing_enabled:
            return None
    elif not policy.auto_forwarding_enabled:
        return None

    if source == "automatic":
        if (
            decision.status != "routed"
            or decision.requires_review
            or decision.confidence < policy.auto_threshold
            or (decision.ambiguity_reason or "").strip()
            or decision.department_id is None
        ):
            return None
        department_id = decision.department_id
        if _has_sent_forward(db, company_id, decision.communication_id):
            return None
    else:
        if not human_confirmed or decision.status not in {"confirmed", "corrected"}:
            return None
        department_id = decision.final_department_id
        if department_id is None:
            return None
        _cancel_unstarted_actions(db, company_id, decision.communication_id, department_id)

    department = db.scalar(
        select(Department).where(
            Department.id == department_id,
            Department.company_id == company_id,
            Department.active.is_(True),
        )
    )
    if department is None:
        if source == "automatic":
            _force_review(db, company_id, decision)
            db.flush()
            return None
        raise AutomaticRoutingError("El departamento final ya no está activo.", error_type="invalid_department")
    if not _valid_email(department.destination_email):
        if source == "automatic":
            _force_review(
                db,
                company_id,
                decision,
                "El departamento no tiene destination_email válido.",
            )
            db.flush()
            return None
        raise AutomaticRoutingError("El departamento no tiene destination_email válido.", error_type="invalid_destination")

    if policy.simulation_mode:
        action = ensure_routing_action(
            db,
            company_id=company_id,
            communication_id=decision.communication_id,
            routing_decision_id=decision.id,
            department_id=department_id,
            triggered_by_user_id=triggered_by_user_id,
            source="simulation",
        )
        if action.status in {"sent", "processing", "cancelled", "simulated"}:
            return None
        action.action_type = "simulated_forward"
        action.source = "simulation"
        action.status = "simulated"
        action.error_code = None
        action.error_message = f"Simulación: se habría reenviado a {department.destination_email}."
        action.completed_at = datetime.now(timezone.utc)
        action.updated_at = action.completed_at
        db.flush()
        return None

    if source == "automatic":
        decision.final_department_id = department_id
        decision.final_category = decision.final_category or decision.category
        decision.updated_at = datetime.now(timezone.utc)
    action = ensure_routing_action(
        db,
        company_id=company_id,
        communication_id=decision.communication_id,
        routing_decision_id=decision.id,
        department_id=department_id,
        triggered_by_user_id=triggered_by_user_id,
        source=source,
    )
    if action.status in {"sent", "processing", "cancelled"}:
        return None
    return enqueue_forwarding_job(db, action)


def enqueue_automatic_routing(
    db: Session,
    *,
    company_id: int,
    communication_id: int,
    created_by_user_id: int | None = None,
) -> BackgroundJob | None:
    """Queue one processed Communication without putting content in the job payload."""

    communication = db.scalar(
        select(Communication).where(
            Communication.id == communication_id,
            Communication.company_id == company_id,
        )
    )
    if communication is None or communication.mailbox_id is None:
        return None
    mailbox = db.scalar(
        select(Mailbox).where(
            Mailbox.id == communication.mailbox_id,
            Mailbox.company_id == company_id,
        )
    )
    if mailbox is None or communication.processing_status != "processed":
        return None
    if current_routing_decision(db, company_id, communication_id) is not None:
        return None
    if not auto_routing_available(_llm_settings(db, company_id)):
        return None

    dedupe_key = f"{AUTO_ROUTING_DEDUPE_PREFIX}{company_id}:{communication_id}"
    existing = db.scalar(
        select(BackgroundJob).where(
            BackgroundJob.company_id == company_id,
            BackgroundJob.job_type == AUTO_ROUTING_JOB_TYPE,
            BackgroundJob.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        return existing

    communication.routing_status = "routing_queued"
    db.flush()
    return enqueue_job(
        db,
        company_id=company_id,
        job_type=AUTO_ROUTING_JOB_TYPE,
        payload={"company_id": company_id, "communication_id": communication_id},
        created_by_user_id=created_by_user_id,
        dedupe_key=dedupe_key,
    )


def _payload_id(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        raise AutomaticRoutingError(f"El payload de routing no contiene {key} válido.", error_type="invalid_payload")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AutomaticRoutingError(f"El payload de routing no contiene {key} válido.", error_type="invalid_payload") from exc
    if parsed <= 0:
        raise AutomaticRoutingError(f"El payload de routing no contiene {key} válido.", error_type="invalid_payload")
    return parsed


def process_automatic_routing(db: Session, job: BackgroundJob) -> dict[str, Any]:
    """Run an automatic decision in the existing worker transaction."""

    payload = job_payload(job)
    payload_company_id = _payload_id(payload, "company_id")
    communication_id = _payload_id(payload, "communication_id")
    if payload_company_id != job.company_id:
        raise AutomaticRoutingError("El job de routing no pertenece al tenant indicado.", error_type="tenant_mismatch")

    communication = db.scalar(
        select(Communication).where(
            Communication.id == communication_id,
            Communication.company_id == job.company_id,
        )
    )
    if communication is None:
        raise AutomaticRoutingError("La comunicación del job ya no existe en el tenant indicado.", error_type="communication_not_found")
    mailbox = db.scalar(
        select(Mailbox).where(
            Mailbox.id == communication.mailbox_id,
            Mailbox.company_id == job.company_id,
        )
    )
    if mailbox is None:
        raise AutomaticRoutingError("El buzón de la comunicación no pertenece al tenant indicado.", error_type="tenant_mismatch")

    if current_routing_decision(db, job.company_id, communication_id) is not None:
        return {"ok": True, "skipped": True, "reason": "La comunicación ya tiene una decisión vigente."}
    if communication.processing_status != "processed":
        raise AutomaticRoutingError("La comunicación todavía no está procesada.", error_type="communication_not_processed")
    if not auto_routing_available(_llm_settings(db, job.company_id)):
        communication.routing_status = "routing_error"
        db.flush()
        raise AutomaticRoutingError("La configuración de auto-routing no está disponible.", error_type="invalid_configuration")

    communication.routing_status = "routing_processing"
    db.flush()
    try:
        decision = analyze_communication(
            db,
            job.company_id,
            communication_id,
            configured_routing_runtime(
                db,
                job.company_id,
                communication_id=communication_id,
            ),
            source="auto",
            thresholds=load_routing_policy(db, job.company_id).as_routing_thresholds(),
        )
    except Exception:
        communication.routing_status = "routing_error"
        db.flush()
        raise
    forwarding_job = enqueue_forwarding_for_decision(
        db,
        company_id=job.company_id,
        decision=decision,
        source="automatic",
    )
    return {
        "ok": True,
        "communication_id": communication_id,
        "decision_id": decision.id,
        "forwarding_job_id": forwarding_job.id if forwarding_job else None,
        "routing_status": communication.routing_status,
    }
