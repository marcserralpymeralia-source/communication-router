"""Tenant-scoped routing playground and evaluation harness services."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Communication,
    CommunicationAttachment,
    Department,
    LLMSettings,
    Mailbox,
    RoutingEvaluationCase,
    RoutingEvaluationResult,
    RoutingEvaluationRun,
    RoutingEvaluationSet,
)
from app.departments.service import build_department_routing_context, list_departments
from app.routing.evaluation_dataset import synthetic_cases
from app.routing.service import (
    DEFAULT_ROUTING_THRESHOLDS,
    RoutingValidationError,
    classify_communication,
)
from app.routing.runtime import RoutingLLMRuntime, RoutingProvider


PROMPT_PURPOSE = "communication_department_routing"
PLAYGROUND_ROLES = ("Administrador", "Supervisor", "Superadmin")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def list_evaluation_sets(db: Session, company_id: int) -> list[RoutingEvaluationSet]:
    return list(
        db.scalars(
            select(RoutingEvaluationSet)
            .where(RoutingEvaluationSet.company_id == company_id)
            .order_by(RoutingEvaluationSet.active.desc(), RoutingEvaluationSet.name)
        )
    )


def get_evaluation_set(db: Session, company_id: int, set_id: int) -> RoutingEvaluationSet | None:
    return db.scalar(
        select(RoutingEvaluationSet).where(
            RoutingEvaluationSet.company_id == company_id,
            RoutingEvaluationSet.id == set_id,
        )
    )


def update_evaluation_set(
    db: Session,
    company_id: int,
    set_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    active: bool | None = None,
    commit: bool = True,
) -> RoutingEvaluationSet:
    evaluation_set = get_evaluation_set(db, company_id, set_id)
    if evaluation_set is None:
        raise ValueError("Conjunto de evaluación no encontrado en este tenant.")
    if name is not None:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("El nombre del conjunto es obligatorio.")
        evaluation_set.name = normalized_name
    if description is not None:
        evaluation_set.description = description.strip() or None
    if active is not None:
        evaluation_set.active = active
    evaluation_set.updated_at = _now()
    db.flush()
    if commit:
        db.commit()
    return evaluation_set


def create_evaluation_set(
    db: Session,
    company_id: int,
    *,
    name: str,
    description: str | None = None,
    commit: bool = True,
) -> RoutingEvaluationSet:
    name = name.strip()
    if not name:
        raise ValueError("El nombre del conjunto es obligatorio.")
    evaluation_set = RoutingEvaluationSet(company_id=company_id, name=name, description=description)
    db.add(evaluation_set)
    db.flush()
    if commit:
        db.commit()
    return evaluation_set


def create_evaluation_case(
    db: Session,
    company_id: int,
    set_id: int,
    *,
    title: str,
    subject: str,
    body: str,
    sender: str | None = None,
    recipients: str | None = None,
    cc_recipients: str | None = None,
    attachment_text: str | None = None,
    expected_department_id: int | None = None,
    expected_category: str | None = None,
    expected_requires_review: bool = True,
    criticality: str = "normal",
    notes: str | None = None,
    commit: bool = True,
) -> RoutingEvaluationCase:
    evaluation_set = get_evaluation_set(db, company_id, set_id)
    if evaluation_set is None:
        raise ValueError("Conjunto de evaluación no encontrado en este tenant.")
    if criticality not in {"normal", "high", "critical"}:
        raise ValueError("La criticidad debe ser normal, high o critical.")
    if expected_department_id is not None and db.scalar(
        select(Department.id).where(
            Department.company_id == company_id,
            Department.id == expected_department_id,
        )
    ) is None:
        raise ValueError("El departamento esperado no pertenece a este tenant.")
    case = RoutingEvaluationCase(
        company_id=company_id,
        evaluation_set_id=set_id,
        title=title.strip(),
        subject=subject.strip(),
        body=body.strip(),
        sender=sender.strip() if sender else None,
        recipients=recipients.strip() if recipients else None,
        cc_recipients=cc_recipients.strip() if cc_recipients else None,
        attachment_text=attachment_text.strip() if attachment_text else None,
        expected_department_id=expected_department_id,
        expected_category=expected_category.strip() if expected_category else None,
        expected_requires_review=expected_requires_review,
        criticality=criticality,
        notes=notes.strip() if notes else None,
    )
    db.add(case)
    db.flush()
    if commit:
        db.commit()
    return case


def update_evaluation_case(
    db: Session,
    company_id: int,
    set_id: int,
    case_id: int,
    *,
    title: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    sender: str | None = None,
    attachment_text: str | None = None,
    expected_department_id: int | None = None,
    expected_category: str | None = None,
    expected_requires_review: bool | None = None,
    criticality: str | None = None,
    active: bool | None = None,
    commit: bool = True,
) -> RoutingEvaluationCase:
    evaluation_set = get_evaluation_set(db, company_id, set_id)
    case = db.scalar(
        select(RoutingEvaluationCase).where(
            RoutingEvaluationCase.company_id == company_id,
            RoutingEvaluationCase.evaluation_set_id == set_id,
            RoutingEvaluationCase.id == case_id,
        )
    )
    if evaluation_set is None or case is None:
        raise ValueError("Caso de evaluación no encontrado en este tenant.")
    if title is not None:
        case.title = title.strip()
    if subject is not None:
        case.subject = subject.strip()
    if body is not None:
        case.body = body.strip()
    if sender is not None:
        case.sender = sender.strip() or None
    if attachment_text is not None:
        case.attachment_text = attachment_text.strip() or None
    if expected_department_id is not None and db.scalar(
        select(Department.id).where(
            Department.company_id == company_id,
            Department.id == expected_department_id,
        )
    ) is None:
        raise ValueError("El departamento esperado no pertenece a este tenant.")
    if expected_department_id is not None:
        case.expected_department_id = expected_department_id
    if expected_category is not None:
        case.expected_category = expected_category.strip() or None
    if expected_requires_review is not None:
        case.expected_requires_review = expected_requires_review
    if criticality is not None:
        if criticality not in {"normal", "high", "critical"}:
            raise ValueError("La criticidad debe ser normal, high o critical.")
        case.criticality = criticality
    if active is not None:
        case.active = active
    case.updated_at = _now()
    db.flush()
    if commit:
        db.commit()
    return case


def seed_demo_evaluation_set(db: Session, company_id: int, *, commit: bool = True) -> RoutingEvaluationSet:
    """Create the deterministic demo set without touching operational data or providers."""
    name = "KIBAK Routing Lab Demo"
    evaluation_set = db.scalar(
        select(RoutingEvaluationSet).where(
            RoutingEvaluationSet.company_id == company_id,
            RoutingEvaluationSet.name == name,
        )
    )
    if evaluation_set is None:
        evaluation_set = create_evaluation_set(
            db, company_id, name=name, description="Dataset sintético local para mejorar RoutingAgent.", commit=False
        )
    departments = {department.name.casefold(): department.id for department in list_departments(db, company_id, active_only=True)}
    existing_titles = set(
        db.scalars(
            select(RoutingEvaluationCase.title).where(
                RoutingEvaluationCase.company_id == company_id,
                RoutingEvaluationCase.evaluation_set_id == evaluation_set.id,
            )
        )
    )
    for item in synthetic_cases():
        if str(item["title"]) in existing_titles:
            continue
        expected_name = item.get("expected_department")
        expected_id = departments.get(str(expected_name).casefold()) if expected_name else None
        create_evaluation_case(
            db,
            company_id,
            evaluation_set.id,
            title=str(item["title"]),
            subject=str(item["subject"]),
            body=str(item["body"]),
            sender=str(item.get("sender") or "demo@demo.invalid"),
            recipients="routing@demo.invalid",
            attachment_text=str(item["attachment_text"]) if item.get("attachment_text") else None,
            expected_department_id=expected_id,
            expected_category=str(item["expected_category"]),
            expected_requires_review=bool(item["expected_requires_review"]),
            criticality=str(item.get("criticality") or "normal"),
            notes=str(item["notes"]) if item.get("notes") else None,
            commit=False,
        )
    if commit:
        db.commit()
    return evaluation_set


def evaluation_context(db: Session, company_id: int) -> dict[str, Any]:
    """Return the exact non-secret organizational context shown to the reviewer."""
    return build_department_routing_context(company_id, db)


def _mailbox_for_dry_run(db: Session, company_id: int, mailbox_id: int | None = None) -> Mailbox:
    statement = select(Mailbox).where(Mailbox.company_id == company_id)
    if mailbox_id is not None:
        statement = statement.where(Mailbox.id == mailbox_id)
    mailbox = db.scalar(statement.order_by(Mailbox.enabled.desc(), Mailbox.id))
    if mailbox is None:
        raise RoutingValidationError("Configura un buzón KIBAK activo para ejecutar el análisis.")
    return mailbox


def playground_analysis(
    db: Session,
    company_id: int,
    *,
    subject: str,
    body: str,
    sender: str | None = None,
    recipients: str | None = None,
    cc_recipients: str | None = None,
    attachment_text: str | None = None,
    mailbox_id: int | None = None,
    provider_call: RoutingProvider | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    mailbox = _mailbox_for_dry_run(db, company_id, mailbox_id)
    communication = Communication(
        company_id=company_id,
        mailbox_id=mailbox.id,
        external_message_id="playground-transient",
        sender_email=sender or None,
        to_recipients=recipients or None,
        cc_recipients=cc_recipients or None,
        subject=subject,
        body_text=body,
    )
    if attachment_text:
        communication.attachments = [
            CommunicationAttachment(
                company_id=company_id,
                filename="playground.txt",
                mime_type="text/plain",
                extracted_text=attachment_text,
            )
        ]
    runtime = RoutingLLMRuntime(
        db,
        company_id,
        provider_call=provider_call,
        user_id=user_id,
        input_reference="playground:transient",
        allow_disabled=provider_call is not None,
    )
    proposal = classify_communication(
        db,
        company_id,
        communication,
        runtime,
        routing_context=evaluation_context(db, company_id),
    )
    return {
        "proposal": proposal.model_dump(mode="json"),
        "prompt_execution": runtime.last_result or {},
        "context": evaluation_context(db, company_id),
        "mailbox": mailbox.email_address,
    }


def _auto_candidate(predicted: int | None, requires_review: bool, confidence: float) -> bool:
    return bool(
        predicted is not None
        and not requires_review
        and confidence >= DEFAULT_ROUTING_THRESHOLDS.auto_route_confidence
    )


def calculate_metrics(results: list[RoutingEvaluationResult]) -> dict[str, Any]:
    total = len(results)
    classified = [item for item in results if item.status == "completed"]
    expected = [item for item in classified if item.expected_department_id is not None]
    correct = [item for item in expected if item.correct_department]
    auto = [item for item in classified if item.auto_route_candidate]
    false_auto = [item for item in auto if item.false_auto_route]
    unclassified = [item for item in classified if item.predicted_department_id is None]
    correct_confidence = [item.confidence for item in expected if item.correct_department]
    error_confidence = [item.confidence for item in expected if not item.correct_department]
    matrix: dict[str, dict[str, int]] = {}
    for item in expected:
        expected_key = str(item.expected_department_id)
        predicted_key = str(item.predicted_department_id) if item.predicted_department_id is not None else "unclassified"
        matrix.setdefault(expected_key, {})[predicted_key] = matrix.setdefault(expected_key, {}).get(predicted_key, 0) + 1
    critical_false = [item for item in false_auto if item.criticality == "critical"]
    return {
        "total_cases": total,
        "completed_cases": len(classified),
        "error_cases": total - len(classified),
        "department_accuracy": round(len(correct) / len(expected), 4) if expected else None,
        "review_rate": round(sum(item.requires_review for item in classified) / len(classified), 4) if classified else None,
        "potential_automation_rate": round(len(auto) / len(classified), 4) if classified else None,
        "false_auto_route_rate": round(len(false_auto) / len(auto), 4) if auto else 0.0,
        "false_auto_route_count": len(false_auto),
        "unclassified_rate": round(len(unclassified) / len(classified), 4) if classified else None,
        "confidence_correct": round(sum(correct_confidence) / len(correct_confidence), 4) if correct_confidence else None,
        "confidence_error": round(sum(error_confidence) / len(error_confidence), 4) if error_confidence else None,
        "critical_false_auto_routes": len(critical_false),
        "confusion_matrix": matrix,
    }


def simulate_threshold(results: list[RoutingEvaluationResult], threshold: float) -> dict[str, Any]:
    threshold = min(max(float(threshold), 0.0), 1.0)
    candidates = [
        item for item in results
        if item.predicted_department_id is not None and not item.requires_review and item.confidence >= threshold
    ]
    false = [item for item in candidates if item.expected_department_id != item.predicted_department_id]
    return {
        "threshold": threshold,
        "candidate_count": len(candidates),
        "automation_rate": round(len(candidates) / len(results), 4) if results else 0.0,
        "false_auto_route_count": len(false),
        "critical_false_auto_routes": sum(item.criticality == "critical" for item in false),
    }


def compare_runs(first: RoutingEvaluationRun, second: RoutingEvaluationRun) -> dict[str, Any]:
    if first.company_id != second.company_id:
        raise ValueError("No se pueden comparar runs de tenants distintos.")
    first_by_case = {item.evaluation_case_id: item for item in first.results}
    second_by_case = {item.evaluation_case_id: item for item in second.results}
    common = sorted(first_by_case.keys() & second_by_case.keys())
    improved = regressed = automation_changed = 0
    for case_id in common:
        before, after = first_by_case[case_id], second_by_case[case_id]
        if bool(after.correct_department) and not bool(before.correct_department):
            improved += 1
        if bool(before.correct_department) and not bool(after.correct_department):
            regressed += 1
        if after.auto_route_candidate != before.auto_route_candidate:
            automation_changed += 1
    return {"common_cases": len(common), "improved": improved, "regressed": regressed, "automation_changed": automation_changed}


def run_evaluation(
    db: Session,
    company_id: int,
    set_id: int,
    *,
    provider_call: RoutingProvider | None = None,
    user_id: int | None = None,
    commit: bool = False,
) -> RoutingEvaluationRun:
    evaluation_set = get_evaluation_set(db, company_id, set_id)
    if evaluation_set is None:
        raise ValueError("Conjunto de evaluación no encontrado en este tenant.")
    if not evaluation_set.active:
        raise ValueError("El conjunto de evaluación está pausado.")
    mailbox = _mailbox_for_dry_run(db, company_id)
    cases = list(
        db.scalars(
            select(RoutingEvaluationCase)
            .where(
                RoutingEvaluationCase.company_id == company_id,
                RoutingEvaluationCase.evaluation_set_id == set_id,
                RoutingEvaluationCase.active.is_(True),
            )
            .order_by(RoutingEvaluationCase.id)
        )
    )
    context = evaluation_context(db, company_id)
    settings = db.scalar(select(LLMSettings).where(LLMSettings.company_id == company_id))
    run = RoutingEvaluationRun(
        company_id=company_id,
        evaluation_set_id=set_id,
        model=(settings.classification_model if settings else "configured-at-runtime"),
        temperature=settings.temperature if settings else None,
        thresholds_json=_json({
            "auto_route_confidence": DEFAULT_ROUTING_THRESHOLDS.auto_route_confidence,
            "review_confidence": DEFAULT_ROUTING_THRESHOLDS.review_confidence,
        }),
        context_snapshot_json=_json(context),
        status="running",
        total_cases=len(cases),
        created_by_user_id=user_id,
        started_at=_now(),
    )
    db.add(run)
    db.flush()
    runtime = RoutingLLMRuntime(
        db,
        company_id,
        provider_call=provider_call,
        user_id=user_id,
        allow_disabled=provider_call is not None,
    )
    for case in cases:
        started = perf_counter()
        proposal = None
        error_message = None
        status = "completed"
        try:
            transient = Communication(
                company_id=company_id,
                mailbox_id=mailbox.id,
                external_message_id=f"evaluation:{run.id}:{case.id}",
                sender_email=case.sender,
                to_recipients=case.recipients,
                cc_recipients=case.cc_recipients,
                subject=case.subject,
                body_text=case.body,
            )
            if case.attachment_text:
                transient.attachments = [
                    CommunicationAttachment(
                        company_id=company_id,
                        filename="evaluation.txt",
                        mime_type="text/plain",
                        extracted_text=case.attachment_text,
                    )
                ]
            runtime.input_reference = f"playground:evaluation:{evaluation_set.id}:case:{case.id}"
            proposal = classify_communication(db, company_id, transient, runtime, routing_context=context)
        except Exception as exc:  # Keep one bad case reviewable without hiding the run.
            status = "error"
            error_message = str(exc)[:500]
        predicted = proposal.proposed_department_id if proposal else None
        confidence = float(proposal.confidence) if proposal else 0.0
        requires_review = bool(proposal.requires_review) if proposal else True
        auto_route = _auto_candidate(predicted, requires_review, confidence)
        correct_department = None if case.expected_department_id is None else predicted == case.expected_department_id
        db.add(
            RoutingEvaluationResult(
                company_id=company_id,
                run_id=run.id,
                evaluation_case_id=case.id,
                criticality=case.criticality,
                expected_department_id=case.expected_department_id,
                predicted_department_id=predicted,
                expected_category=case.expected_category,
                predicted_category=proposal.category if proposal else None,
                expected_requires_review=case.expected_requires_review,
                requires_review=requires_review,
                confidence=confidence,
                reason=proposal.reason if proposal else None,
                alternative_department_id=proposal.alternative_department_id if proposal else None,
                correct_department=correct_department,
                auto_route_candidate=auto_route,
                false_auto_route=bool(auto_route and case.expected_department_id != predicted),
                latency_ms=int((perf_counter() - started) * 1000),
                status=status,
                error_message=error_message,
            )
        )
        db.flush()
        if runtime.last_result:
            run.prompt_name = str(runtime.last_result.get("prompt_name") or run.prompt_name)
            run.prompt_purpose = str(runtime.last_result.get("prompt_purpose") or run.prompt_purpose)
            run.prompt_version = int(runtime.last_result.get("prompt_version") or run.prompt_version)
            run.model = str(runtime.last_result.get("model") or run.model)
    results = list(db.scalars(select(RoutingEvaluationResult).where(RoutingEvaluationResult.company_id == company_id, RoutingEvaluationResult.run_id == run.id)))
    run.metrics_json = _json(calculate_metrics(results))
    run.status = "completed"
    run.completed_at = _now()
    db.flush()
    if commit:
        db.commit()
    return run
