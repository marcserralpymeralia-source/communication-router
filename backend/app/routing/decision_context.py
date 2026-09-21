"""Small, tenant-scoped retrieval context for the routing judge.

This module deliberately uses the existing DepartmentKnowledge ontology.  The
retrieval is deterministic and local so it does not add an LLM call or a
vector-service dependency before the single routing inference.
"""

from __future__ import annotations

import os
import re
import unicodedata
from functools import lru_cache
from math import sqrt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Communication, Department, DepartmentKnowledge


ROUTING_CANDIDATE_LIMIT = 3
MAX_ROUTING_CANDIDATES = 4
MAX_KNOWLEDGE_TOTAL = 10
MAX_KNOWLEDGE_PER_DEPARTMENT = 4
MAX_DECISION_CONTEXT_CHARS = 12000

# The weights are intentionally inspectable: semantic overlap leads, while
# knowledge type and editorial priority can resolve close department borders.
RERANK_WEIGHTS = {
    "profile_relevance": 0.55,
    "knowledge_relevance": 0.30,
    "type": 0.08,
    "priority": 0.05,
    "related_candidate": 0.12,
}
KNOWLEDGE_TYPE_WEIGHTS = {
    "guideline": 1.00,
    "exception": 0.95,
    "exclusion": 0.90,
    "responsibility": 0.72,
    "example": 0.55,
}
PRIORITY_WEIGHTS = {"CRITICAL": 1.00, "HIGH": 0.82, "NORMAL": 0.60, "LOW": 0.38}
BOUNDARY_TYPES = {"guideline", "exception", "exclusion"}
_STOPWORDS = {
    "a", "al", "ante", "con", "de", "del", "el", "en", "entre", "es", "esta",
    "este", "la", "las", "lo", "los", "más", "no", "para", "por", "que", "se",
    "su", "sus", "un", "una", "y", "o", "the", "to", "of", "and", "is", "for",
}


def _setting_limit(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, 1), maximum)


def routing_candidate_limit() -> int:
    return _setting_limit("ROUTING_CANDIDATE_LIMIT", ROUTING_CANDIDATE_LIMIT, MAX_ROUTING_CANDIDATES)


def routing_knowledge_limits() -> tuple[int, int]:
    return (
        _setting_limit("ROUTING_MAX_KNOWLEDGE_TOTAL", MAX_KNOWLEDGE_TOTAL, MAX_KNOWLEDGE_TOTAL),
        _setting_limit("ROUTING_MAX_KNOWLEDGE_PER_DEPARTMENT", MAX_KNOWLEDGE_PER_DEPARTMENT, MAX_KNOWLEDGE_PER_DEPARTMENT),
    )


def _tokens(value: str | None) -> set[str]:
    normalized = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    return {token for token in re.findall(r"[a-z0-9]{3,}", normalized) if token not in _STOPWORDS}


def _relevance(query: str, text: str) -> float:
    query_tokens = _tokens(query)
    text_tokens = _tokens(text)
    if not query_tokens or not text_tokens:
        return 0.0
    overlap = len(query_tokens & text_tokens)
    return min(1.0, overlap / sqrt(len(query_tokens) * len(text_tokens)))


def _word_limit(value: str, limit: int = 120) -> str:
    words = value.split()
    if len(words) <= limit:
        return value.strip()
    return " ".join(words[:limit]).rstrip(" ,.;:")


@lru_cache(maxsize=512)
def _cached_profile(
    department_id: int,
    name: str,
    description: str,
    semantic_items: tuple[tuple[str, str, str, str], ...],
) -> str:
    """Cache only semantic department inputs, never members or RACI."""

    responsibilities = [f"{title}: {content}" for kind, title, content, _priority in semantic_items if kind == "responsibility"]
    boundaries = [f"{title}: {content}" for kind, title, content, _priority in semantic_items if kind in {"exclusion", "guideline", "exception"}]
    parts = [name.strip()]
    if description.strip():
        parts.append(description.strip())
    if responsibilities:
        parts.append("Responsabilidades: " + " ".join(responsibilities))
    if boundaries:
        parts.append("Fronteras y excepciones: " + " ".join(boundaries))
    return _word_limit(" ".join(parts), 120)


def build_department_routing_profile(department: Department, items: list[DepartmentKnowledge]) -> str:
    semantic_items = tuple(
        sorted(
            (
                str(item.knowledge_type).strip().lower(),
                str(item.title or "").strip(),
                str(item.content or "").strip(),
                str(item.priority or "NORMAL").strip().upper(),
            )
            for item in items
            if item.active and str(item.knowledge_type).strip().lower() != "example"
        )
    )
    return _cached_profile(
        department.id,
        department.name,
        department.description or "",
        semantic_items,
    )


def _communication_query(db: Session, communication: Communication) -> str:
    parts = [communication.subject or "", communication.body_text or ""]
    if communication.thread_id:
        previous = db.scalars(
            select(Communication)
            .where(
                Communication.company_id == communication.company_id,
                Communication.thread_id == communication.thread_id,
                Communication.id != communication.id,
            )
            .order_by(Communication.received_at.desc().nullslast(), Communication.id.desc())
            .limit(2)
        ).all()
        parts.extend(f"{item.subject or ''} {item.body_text or ''}" for item in previous)
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _priority_value(value: str | None) -> str:
    return str(value or "NORMAL").strip().upper()


def _knowledge_score(item: DepartmentKnowledge, query: str, candidate_ids: set[int]) -> tuple[float, bool]:
    kind = str(item.knowledge_type).strip().lower()
    priority = _priority_value(item.priority)
    related = item.related_department_id in candidate_ids and item.related_department_id != item.department_id
    score = (
        RERANK_WEIGHTS["knowledge_relevance"] * _relevance(query, f"{item.title} {item.content}")
        + RERANK_WEIGHTS["type"] * KNOWLEDGE_TYPE_WEIGHTS.get(kind, 0.5)
        + RERANK_WEIGHTS["priority"] * PRIORITY_WEIGHTS.get(priority, PRIORITY_WEIGHTS["NORMAL"])
        + (RERANK_WEIGHTS["related_candidate"] if related and kind in BOUNDARY_TYPES else 0.0)
    )
    return score, related


def build_routing_decision_context(
    db: Session,
    company_id: int,
    communication: Communication,
) -> dict[str, Any]:
    """Return candidates and evidence only from the current tenant."""

    if communication.company_id != company_id:
        raise ValueError("Communication does not belong to the requested tenant")

    departments = db.scalars(
        select(Department)
        .where(Department.company_id == company_id, Department.active.is_(True))
        .order_by(Department.name, Department.id)
    ).all()
    department_ids = [department.id for department in departments]
    knowledge_items = (
        db.scalars(
            select(DepartmentKnowledge)
            .where(
                DepartmentKnowledge.department_id.in_(department_ids or [-1]),
                DepartmentKnowledge.active.is_(True),
            )
            .order_by(DepartmentKnowledge.id)
        ).all()
        if department_ids
        else []
    )
    knowledge_by_department: dict[int, list[DepartmentKnowledge]] = {department.id: [] for department in departments}
    for item in knowledge_items:
        knowledge_by_department.setdefault(item.department_id, []).append(item)

    query = _communication_query(db, communication)
    scored_departments = []
    for department in departments:
        profile = build_department_routing_profile(department, knowledge_by_department.get(department.id, []))
        profile_relevance = _relevance(query, profile)
        item_relevance = max(
            (_relevance(query, f"{item.title} {item.content}") for item in knowledge_by_department.get(department.id, [])),
            default=0.0,
        )
        score = (
            RERANK_WEIGHTS["profile_relevance"] * profile_relevance
            + RERANK_WEIGHTS["knowledge_relevance"] * item_relevance
            + RERANK_WEIGHTS["type"] * (1.0 if knowledge_by_department.get(department.id) else 0.0)
        )
        scored_departments.append((score, department, profile, profile_relevance))
    scored_departments.sort(key=lambda value: (-value[0], value[1].name.casefold(), value[1].id))
    selected = scored_departments[: routing_candidate_limit()]
    selected_ids = {department.id for _score, department, _profile, _relevance_score in selected}

    evidence_candidates: list[dict[str, Any]] = []
    for _candidate_score, department, _profile, _profile_relevance in selected:
        items = []
        for item in knowledge_by_department.get(department.id, []):
            score, related = _knowledge_score(item, query, selected_ids)
            kind = str(item.knowledge_type).strip().lower()
            items.append(
                {
                    "item": item,
                    "score": score,
                    "related": related,
                    "is_rule": kind in BOUNDARY_TYPES,
                }
            )
        items.sort(key=lambda value: (-int(value["is_rule"]), -value["score"], value["item"].id))
        evidence_candidates.extend(items[: routing_knowledge_limits()[1]])

    total_limit, _per_department_limit = routing_knowledge_limits()
    evidence_candidates.sort(
        key=lambda value: (-int(value["is_rule"]), -value["score"], value["item"].id)
    )
    evidence_candidates = evidence_candidates[:total_limit]
    selected_evidence = []
    context_chars = 0
    for value in evidence_candidates:
        item = value["item"]
        item_chars = len(str(item.title or "")) + len(str(item.content or ""))
        if selected_evidence and context_chars + item_chars > MAX_DECISION_CONTEXT_CHARS:
            continue
        selected_evidence.append(value)
        context_chars += item_chars
    evidence_candidates = selected_evidence
    department_names = {department.id: department.name for department in departments}
    evidence = []
    for value in evidence_candidates:
        item = value["item"]
        kind = str(item.knowledge_type).strip().lower()
        evidence.append(
            {
                "evidence_id": f"K{item.id}",
                "knowledge_id": item.id,
                "department_id": item.department_id,
                "department_name": department_names.get(item.department_id),
                "type": kind,
                "title": item.title,
                "content": item.content,
                "related_department_id": item.related_department_id,
                "related_department_name": department_names.get(item.related_department_id),
                "priority": _priority_value(item.priority),
                "score": round(value["score"], 4),
            }
        )
    boundary_rules = [
        {
            "evidence_id": item["evidence_id"],
            "department_id": item["department_id"],
            "related_department_id": item["related_department_id"],
            "type": item["type"],
        }
        for item in evidence
        if item["related_department_id"] in selected_ids and item["type"] in BOUNDARY_TYPES
    ]
    candidates = [
        {
            "department_id": department.id,
            "name": department.name,
            "profile": profile,
            "candidate_score": round(candidate_score, 4),
            "profile_relevance": round(profile_relevance, 4),
            "active": True,
        }
        for candidate_score, department, profile, profile_relevance in selected
    ]
    thread_context = []
    if communication.thread_id:
        thread_context = [
            {
                "subject": item.subject,
                "body": (item.body_text or "")[:600],
            }
            for item in db.scalars(
                select(Communication)
                .where(
                    Communication.company_id == company_id,
                    Communication.thread_id == communication.thread_id,
                    Communication.id != communication.id,
                )
                .order_by(Communication.received_at.desc().nullslast(), Communication.id.desc())
                .limit(2)
            ).all()
        ]
    return {
        "version": "routing-decision-context.v1",
        "departments": candidates,
        "knowledge": evidence,
        "boundary_rules": boundary_rules,
        "thread_context": thread_context,
        "retrieval": {
            "candidate_limit": routing_candidate_limit(),
            "knowledge_limit": total_limit,
            "knowledge_per_department": routing_knowledge_limits()[1],
            "candidate_scores": {str(item["department_id"]): item["candidate_score"] for item in candidates},
            "evidence_ids": [item["evidence_id"] for item in evidence],
        },
    }


__all__ = [
    "BOUNDARY_TYPES",
    "MAX_KNOWLEDGE_PER_DEPARTMENT",
    "MAX_KNOWLEDGE_TOTAL",
    "MAX_DECISION_CONTEXT_CHARS",
    "MAX_ROUTING_CANDIDATES",
    "RERANK_WEIGHTS",
    "build_department_routing_profile",
    "build_routing_decision_context",
    "routing_candidate_limit",
    "routing_knowledge_limits",
]
