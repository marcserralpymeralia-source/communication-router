"""Offline tooling for turning reviewed routing cases into editable knowledge.

The historical CSV is evidence, not a production prompt.  This module keeps
the legacy proposal and human correction side by side, derives a final human
destination, and provides a non-destructive import plan for reviewed proposals.
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Department, DepartmentKnowledge


# The offline source must provide the destination for NO_FORWARD. Keeping this
# unset prevents tenant-specific mailboxes from becoming code-level defaults.
NO_FORWARD_DESTINATION: str | None = None
KNOWLEDGE_TYPES = {"RESPONSIBILITY", "EXCLUSION", "DIRECTIVE", "EXCEPTION", "EXAMPLE"}
IMPORT_TYPE_MAP = {"DIRECTIVE": "guideline"}
_EMAIL_RE = re.compile(r"(?i)[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")


@dataclass(frozen=True)
class RoutingCase:
    case_id: str
    sender_or_source: str
    subject: str
    body: str
    legacy_prediction_raw: str
    legacy_prediction: str
    human_correction_raw: str
    final_human_destination: str
    human_reason: str
    status: str


def _first(row: dict[str, str], *names: str) -> str:
    for name in names:
        if name in row:
            return (row.get(name) or "").strip()
    return ""


def normalize_destination(value: str | None) -> str:
    """Normalize casing and separators without merging different domains."""

    value = re.sub(r"\s+", " ", str(value or "").strip())
    if not value:
        return ""
    return value.casefold()


def normalize_label(value: str | None) -> str:
    """Normalize a display label for comparison without using it as identity."""

    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", normalized.casefold()).strip()


def _emails(value: str) -> list[str]:
    return [normalize_destination(item) for item in _EMAIL_RE.findall(value)]


def reconstruct_human_destination(
    legacy_prediction: str,
    correction: str,
    *,
    no_forward_destination: str | None = None,
) -> str:
    """Resolve the reviewed destination while preserving the original inputs.

    ``OK`` accepts the legacy proposal, ``NO_FORWARD`` maps to the reviewed
    maintenance mailbox, and an explicit correction wins.  Multiple targets
    are preserved only when the reviewer explicitly indicates an additional
    destination (``+`` or ``també a``).
    """

    legacy = normalize_destination(legacy_prediction)
    correction = str(correction or "").strip()
    if not correction or correction.casefold() == "ok":
        return legacy
    if correction.casefold() == "no_forward":
        return normalize_destination(no_forward_destination)

    destinations = _emails(correction)
    if not destinations:
        return normalize_destination(correction)
    if "+" in correction:
        return "; ".join(dict.fromkeys(destinations))
    if "també a" in correction.casefold() or "tambien a" in correction.casefold():
        ordered = ([legacy] if legacy else []) + destinations
        return "; ".join(dict.fromkeys(item for item in ordered if item))
    return destinations[0]


def load_routing_cases(
    path: str | Path,
    *,
    no_forward_destination: str | None = None,
) -> list[RoutingCase]:
    """Load either the normalized CSV or the original six-column CSV."""

    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    cases: list[RoutingCase] = []
    normalized = "case_id" in (reader.fieldnames or [])
    for index, row in enumerate(rows, start=1):
        legacy_raw = _first(row, "agent_proposal_raw", "Reenviar a ", "Reenviar a")
        legacy = _first(row, "agent_proposal_normalized") or normalize_destination(legacy_raw)
        correction_raw = _first(row, "human_correction_raw", "correccion")
        derived = reconstruct_human_destination(
            legacy,
            correction_raw,
            no_forward_destination=no_forward_destination,
        )
        final = _first(row, "final_destination_normalized") or derived
        cases.append(
            RoutingCase(
                case_id=_first(row, "case_id") or str(index),
                sender_or_source=_first(row, "sender_or_source", "Adreça client/proveedor"),
                subject=_first(row, "subject", "Assumpte correu"),
                body=_first(row, "body", "Text del correu rebut"),
                legacy_prediction_raw=legacy_raw,
                legacy_prediction=legacy,
                human_correction_raw=correction_raw,
                final_human_destination=final,
                human_reason=_first(row, "human_reason", "motiu"),
                status=_first(row, "status") or ("normalized" if normalized else "original"),
            )
        )
    return cases


def compare_case_sources(original: Iterable[RoutingCase], normalized: Iterable[RoutingCase]) -> list[dict[str, Any]]:
    """Return row-level differences without copying message bodies to output."""

    differences: list[dict[str, Any]] = []
    for original_case, normalized_case in zip(original, normalized):
        fields = {}
        for field in ("sender_or_source", "subject", "legacy_prediction", "human_correction_raw"):
            left = getattr(original_case, field)
            right = getattr(normalized_case, field)
            if normalize_destination(left) != normalize_destination(right):
                fields[field] = {"original": left, "normalized": right}
        reconstructed = reconstruct_human_destination(
            original_case.legacy_prediction,
            original_case.human_correction_raw,
        )
        if normalize_destination(reconstructed) != normalize_destination(normalized_case.final_human_destination):
            fields["final_human_destination"] = {
                "reconstructed": reconstructed,
                "normalized": normalized_case.final_human_destination,
            }
        if fields:
            differences.append({"case_id": normalized_case.case_id, "fields": fields})
    return differences


def _destination_label(destination: str) -> str:
    local = destination.split("@", 1)[0].replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in local.split())


def _supporting_ids(cases: list[RoutingCase], destination: str, limit: int = 20) -> list[str]:
    return [case.case_id for case in cases if destination in case.final_human_destination.split("; ")][:limit]


def analyze_routing_cases(cases: list[RoutingCase]) -> dict[str, Any]:
    confusion = Counter((case.legacy_prediction or "(none)", case.final_human_destination or "(none)") for case in cases)
    final_destinations = Counter(case.final_human_destination for case in cases)
    corrections = Counter(case.human_correction_raw or "(blank)" for case in cases)
    sender_destinations: dict[str, set[str]] = defaultdict(set)
    for case in cases:
        sender_destinations[normalize_destination(case.sender_or_source)].update(case.final_human_destination.split("; "))
    multi_sender_signals = {
        sender: sorted(destinations)
        for sender, destinations in sender_destinations.items()
        if sender and len(destinations) > 1
    }
    variants: dict[str, set[str]] = defaultdict(set)
    for case in cases:
        for destination in _emails(case.human_correction_raw):
            variants[destination.split("@", 1)[0]].add(destination)
    return {
        "rows": len(cases),
        "unique_subject_body": len({(case.subject, case.body) for case in cases}),
        "status_counts": dict(Counter(case.status for case in cases)),
        "correction_counts": dict(corrections.most_common()),
        "final_destination_counts": dict(final_destinations.most_common()),
        "confusion_matrix": [
            {"legacy_prediction": left, "final_human_destination": right, "count": count}
            for (left, right), count in confusion.most_common()
        ],
        "exact_agreement": sum(
            count for (left, right), count in confusion.items() if left == right
        ),
        "multi_destination_cases": [case.case_id for case in cases if "; " in case.final_human_destination],
        "non_empty_reasons": sum(bool(case.human_reason.strip()) for case in cases),
        "sender_with_multiple_destinations": multi_sender_signals,
        "destination_variants_by_local_part": {key: sorted(value) for key, value in variants.items() if len(value) > 1},
        "unresolved_correction_labels": sorted(
            {
                case.human_correction_raw.strip()
                for case in cases
                if case.human_correction_raw.strip()
                and not _emails(case.human_correction_raw)
                and case.human_correction_raw.casefold() not in {"ok", "no_forward"}
            }
        ),
    }


def _case_destinations(case: RoutingCase) -> list[str]:
    return [normalize_destination(item) for item in case.final_human_destination.split("; ") if item]


def _supporting_case_ids_for_destination(cases: Iterable[RoutingCase], destination: str) -> list[str]:
    normalized = normalize_destination(destination)
    return [case.case_id for case in cases if normalized in _case_destinations(case)]


def build_department_reconciliation(
    cases: Iterable[RoutingCase],
    departments: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Reconcile observed human destinations against a tenant inventory.

    A destination email is the lookup key for this report, while the returned
    department id is the only identity suitable for a later tenant import.
    Local-part matches across different domains are intentionally ambiguous.
    """

    cases = list(cases)
    current = [dict(item) for item in departments]
    by_destination: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_local_part: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for department in current:
        destination = normalize_destination(department.get("destination_email"))
        if destination:
            by_destination[destination].append(department)
            by_local_part[destination.split("@", 1)[0]].append(department)

    observed = Counter(
        destination
        for case in cases
        for destination in _case_destinations(case)
    )
    records: list[dict[str, Any]] = []
    for destination, count in observed.most_common():
        exact = by_destination.get(destination, [])
        local_matches = by_local_part.get(destination.split("@", 1)[0], [])
        if len(exact) == 1:
            department = exact[0]
            status = "EXISTING"
            match = "EXACT_DESTINATION"
        elif exact:
            department = None
            status = "AMBIGUOUS"
            match = "DUPLICATE_DESTINATION"
        elif local_matches:
            department = None
            status = "AMBIGUOUS"
            match = "LOCAL_PART_DOMAIN_VARIANT"
        else:
            department = None
            status = "REALMENTE NUEVO"
            match = "NO_TENANT_EQUIVALENT"
        records.append(
            {
                "observed_destination": destination,
                "observed_count": count,
                "supporting_case_ids": _supporting_case_ids_for_destination(cases, destination),
                "status": status,
                "match": match,
                "department_id": department.get("id") if department else None,
                "department_name": department.get("name") if department else None,
                "current_destination": department.get("destination_email") if department else None,
                "current_active": department.get("active") if department else None,
                "local_part_candidates": [
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "destination_email": item.get("destination_email"),
                        "active": item.get("active"),
                    }
                    for item in local_matches
                ],
            }
        )
    observed_destinations = {record["observed_destination"] for record in records}
    return {
        "company_id": current[0].get("company_id") if current else None,
        "departments": records,
        "unused_current_departments": [
            item for item in current
            if normalize_destination(item.get("destination_email")) not in observed_destinations
        ],
    }


_PROPOSAL_REVIEW = {
    ("mantenimiento", "RESPONSIBILITY", "Gestión operativa de mantenimiento"): (
        "REVIEW",
        "La evidencia es amplia pero heterogénea; el departamento ya tiene un ámbito semántico vigente y la regla necesita revisión de fronteras.",
    ),
    ("mantenimiento", "EXCLUSION", "Frontera con Comercial"): (
        "APPROVE",
        "Directriz de frontera compatible con la responsabilidad vigente de Comercial y útil para separar oferta de ejecución.",
    ),
    ("mantenimiento", "EXAMPLE", "Servicio o pedido ya operativo"): (
        "REVIEW",
        "Los casos respaldan actividad operativa, pero el texto mezcla servicio, pedido y mantenimiento; no importar sin revisión editorial.",
    ),
    ("operaciones", "RESPONSIBILITY", "Coordinación de intervención"): (
        "APPROVE",
        "La responsabilidad coincide con el ámbito real de Operaciones; se espera SKIP si el Knowledge actual ya es equivalente.",
    ),
    ("operaciones", "EXCLUSION", "No confundir con reparación"): (
        "APPROVE",
        "Frontera explícita entre coordinación y actuación, respaldada por casos de ambas direcciones.",
    ),
    ("comercial", "RESPONSIBILITY", "Oferta y relación comercial"): (
        "REVIEW",
        "Solo hay dos casos con destino Comercial; evidencia insuficiente para ampliar el contrato sin revisión.",
    ),
    ("facturacion", "RESPONSIBILITY", "Facturación y pagos"): (
        "APPROVE",
        "Compatible con el ámbito real de Facturación; la comparación determinista debe decidir SKIP/UPDATE.",
    ),
    ("secretaria", "RESPONSIBILITY", "Back-office financiero"): (
        "REJECT",
        "El destino no existe en el inventario actual del tenant; requiere decisión organizativa antes de crear departamento.",
    ),
    ("inspeccion", "RESPONSIBILITY", "Cartera de inspección"): (
        "REJECT",
        "El destino no existe en el inventario actual del tenant; no se crea un departamento implícitamente.",
    ),
    ("mantenimientointegral", "RESPONSIBILITY", "Contratos de mantenimiento integral"): (
        "REVIEW",
        "Existe el mismo local-part en otro dominio (Quibac); el dominio es evidencia de identidad distinta y no puede fusionarse.",
    ),
    ("calidad", "RESPONSIBILITY", "Calidad y cumplimiento"): (
        "APPROVE",
        "Compatible con el ámbito real de Calidad; la comparación determinista debe decidir SKIP/UPDATE.",
    ),
    ("central", "EXCEPTION", "Escalado de contexto insuficiente"): (
        "REVIEW",
        "Central recibe casos heterogéneos; la excepción es razonable como hipótesis, pero no debe convertirse en regla amplia sin holdout.",
    ),
}


def _proposal_key(proposal: dict[str, Any]) -> tuple[str, str, str]:
    destination = normalize_destination(proposal.get("destination_email"))
    return (
        destination.split("@", 1)[0] if "@" in destination else normalize_label(proposal.get("department")),
        str(proposal.get("type") or "").strip().upper(),
        str(proposal.get("title") or "").strip(),
    )


def _semantic_tokens(value: str | None) -> set[str]:
    normalized = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").casefold()
    return {token for token in re.findall(r"[a-z0-9]{4,}", normalized)}


def _semantic_equivalent(left: str | None, right: str | None, threshold: float = 0.55) -> bool:
    left_tokens = _semantic_tokens(left)
    right_tokens = _semantic_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return True
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens)) >= threshold


def _find_equivalent_knowledge(
    db: Session,
    department: Department,
    proposal: dict[str, Any],
    kind: str,
) -> DepartmentKnowledge | None:
    items = list(
        db.scalars(
            select(DepartmentKnowledge).where(
                DepartmentKnowledge.department_id == department.id,
                DepartmentKnowledge.knowledge_type == kind,
                DepartmentKnowledge.active.is_(True),
            )
        ).all()
    )
    desired = str(proposal["content"]).strip()
    for item in items:
        if _semantic_equivalent(desired, item.content) or _semantic_equivalent(desired, department.description):
            return item
    return None


def _contradicting_case_ids(cases: Iterable[RoutingCase], destination: str) -> list[str]:
    normalized = normalize_destination(destination)
    return [
        case.case_id
        for case in cases
        if normalize_destination(case.legacy_prediction) == normalized
        and normalized not in _case_destinations(case)
    ]


def review_knowledge_proposals(
    proposals: Iterable[dict[str, Any]],
    cases: Iterable[RoutingCase],
    departments: Iterable[dict[str, Any]],
    current_knowledge: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Add deterministic evidence and a human-review status to proposals."""

    cases = list(cases)
    departments = [dict(item) for item in departments]
    current_knowledge = [dict(item) for item in current_knowledge]
    by_destination = {
        normalize_destination(item.get("destination_email")): item
        for item in departments
        if item.get("destination_email")
    }
    reviewed: list[dict[str, Any]] = []
    for proposal in proposals:
        item = dict(proposal)
        destination = normalize_destination(item.get("destination_email"))
        department = by_destination.get(destination)
        support_ids = _supporting_case_ids_for_destination(cases, destination)
        contradiction_ids = _contradicting_case_ids(cases, destination)
        status, note = _PROPOSAL_REVIEW.get(
            _proposal_key(item),
            ("REVIEW", "No existe una decisión editorial explícita para esta propuesta."),
        )
        related_name = str(item.get("related_department") or "").strip()
        related_department = next(
            (
                candidate for candidate in departments
                if normalize_label(candidate.get("name")) == normalize_label(related_name)
            ),
            None,
        ) if related_name else None
        item.update(
            {
                "department_id": department.get("id") if department else None,
                "related_department_id": related_department.get("id") if related_department else None,
                "support_count": len(support_ids),
                "contradiction_count": len(contradiction_ids),
                "supporting_case_ids": support_ids,
                "contradicting_case_ids": contradiction_ids,
                "status": status,
                "notes": note,
                "current_knowledge_matches": [
                    {
                        "id": current.get("id"),
                        "department_id": current.get("department_id"),
                        "title": current.get("title"),
                        "knowledge_type": current.get("knowledge_type"),
                    }
                    for current in current_knowledge
                    if department
                    and current.get("department_id") == department.get("id")
                    and str(current.get("knowledge_type") or "").casefold() == IMPORT_TYPE_MAP.get(
                        str(item.get("type") or "").upper(), str(item.get("type") or "").lower()
                    ).casefold()
                ],
            }
        )
        reviewed.append(item)
    return reviewed


def boundary_review(
    cases: Iterable[RoutingCase],
    departments: Iterable[dict[str, Any]],
    current_knowledge: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Summarize the requested department boundaries without making rules."""

    cases = list(cases)
    departments = list(departments)
    pairs = [
        ("Comercial", "Mantenimiento", "Oferta frente a ejecución operativa"),
        ("Operaciones", "Mantenimiento", "Coordinación frente a reparación"),
        ("Central", "Mantenimiento", "Contexto insuficiente frente a acción concreta"),
        ("Mantenimiento Integral", "Mantenimiento", "Dominio/contrato antes de fusionar ownership"),
        ("Facturación", "Mantenimiento", "Pago/documentación frente a actuación"),
        ("Información", "Mantenimiento", "Cambio de datos frente a intervención"),
        ("Central", "Secretaría", "Administrativo transversal frente a back-office no inventariado"),
        ("Técnico", "Mantenimiento", "Soporte de producto frente a avería"),
        ("Ingeniería", "Técnico", "Diseño/cálculo frente a soporte de producto"),
        ("Operaciones", "Prevención", "Coordinación de visita frente a seguridad/CAE"),
    ]
    by_name = {
        normalize_label(item.get("name")): item
        for item in departments
        if item.get("name")
    }

    def destinations_for(name: str) -> set[str]:
        local_part = normalize_label(name).replace(" ", "")
        current = {
            normalize_destination(by_name[name_key].get("destination_email"))
            for name_key in {normalize_label(name)}
            if name_key in by_name and by_name[name_key].get("destination_email")
        }
        observed = {
            destination
            for case in cases
            for destination in _case_destinations(case)
            if destination.split("@", 1)[0] == local_part
        }
        return current | observed

    results = []
    for left_name, right_name, candidate in pairs:
        left_destinations = destinations_for(left_name)
        right_destinations = destinations_for(right_name)
        boundary_destinations = left_destinations | right_destinations
        relevant = [
            case for case in cases
            if boundary_destinations.intersection(_case_destinations(case))
            or normalize_destination(case.legacy_prediction) in boundary_destinations
        ]
        directions = Counter()
        for case in relevant:
            legacy = normalize_destination(case.legacy_prediction)
            for target in _case_destinations(case):
                if legacy in boundary_destinations and target in boundary_destinations and legacy != target:
                    directions[f"{legacy} -> {target}"] += 1
        department_ids = {
            by_name.get(normalize_label(name), {}).get("id")
            for name in (left_name, right_name)
        }
        existing = [
            item for item in current_knowledge
            if item.get("department_id") in department_ids
        ]
        recommendation = "NO RULE NEEDED" if not directions and not relevant else "REVIEW"
        results.append(
            {
                "left": left_name,
                "right": right_name,
                "relevant_case_count": len(relevant),
                "confusion_directions": dict(directions),
                "candidate_rule": candidate,
                "supporting_case_ids": [case.case_id for case in relevant],
                "contradicting_case_ids": [],
                "current_knowledge_titles": [item.get("title") for item in existing],
                "recommendation": recommendation,
            }
        )
    return results


def multi_destination_analysis(cases: Iterable[RoutingCase]) -> list[dict[str, Any]]:
    """Classify explicit multi-recipient corrections for human review."""

    results = []
    for case in cases:
        destinations = _case_destinations(case)
        if len(destinations) < 2:
            continue
        local_parts = {destination.split("@", 1)[0] for destination in destinations}
        if {"facturacion", "operaciones"}.issubset(local_parts):
            classification = "MULTI-ACTION REAL"
            recommendation = "Mantener dos acciones explícitas en la evaluación; no usar el segundo destino como runner-up."
        elif {"prevencion", "operaciones"}.issubset(local_parts):
            classification = "CC/NOTIFICACIÓN"
            recommendation = "Operaciones parece responsable de la fecha; Prevención debe tratarse como informado/participante por el plan de seguridad."
        else:
            classification = "REVIEW"
            recommendation = "No inferir multi-forward automático sin contexto adicional."
        results.append(
            {
                "case_id": case.case_id,
                "sender": case.sender_or_source,
                "subject": case.subject,
                "body_summary": re.sub(r"\s+", " ", case.body).strip()[:240],
                "legacy_prediction": case.legacy_prediction,
                "human_correction": case.human_correction_raw,
                "destinations": destinations,
                "classification": classification,
                "recommendation": recommendation,
            }
        )
    return results


def build_evaluation_record(case: RoutingCase) -> dict[str, Any]:
    destinations = [item for item in case.final_human_destination.split("; ") if item]
    return {
        "case_id": case.case_id,
        "title": case.subject or f"Caso {case.case_id}",
        "subject": case.subject,
        "body": case.body,
        "sender": case.sender_or_source,
        "legacy_prediction": case.legacy_prediction,
        "human_correction": case.human_correction_raw,
        "expected_destination": destinations[0] if destinations else None,
        "expected_destinations": destinations,
        "expected_requires_review": len(destinations) != 1,
        "human_reason": case.human_reason,
        "status": case.status,
    }


def _resolve_destination_for_local_part(
    cases: Iterable[RoutingCase],
    local_part: str,
    departments: Iterable[dict[str, Any]] = (),
) -> str | None:
    observed = {
        destination
        for case in cases
        for destination in _case_destinations(case)
        if destination.split("@", 1)[0] == local_part
    }
    known = {
        normalize_destination(item.get("destination_email"))
        for item in departments
        if item.get("destination_email")
    }
    unrepresented = sorted(observed - known)
    candidates = unrepresented or sorted(observed)
    return candidates[0] if len(candidates) == 1 else None


def build_knowledge_proposals(
    cases: list[RoutingCase],
    departments: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Create a compact, reviewable proposal set from repeated destinations.

    The text is intentionally conservative: it describes observed ownership
    and labels every proposal as a hypothesis.  It is not imported implicitly.
    """

    templates = (
        ("mantenimiento", "RESPONSIBILITY", "Gestión operativa de mantenimiento", "Gestiona reparaciones, verificaciones, actuaciones y seguimiento de servicios ya contratados.", "HIGH"),
        ("mantenimiento", "EXCLUSION", "Frontera con Comercial", "Una consulta de precio u oferta sin una actuación o servicio ya aceptado no debe asignarse automáticamente a Mantenimiento.", "HIGH"),
        ("mantenimiento", "EXAMPLE", "Servicio o pedido ya operativo", "Cuando el correo requiere ejecutar o resolver una actuación de un servicio existente, Mantenimiento es el propietario observado.", "NORMAL"),
        ("operaciones", "RESPONSIBILITY", "Coordinación de intervención", "Coordina fechas, visitas, planificación y estado operativo de intervenciones ya previstas.", "HIGH"),
        ("operaciones", "EXCLUSION", "No confundir con reparación", "Preguntar cuándo acudir o coordinar una visita apunta a Operaciones; gestionar la reparación o actuación apunta a Mantenimiento.", "HIGH"),
        ("comercial", "RESPONSIBILITY", "Oferta y relación comercial", "Gestiona solicitudes de oferta, condiciones comerciales y oportunidades antes de una ejecución operativa confirmada.", "HIGH"),
        ("facturacion", "RESPONSIBILITY", "Facturación y pagos", "Gestiona revisión de facturas, transferencias, vencimientos y consultas administrativas de pago.", "HIGH"),
        ("secretaria", "RESPONSIBILITY", "Back-office financiero", "Gestiona avisos de confirming, abonos de órdenes de pago y aprobaciones administrativas observadas.", "HIGH"),
        ("inspeccion", "RESPONSIBILITY", "Cartera de inspección", "Gestiona verificaciones y pedidos de la cartera de inspección cuando el cliente o contrato identifica esa unidad.", "HIGH"),
        ("mantenimientointegral", "RESPONSIBILITY", "Contratos de mantenimiento integral", "Gestiona comunicaciones cuyo ownership depende del cliente o contrato de mantenimiento integral.", "HIGH"),
        ("calidad", "RESPONSIBILITY", "Calidad y cumplimiento", "Gestiona documentación de proveedores, informes y comunicaciones de calidad o cumplimiento.", "NORMAL"),
        ("central", "EXCEPTION", "Escalado de contexto insuficiente", "Central puede recibir comunicaciones sin acción identificable o actuar como punto de clasificación cuando falta contexto.", "NORMAL"),
    )
    proposals = []
    for local_part, kind, title, content, priority in templates:
        destination = _resolve_destination_for_local_part(cases, local_part, departments)
        if destination is None:
            continue
        support = _supporting_ids(cases, destination)
        support_count = sum(destination in case.final_human_destination.split("; ") for case in cases)
        if not support:
            continue
        proposals.append(
            {
                "department": _destination_label(destination),
                "destination_email": destination,
                "type": kind,
                "title": title,
                "content": content,
                "related_department": None,
                "priority": priority,
                "supporting_case_ids": support,
                "support_count": support_count,
                "confidence": "high" if support_count >= 5 else "medium" if support_count >= 3 else "low",
                "notes": "Hipótesis derivada de decisiones humanas; revisar antes de importar.",
            }
        )
    return proposals


def validate_knowledge_proposals(proposals: Any, cases: Iterable[RoutingCase] = ()) -> list[dict[str, Any]]:
    if not isinstance(proposals, list):
        raise ValueError("Knowledge proposals must be a list")
    case_ids = {case.case_id for case in cases}
    validated = []
    natural_keys = set()
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise ValueError("Each knowledge proposal must be an object")
        missing = [field for field in ("department", "type", "title", "content", "priority") if not str(proposal.get(field) or "").strip()]
        if missing:
            raise ValueError(f"Missing proposal fields: {', '.join(missing)}")
        kind = str(proposal["type"]).strip().upper()
        if kind not in KNOWLEDGE_TYPES:
            raise ValueError(f"Unsupported proposal type: {kind}")
        key = (str(proposal["department"]).strip().casefold(), kind, str(proposal["title"]).strip().casefold())
        if key in natural_keys:
            raise ValueError(f"Duplicate proposal: {key[0]} / {key[2]}")
        natural_keys.add(key)
        supporting = [str(item) for item in proposal.get("supporting_case_ids", [])]
        unknown_cases = sorted(set(supporting) - case_ids) if case_ids else []
        if unknown_cases:
            raise ValueError(f"Unknown supporting case IDs: {', '.join(unknown_cases)}")
        normalized = dict(proposal)
        normalized.update({"type": kind, "priority": str(proposal["priority"]).strip().upper()})
        status = str(proposal.get("status") or "APPROVE").strip().upper()
        if status not in {"APPROVE", "REVIEW", "REJECT"}:
            raise ValueError(f"Unsupported proposal status: {status}")
        normalized["status"] = status
        validated.append(normalized)
    return validated


def plan_knowledge_import(db: Session, company_id: int, proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Plan CREATE/UPDATE/SKIP/CONFLICT/REVIEW without changing the DB."""

    validated = validate_knowledge_proposals(proposals)
    departments = {
        department.name.strip().casefold(): department
        for department in db.scalars(select(Department).where(Department.company_id == company_id))
    }
    plan = []
    for proposal in validated:
        status = proposal.get("status", "APPROVE")
        if status in {"REVIEW", "REJECT"}:
            plan.append(
                {
                    "operation": "REVIEW",
                    "status": status,
                    "department": str(proposal["department"]).strip(),
                    "department_id": proposal.get("department_id"),
                    "title": str(proposal["title"]).strip(),
                    "support_count": proposal.get("support_count", 0),
                    "contradiction_count": proposal.get("contradiction_count", 0),
                    "notes": proposal.get("notes", ""),
                }
            )
            continue
        department_name = str(proposal["department"]).strip()
        department = None
        if proposal.get("department_id") is not None:
            department = db.scalar(
                select(Department).where(
                    Department.id == int(proposal["department_id"]),
                    Department.company_id == company_id,
                )
            )
        if department is None:
            department = departments.get(department_name.casefold())
        if department is None:
            plan.append(
                {
                    "operation": "NEW DEPARTMENT",
                    "status": status,
                    "department": department_name,
                    "destination_email": proposal.get("destination_email"),
                    "support_count": proposal.get("support_count", 0),
                }
            )
            continue
        kind = IMPORT_TYPE_MAP.get(proposal["type"], proposal["type"].lower())
        item = db.scalar(
            select(DepartmentKnowledge).where(
                DepartmentKnowledge.department_id == department.id,
                DepartmentKnowledge.title == str(proposal["title"]).strip(),
                DepartmentKnowledge.knowledge_type == kind,
            )
        )
        desired = {
            "department_id": department.id,
            "title": str(proposal["title"]).strip(),
            "content": str(proposal["content"]).strip(),
            "knowledge_type": kind,
            "priority": str(proposal["priority"]).strip().upper(),
        }
        equivalent_item = item or _find_equivalent_knowledge(db, department, proposal, kind)
        if equivalent_item is not None and item is None:
            operation = "SKIP"
        elif item is None:
            operation = "CREATE"
        elif all(getattr(item, field) == value for field, value in desired.items() if field != "department_id"):
            operation = "SKIP"
        else:
            operation = "UPDATE"
        plan.append({
            "operation": operation,
            "status": status,
            "department": department_name,
            "existing_knowledge_id": equivalent_item.id if equivalent_item is not None else None,
            "support_count": proposal.get("support_count", 0),
            "contradiction_count": proposal.get("contradiction_count", 0),
            "notes": proposal.get("notes", ""),
            **desired,
        })
    return plan


def apply_knowledge_proposals(db: Session, company_id: int, proposals: list[dict[str, Any]]) -> dict[str, int]:
    """Apply only reviewed proposals; caller owns commit/rollback."""

    from app.departments.service import create_department_knowledge, update_department_knowledge

    plan = plan_knowledge_import(db, company_id, proposals)
    blocked = {"NEW DEPARTMENT", "REVIEW", "CONFLICT"}
    if any(item["operation"] in blocked for item in plan):
        raise ValueError("Resolve NEW DEPARTMENT/REVIEW/CONFLICT operations before importing knowledge")
    counters = Counter(item["operation"] for item in plan)
    department_by_name = {
        department.name.strip().casefold(): department
        for department in db.scalars(select(Department).where(Department.company_id == company_id))
    }
    for proposal in validate_knowledge_proposals(proposals):
        if proposal.get("status", "APPROVE") != "APPROVE":
            continue
        department = None
        if proposal.get("department_id") is not None:
            department = db.scalar(
                select(Department).where(
                    Department.id == int(proposal["department_id"]),
                    Department.company_id == company_id,
                )
            )
        if department is None:
            department = department_by_name.get(str(proposal["department"]).strip().casefold())
        if department is None:
            raise ValueError(f"Department not found for approved proposal: {proposal['department']}")
        kind = IMPORT_TYPE_MAP.get(proposal["type"], proposal["type"].lower())
        existing = db.scalar(
            select(DepartmentKnowledge).where(
                DepartmentKnowledge.department_id == department.id,
                DepartmentKnowledge.title == str(proposal["title"]).strip(),
                DepartmentKnowledge.knowledge_type == kind,
            )
        )
        operation_item = next(
            item for item in plan
            if item.get("department") == proposal["department"]
            and item.get("title") == proposal["title"]
        )
        if existing is None and operation_item.get("existing_knowledge_id"):
            existing = db.get(DepartmentKnowledge, operation_item["existing_knowledge_id"])
        if operation_item["operation"] == "SKIP":
            continue
        if existing is None:
            create_department_knowledge(
                db, company_id, department.id, title=proposal["title"], content=proposal["content"],
                knowledge_type=kind, priority=proposal["priority"], commit=False,
            )
        elif operation_item["operation"] == "UPDATE":
            update_department_knowledge(
                db, company_id, existing.id, title=proposal["title"], content=proposal["content"],
                knowledge_type=kind, priority=proposal["priority"], commit=False,
            )
    return {key.lower(): value for key, value in counters.items()}


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


__all__ = [
    "NO_FORWARD_DESTINATION",
    "RoutingCase",
    "analyze_routing_cases",
    "apply_knowledge_proposals",
    "boundary_review",
    "build_evaluation_record",
    "build_department_reconciliation",
    "build_knowledge_proposals",
    "compare_case_sources",
    "json_dump",
    "load_routing_cases",
    "multi_destination_analysis",
    "normalize_destination",
    "normalize_label",
    "plan_knowledge_import",
    "reconstruct_human_destination",
    "review_knowledge_proposals",
    "validate_knowledge_proposals",
]
