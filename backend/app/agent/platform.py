from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parseaddr
from math import isfinite
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.agent.extraction import OrderExtractionInput, OrderExtractionResult, extract_order
from app.agent.model_catalog import DEFAULT_OPENAI_MODEL, LEGACY_OPENAI_MODEL_FALLBACK, resolve_openai_runtime_model
from app.core.channel_identity import inbound_channel_key
from app.core.encryption import decrypt_secret
from app.db.models import (
    Alert,
    Email,
    EmailAttachment,
    Company,
    Customer,
    CustomerAlias,
    CustomerContact,
    CustomerContactPoint,
    CustomerDomain,
    CustomerProductKnowledge,
    EmailSettings,
    ExportJob,
    DecisionSettings,
    InputChannel,
    InboundMessage,
    LearnedAlias,
    NormalizedInput,
    Order,
    OrderLine,
    OrderReview,
    Product,
    ProductAlias,
    PromptTemplate,
    PromptVersion,
    RagCase,
    RagDocument,
    ScoringResult,
    ScoringSettings,
    LLMSettings,
)
from app.logs.service import log_action
from app.knowledge.service import retrieve_product_knowledge
from app.orders.state import ORDER_STATE
from app.orders.scoring import calculate_order_score
from app.semantic_retrieval.embeddings import EmbeddingProvider
from app.semantic_retrieval.products import find_product_candidates
from app.settings.integrations import classify_sample, extract_sample
from app.settings.service import get_or_create_settings


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class InboundPayload:
    company_id: int
    channel_key: str
    source_external_id: str | None = None
    sender: str | None = None
    recipient: str | None = None
    subject: str | None = None
    raw_text: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    attachments: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class PipelineResult:
    ok: bool
    status: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class BaseChannelIngestionService(Protocol):
    channel_key: str

    def ingest(self, db: Session, payload: InboundPayload, user=None) -> PipelineResult:
        raise NotImplementedError


class ChannelIngestionService:
    def __init__(self) -> None:
        self._registry: dict[str, BaseChannelIngestionService] = {}

    def register(self, service: BaseChannelIngestionService) -> None:
        self._registry[service.channel_key] = service

    def ingest(self, db: Session, payload: InboundPayload, user=None) -> PipelineResult:
        service = self._registry.get(payload.channel_key)
        if not service:
            return PipelineResult(ok=False, status="unsupported_channel", message=f"Canal no soportado: {payload.channel_key}")
        return service.ingest(db, payload, user=user)

    def active_channels(self, db: Session, company_id: int) -> list[InputChannel]:
        return db.scalars(
            select(InputChannel).where(InputChannel.company_id == company_id, InputChannel.is_active == True)  # noqa: E712
        ).all()


class EmailIngestionService:
    channel_key = "email"

    def ingest(self, db: Session, payload: InboundPayload, user=None) -> PipelineResult:
        return PipelineResult(ok=True, status="received", message="Email registrado para el pipeline común.")

    def sync(self, db: Session, company_id: int) -> dict:
        from app.settings.integrations import read_latest_imap_emails

        settings = get_or_create_settings(db, EmailSettings, company_id)
        return read_latest_imap_emails(db, settings, company_id)


class WhatsAppIngestionService:
    channel_key = "whatsapp"

    def ingest(self, db: Session, payload: InboundPayload, user=None) -> PipelineResult:
        raise NotImplementedError("WhatsApp todavía no está implementado.")


class VoiceIngestionService:
    channel_key = "voice"

    def ingest(self, db: Session, payload: InboundPayload, user=None) -> PipelineResult:
        raise NotImplementedError("Voz todavía no está implementado.")


class SocialIngestionService:
    channel_key = "social"

    def ingest(self, db: Session, payload: InboundPayload, user=None) -> PipelineResult:
        raise NotImplementedError("Redes sociales todavía no están implementadas.")


class NormalizationService:
    def normalize(self, text: str | None, *, preserve_lines: bool = False) -> str:
        raw = (text or "").strip()
        if not raw:
            return ""
        if preserve_lines:
            return "\n".join(line.strip() for line in raw.splitlines() if line.strip())
        return " ".join(raw.split())


class PDFExtractionService:
    def extract_text(self, path: str) -> str:
        return ""


class OCRService:
    def extract_text(self, path: str) -> str:
        return ""


class SpeechToTextService:
    def transcribe(self, path: str) -> str:
        return ""


class ClassificationAgent:
    def classify(self, text: str) -> dict[str, Any]:
        return {"tipo": "pedido", "confianza": 0.0, "motivo": "Clasificador pendiente de conectar."}


class OrderExtractionAgent:
    def extract(self, text: str) -> dict[str, Any]:
        return {"cliente": {}, "pedido": {"lineas": []}, "motivos_revision": ["Motor de extracción pendiente"]}


def _normalized_identity(value: str | None) -> str:
    if not value:
        return ""
    return "".join(char for char in value.lower().strip() if char.isalnum())


def _normalize_match_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.lower().strip()
    normalized = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def _match_text_similarity(left: str | None, right: str | None) -> float:
    left_normalized = _normalize_match_text(left)
    right_normalized = _normalize_match_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    sequence_score = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    token_score = SequenceMatcher(
        None,
        " ".join(sorted(left_normalized.split())),
        " ".join(sorted(right_normalized.split())),
    ).ratio()
    return max(sequence_score, token_score)


def _product_name_is_compatible(detected_name: str | None, product: Product, threshold: float = 0.55) -> bool:
    if not detected_name:
        return True
    score = _match_text_similarity(detected_name, product.name)
    if product.description:
        score = max(score, _match_text_similarity(detected_name, product.description))
    return score >= threshold


def _detected_customer_is_tenant(
    db: Session,
    company_id: int,
    *,
    detected_name: str | None = None,
    tax_id: str | None = None,
) -> bool:
    company = db.get(Company, company_id)
    if not company:
        return False

    detected_name_normalized = _normalized_identity(detected_name)
    tenant_names = {
        _normalized_identity(company.name),
        _normalized_identity(company.legal_name),
    }
    tenant_names.discard("")

    if detected_name_normalized and detected_name_normalized in tenant_names:
        return True

    detected_tax_id = _normalized_identity(tax_id)
    tenant_tax_id = _normalized_identity(company.tax_id)
    if detected_tax_id and tenant_tax_id and detected_tax_id == tenant_tax_id:
        return True

    return False


class CustomerMatchingService:
    def match(self, db: Session, company_id: int, *, detected_name: str | None = None, detected_code: str | None = None, sender: str | None = None) -> tuple[Customer | None, str, float]:
        sender_clean = parseaddr(sender or "")[1].strip().lower()

        if detected_code:
            customer = db.scalar(select(Customer).where(Customer.company_id == company_id, Customer.code == detected_code))
            if customer:
                return customer, "codigo", 1.0
        if sender_clean:
            customer = db.scalar(
                select(Customer).where(
                    Customer.company_id == company_id,
                    Customer.primary_email.ilike(sender_clean),
                )
            )
            if customer:
                return customer, "email_principal", 0.99

            exact_contact = db.scalar(
                select(CustomerContact).where(
                    CustomerContact.company_id == company_id,
                    CustomerContact.email.ilike(sender_clean),
                )
            )
            if exact_contact:
                customer = db.get(Customer, exact_contact.customer_id)
                if customer:
                    return customer, "email_contacto", 0.99

            exact_point = db.scalar(
                select(CustomerContactPoint).where(
                    CustomerContactPoint.company_id == company_id,
                    CustomerContactPoint.type == "email",
                    CustomerContactPoint.active == True,  # noqa: E712
                    CustomerContactPoint.value.ilike(sender_clean),
                )
            )
            if exact_point:
                customer = db.get(Customer, exact_point.customer_id)
                if customer:
                    return customer, "contact_point_email", 0.99
        if sender_clean and "@" in sender_clean:
            domain = sender_clean.split("@", 1)[1]

            domain_points = db.scalars(
                select(CustomerContactPoint).where(
                    CustomerContactPoint.company_id == company_id,
                    CustomerContactPoint.type == "domain",
                    CustomerContactPoint.active == True,  # noqa: E712
                    CustomerContactPoint.value.ilike(domain),
                )
            ).all()
            domain_customer_ids = {point.customer_id for point in domain_points}
            if len(domain_customer_ids) == 1:
                customer = db.get(Customer, next(iter(domain_customer_ids)))
                if customer:
                    return customer, "contact_point_domain", 0.97

            contacts = db.scalars(
                select(CustomerContact).where(
                    CustomerContact.company_id == company_id,
                    CustomerContact.email.ilike(f"%@{domain}"),
                )
            ).all()
            contact_customer_ids = {contact.customer_id for contact in contacts}
            if len(contact_customer_ids) == 1:
                customer = db.get(Customer, next(iter(contact_customer_ids)))
                if customer:
                    return customer, "dominio_contacto_unico", 0.96
        if detected_name:
            point_alias = db.scalar(
                select(CustomerContactPoint).where(
                    CustomerContactPoint.company_id == company_id,
                    CustomerContactPoint.active == True,  # noqa: E712
                    CustomerContactPoint.value.ilike(detected_name),
                )
            )
            if point_alias:
                customer = db.get(Customer, point_alias.customer_id)
                if customer:
                    return customer, "contact_point", 0.95
            learned = db.scalar(
                select(LearnedAlias).where(
                    LearnedAlias.company_id == company_id,
                    LearnedAlias.alias_type == "customer",
                    LearnedAlias.alias.ilike(detected_name),
                )
            )
            if learned and learned.customer_id:
                customer = db.get(Customer, learned.customer_id)
                if customer:
                    return customer, "alias_aprendido", 0.98
            alias = db.scalar(
                select(CustomerAlias).where(
                    CustomerAlias.company_id == company_id,
                    CustomerAlias.alias.ilike(detected_name),
                )
            )
            if alias:
                customer = db.get(Customer, alias.customer_id)
                if customer:
                    return customer, "alias", 0.92
            customers = db.scalars(select(Customer).where(Customer.company_id == company_id)).all()
            if customers:
                best = max(customers, key=lambda customer: SequenceMatcher(None, detected_name.lower(), customer.fiscal_name.lower()).ratio())
                score = SequenceMatcher(None, detected_name.lower(), best.fiscal_name.lower()).ratio()
                if score >= 0.65:
                    return best, "nombre_aproximado", score
        return None, "sin_identificar", 0.0


class ProductMatchingService:
    def match(self, db: Session, company_id: int, *, reference: str | None = None, detected_name: str | None = None) -> tuple[Product | None, str, float]:
        if reference:
            product = db.scalar(
                select(Product).where(
                    Product.company_id == company_id,
                    Product.reference == reference,
                )
            )
            if product and _product_name_is_compatible(detected_name, product):
                return product, "referencia_exacta", 1.0

            normalized_reference = reference.strip()
            if len(normalized_reference) >= 6:
                partial_matches = db.scalars(
                    select(Product).where(
                        Product.company_id == company_id,
                        Product.reference.ilike(f"{normalized_reference}%"),
                    )
                ).all()

                if len(partial_matches) == 1:
                    return partial_matches[0], "referencia_parcial_unica", 0.95
        if detected_name:
            learned = db.scalar(
                select(LearnedAlias).where(
                    LearnedAlias.company_id == company_id,
                    LearnedAlias.alias_type == "product",
                    LearnedAlias.alias.ilike(detected_name),
                )
            )
            if learned and learned.product_id:
                product = db.get(Product, learned.product_id)
                if product:
                    return product, "alias_aprendido", 0.98
            alias = db.scalar(
                select(ProductAlias).where(
                    ProductAlias.company_id == company_id,
                    ProductAlias.alias.ilike(detected_name),
                )
            )
            if alias:
                product = db.get(Product, alias.product_id)
                if product:
                    return product, "alias", 0.92
            products = db.scalars(select(Product).where(Product.company_id == company_id)).all()
            if products:
                best = max(products, key=lambda product: _match_text_similarity(detected_name, product.name))
                score = _match_text_similarity(detected_name, best.name)
                if score >= 0.6:
                    return best, "nombre_aproximado", score
        return None, "sin_referencia", 0.0


class RAGRetrievalService:
    def retrieve_cases(
        self,
        db: Session,
        company_id: int,
        query: str,
        limit: int = 5,
        *,
        customer_id: int | None = None,
        order_id: int | None = None,
    ) -> list[RagCase]:
        if not query.strip() and not customer_id and not order_id:
            return []
        pattern = f"%{query.strip()[:80]}%" if query.strip() else "%"
        conditions = [RagCase.company_id == company_id]
        if customer_id:
            conditions.append(RagCase.customer_id == customer_id)
        if order_id:
            conditions.append(RagCase.order_id == order_id)
        return db.scalars(
            select(RagCase)
            .where(*conditions, RagCase.summary.ilike(pattern))
            .order_by(RagCase.created_at.desc())
            .limit(limit)
        ).all()

    def retrieve_documents(
        self,
        db: Session,
        company_id: int,
        query: str,
        limit: int = 5,
        *,
        source_entity: str | None = None,
        source_entity_id: int | None = None,
    ) -> list[RagDocument]:
        if not query.strip() and source_entity is None and source_entity_id is None:
            return []
        pattern = f"%{query.strip()[:80]}%" if query.strip() else "%"
        conditions = [RagDocument.company_id == company_id]
        if source_entity:
            conditions.append(RagDocument.source_entity == source_entity)
        if source_entity_id is not None:
            conditions.append(RagDocument.source_entity_id == source_entity_id)
        return db.scalars(
            select(RagDocument)
            .where(*conditions, RagDocument.content_text.ilike(pattern))
            .order_by(RagDocument.created_at.desc())
            .limit(limit)
        ).all()


@dataclass(slots=True)
class DecisionCandidate:
    label: str
    source: str
    confidence: float
    reason: str
    customer_id: int | None = None
    product_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class DecisionEngineService:
    def __init__(self, *, semantic_provider: EmbeddingProvider | None = None) -> None:
        self.rag = RAGRetrievalService()
        self.semantic_provider = semantic_provider
        self._semantic_unavailable_company_ids: set[int] = set()

    def reset_runtime_state(self) -> None:
        self._semantic_unavailable_company_ids.clear()

    def decision_settings(self, db: Session, company_id: int) -> DecisionSettings:
        return get_or_create_settings(db, DecisionSettings, company_id)

    def customer_decision(
        self,
        db: Session,
        company_id: int,
        *,
        detected_name: str | None = None,
        detected_code: str | None = None,
        sender: str | None = None,
        tax_id: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        settings = self.decision_settings(db, company_id)
        candidates: list[DecisionCandidate] = []
        customer_map: dict[int, DecisionCandidate] = {}

        def add_candidate(candidate: DecisionCandidate) -> None:
            existing = customer_map.get(candidate.customer_id or -1)
            if existing:
                if candidate.confidence > existing.confidence:
                    customer_map[candidate.customer_id or -1] = candidate
                return
            customer_map[candidate.customer_id or -1] = candidate

        if settings.enable_exact_match:
            if detected_code:
                customer = db.scalar(select(Customer).where(Customer.company_id == company_id, Customer.code == detected_code))
                if customer:
                    add_candidate(DecisionCandidate(customer.fiscal_name, "exact_code", 1.0, "Código de cliente exacto", customer_id=customer.id, metadata={"code": customer.code}))
            if tax_id:
                customer = db.scalar(select(Customer).where(Customer.company_id == company_id, Customer.tax_id == tax_id))
                if customer:
                    add_candidate(DecisionCandidate(customer.fiscal_name, "exact_tax_id", 0.99, "CIF/NIF exacto", customer_id=customer.id, metadata={"tax_id": customer.tax_id}))
            sender_email = parseaddr(sender or "")[1].strip().lower()
            if sender_email and "@" in sender_email:
                customer = db.scalar(
                    select(Customer).where(
                        Customer.company_id == company_id,
                        Customer.primary_email.ilike(sender_email),
                    )
                )
                if customer:
                    add_candidate(
                        DecisionCandidate(
                            customer.fiscal_name,
                            "exact_email",
                            0.99,
                            "Email principal exacto",
                            customer_id=customer.id,
                            metadata={"email": customer.primary_email},
                        )
                    )

                contacts = db.scalars(
                    select(CustomerContact).where(
                        CustomerContact.company_id == company_id,
                        CustomerContact.email.ilike(sender_email),
                    )
                ).all()
                contact_customer_ids = {contact.customer_id for contact in contacts}
                if len(contact_customer_ids) == 1:
                    customer = db.get(Customer, next(iter(contact_customer_ids)))
                    if customer:
                        add_candidate(
                            DecisionCandidate(
                                customer.fiscal_name,
                                "exact_contact_email",
                                0.99,
                                "Email de contacto exacto",
                                customer_id=customer.id,
                                metadata={"email": sender_email},
                            )
                        )

                points = db.scalars(
                    select(CustomerContactPoint).where(
                        CustomerContactPoint.company_id == company_id,
                        CustomerContactPoint.type == "email",
                        CustomerContactPoint.active == True,  # noqa: E712
                        CustomerContactPoint.value.ilike(sender_email),
                    )
                ).all()
                point_customer_ids = {point.customer_id for point in points}
                if len(point_customer_ids) == 1:
                    customer = db.get(Customer, next(iter(point_customer_ids)))
                    if customer:
                        add_candidate(
                            DecisionCandidate(
                                customer.fiscal_name,
                                "exact_contact_point_email",
                                0.99,
                                "Email aprendido exacto",
                                customer_id=customer.id,
                                metadata={"email": sender_email},
                            )
                        )

                domain = sender_email.split("@", 1)[1]

                domain_rows = db.scalars(
                    select(CustomerDomain).where(
                        CustomerDomain.company_id == company_id,
                        CustomerDomain.domain.ilike(domain),
                    )
                ).all()
                domain_customer_ids = {row.customer_id for row in domain_rows}
                if len(domain_customer_ids) == 1:
                    customer = db.get(Customer, next(iter(domain_customer_ids)))
                    if customer:
                        add_candidate(
                            DecisionCandidate(
                                customer.fiscal_name,
                                "exact_domain",
                                0.97,
                                "Dominio asociado de forma unívoca",
                                customer_id=customer.id,
                                metadata={"domain": domain},
                            )
                        )

                domain_contacts = db.scalars(
                    select(CustomerContact).where(
                        CustomerContact.company_id == company_id,
                        CustomerContact.email.ilike(f"%@{domain}"),
                    )
                ).all()
                domain_contact_customer_ids = {contact.customer_id for contact in domain_contacts}
                if len(domain_contact_customer_ids) == 1:
                    customer = db.get(Customer, next(iter(domain_contact_customer_ids)))
                    if customer:
                        add_candidate(
                            DecisionCandidate(
                                customer.fiscal_name,
                                "contact_email_domain",
                                0.96,
                                "Dominio de contacto asociado de forma unívoca",
                                customer_id=customer.id,
                                metadata={"domain": domain},
                            )
                        )

        if settings.enable_alias_match and detected_name:
            alias = db.scalar(
                select(CustomerAlias)
                .join(Customer, Customer.id == CustomerAlias.customer_id)
                .where(CustomerAlias.company_id == company_id, CustomerAlias.alias.ilike(detected_name))
            )
            if alias:
                customer = db.get(Customer, alias.customer_id)
                if customer:
                    add_candidate(DecisionCandidate(customer.fiscal_name, "approved_alias", 0.97, f"Alias aprobado: {alias.alias}", customer_id=customer.id, metadata={"alias": alias.alias}))
            learned = db.scalar(
                select(LearnedAlias)
                .where(LearnedAlias.company_id == company_id, LearnedAlias.alias_type == "customer", LearnedAlias.alias.ilike(detected_name), LearnedAlias.approved == True)  # noqa: E712
            )
            if learned and learned.customer_id:
                customer = db.get(Customer, learned.customer_id)
                if customer:
                    add_candidate(DecisionCandidate(customer.fiscal_name, "learned_alias", 0.985, f"Alias aprendido: {learned.alias}", customer_id=customer.id, metadata={"alias": learned.alias, "source": learned.source}))

        if settings.enable_history_match and detected_name:
            customer_rows = db.scalars(select(Customer).where(Customer.company_id == company_id)).all()
            for customer in customer_rows:
                score = SequenceMatcher(None, detected_name.lower(), customer.fiscal_name.lower()).ratio()
                if score >= 0.72:
                    add_candidate(DecisionCandidate(customer.fiscal_name, "fuzzy_name", round(score, 2), "Coincidencia aproximada con histórico", customer_id=customer.id))
            if sender and "@" in sender:
                domain = sender.lower().split("@", 1)[1]
                historical = db.scalar(
                    select(Customer)
                    .join(Order, Order.customer_id == Customer.id)
                    .outerjoin(CustomerDomain, CustomerDomain.customer_id == Customer.id)
                    .where(
                        Customer.company_id == company_id,
                        or_(
                            Customer.primary_email.ilike(f"%@{domain}"),
                            CustomerDomain.domain.ilike(domain),
                        ),
                    )
                    .group_by(Customer.id)
                    .order_by(func.count(Order.id).desc())
                )
                if historical:
                    add_candidate(DecisionCandidate(historical.fiscal_name, "historical_sender", 0.94, "Remitente habitual en histórico", customer_id=historical.id, metadata={"domain": domain}))

        rag_hits = self.rag.retrieve_cases(db, company_id, text or detected_name or sender or "", limit=3) if settings.enable_rag_match else []
        if rag_hits:
            top = rag_hits[0]
            if top.customer_id:
                customer = db.get(Customer, top.customer_id)
                if customer:
                    add_candidate(DecisionCandidate(customer.fiscal_name, "rag_case", 0.82, f"Caso similar: {top.summary[:140]}", customer_id=customer.id, metadata={"case_id": top.id, "similarity": top.similarity_score}))

        ordered = sorted(customer_map.values(), key=lambda item: (item.confidence, item.source == "learned_alias", item.source == "approved_alias"), reverse=True)
        selected = ordered[0] if ordered else None
        alternatives = ordered[1:4]
        requires_review = True
        if selected:
            second = alternatives[0].confidence if alternatives else 0
            requires_review = selected.confidence < 0.9 or (selected.confidence - second) < 0.08
        return {
            "selected": selected,
            "alternatives": alternatives,
            "requires_review": requires_review,
            "evidence": [
                {
                    "label": item.label,
                    "source": item.source,
                    "confidence": item.confidence,
                    "reason": item.reason,
                    "metadata": item.metadata,
                }
                for item in ordered
            ],
            "llm_supported": settings.enable_llm_support and bool(getattr(get_or_create_settings(db, LLMSettings, company_id), "api_key_encrypted", None)),
        }

    def product_decision(
        self,
        db: Session,
        company_id: int,
        *,
        customer_id: int | None = None,
        reference: str | None = None,
        detected_name: str | None = None,
        ean: str | None = None,
        sku: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        settings = self.decision_settings(db, company_id)
        candidates: dict[int, DecisionCandidate] = {}
        product_catalog = db.scalars(select(Product).where(Product.company_id == company_id)).all()
        product_alias_map: dict[int, list[str]] = {}
        reference_conflict = False
        for product in product_catalog:
            aliases = db.scalars(select(ProductAlias.alias).where(ProductAlias.company_id == company_id, ProductAlias.product_id == product.id)).all()
            product_alias_map[product.id] = [alias.lower() for alias in aliases if alias]

        def add_candidate(candidate: DecisionCandidate) -> None:
            key = candidate.product_id or -1
            existing = candidates.get(key)
            if existing and existing.confidence >= candidate.confidence:
                return
            candidates[key] = candidate

        def add_text_matches(docs: list[RagDocument], confidence: float, source: str) -> None:
            for doc in docs:
                doc_text = f"{doc.title}\n{doc.content_text}".lower()
                for product in product_catalog:
                    terms = {
                        product.reference.lower(),
                        (product.alternative_code or "").lower(),
                        product.name.lower(),
                        (product.brand or "").lower(),
                    }
                    terms.update(product_alias_map.get(product.id, []))
                    if any(term and term in doc_text for term in terms):
                        add_candidate(
                            DecisionCandidate(
                                product.name,
                                source,
                                confidence,
                                f"Documento de conocimiento: {doc.title[:120]}",
                                product_id=product.id,
                                metadata={"document_id": doc.id, "document_title": doc.title, "customer_scope": doc.source_entity_id if doc.source_entity == "customer" else None},
                            )
                        )
                        break

        if settings.enable_exact_match:
            if reference:
                product = db.scalar(select(Product).where(Product.company_id == company_id, Product.reference == reference))
                if product:
                    if _product_name_is_compatible(detected_name, product):
                        add_candidate(DecisionCandidate(product.name, "exact_reference", 1.0, "Referencia exacta", product_id=product.id, metadata={"reference": product.reference}))
                    else:
                        reference_conflict = True
            for field_name, value, confidence, reason in [
                ("ean", ean, 0.99, "EAN exacto"),
                ("sku", sku, 0.98, "SKU exacto"),
            ]:
                if value:
                    product = db.scalar(
                        select(Product).where(
                            Product.company_id == company_id,
                            Product.ean == value if field_name == "ean" else Product.alternative_code == value,
                        )
                    )
                    if product:
                        add_candidate(DecisionCandidate(product.name, f"exact_{field_name}", confidence, reason, product_id=product.id, metadata={field_name: value}))

        if settings.enable_alias_match and detected_name:
            alias = db.scalar(
                select(ProductAlias)
                .join(Product, Product.id == ProductAlias.product_id)
                .where(ProductAlias.company_id == company_id, ProductAlias.alias.ilike(detected_name))
            )
            if alias:
                product = db.get(Product, alias.product_id)
                if product:
                    add_candidate(DecisionCandidate(product.name, "approved_alias", 0.97, f"Alias aprobado: {alias.alias}", product_id=product.id, metadata={"alias": alias.alias}))
            learned = db.scalar(
                select(LearnedAlias)
                .where(LearnedAlias.company_id == company_id, LearnedAlias.alias_type == "product", LearnedAlias.alias.ilike(detected_name), LearnedAlias.approved == True)  # noqa: E712
            )
            if learned and learned.product_id:
                product = db.get(Product, learned.product_id)
                if product:
                    add_candidate(DecisionCandidate(product.name, "learned_alias", 0.985, f"Alias aprendido: {learned.alias}", product_id=product.id, metadata={"alias": learned.alias, "source": learned.source}))

        if settings.enable_history_match and customer_id:
            historical = db.execute(
                select(Product, func.count(OrderLine.id).label("usage_count"))
                .join(OrderLine, OrderLine.product_id == Product.id)
                .join(Order, Order.id == OrderLine.order_id)
                .where(Product.company_id == company_id, Order.customer_id == customer_id)
                .group_by(Product.id)
                .order_by(func.count(OrderLine.id).desc(), Product.reference.asc())
                .limit(10)
            ).all()
            for product, usage_count in historical:
                if usage_count >= settings.min_product_frequency:
                    add_candidate(DecisionCandidate(product.name, "customer_history", min(0.95, 0.72 + min(usage_count, 10) * 0.02), f"Producto habitual del cliente ({usage_count} usos)", product_id=product.id, metadata={"usage_count": usage_count}))

        if detected_name and settings.enable_history_match:
            for product in product_catalog:
                score = _match_text_similarity(detected_name, product.name)
                if score >= 0.7:
                    add_candidate(DecisionCandidate(product.name, "fuzzy_name", round(score, 2), "Coincidencia aproximada con catálogo", product_id=product.id))

        if settings.enable_rag_match:
            customer_docs = self.rag.retrieve_documents(
                db,
                company_id,
                text or detected_name or reference or "",
                limit=3,
                source_entity="customer",
                source_entity_id=customer_id,
            ) if customer_id else []
            global_docs = self.rag.retrieve_documents(db, company_id, text or detected_name or reference or "", limit=3)
            if customer_docs:
                add_text_matches(customer_docs, 0.92, "customer_knowledge")
            if global_docs:
                add_text_matches(global_docs, 0.83, "rag_document")
            if not candidates and global_docs:
                doc = global_docs[0]
                add_candidate(
                    DecisionCandidate(
                        detected_name or reference or doc.title,
                        "rag_document",
                        0.8,
                        f"Documento similar: {doc.title[:140]}",
                        metadata={"document_id": doc.id, "document_title": doc.title},
                    )
                )

        semantic_candidates = self._semantic_product_candidates(
            db,
            company_id,
            query=text or detected_name or reference or "",
        )
        knowledge_evidence = self._product_knowledge_evidence(
            db,
            company_id,
            query=text or detected_name or reference or "",
            customer_id=customer_id,
            product_ids=[
                item.product_id
                for item in [*candidates.values(), *semantic_candidates]
                if item.product_id is not None
            ],
        )
        ordered = sorted(candidates.values(), key=lambda item: (item.confidence, item.source == "learned_alias", item.source == "approved_alias"), reverse=True)
        selected = ordered[0] if ordered else None
        alternatives = ordered[1:4]
        requires_review = True
        if selected:
            second = alternatives[0].confidence if alternatives else 0
            requires_review = selected.confidence < 0.9 or (selected.confidence - second) < 0.08
            if reference_conflict and selected.source != "exact_reference":
                requires_review = True
        evidence = [
            {
                "label": item.label,
                "source": item.source,
                "confidence": item.confidence,
                "reason": item.reason,
                "metadata": item.metadata,
            }
            for item in ordered
        ]
        evidence.extend(
            {
                "label": item.label,
                "source": item.source,
                "confidence": item.confidence,
                "reason": item.reason,
                "metadata": item.metadata,
            }
            for item in semantic_candidates
        )
        evidence.extend(
            {
                "label": item.label,
                "source": item.source,
                "confidence": item.confidence,
                "reason": item.reason,
                "metadata": item.metadata,
            }
            for item in knowledge_evidence
        )
        return {
            "selected": selected,
            "alternatives": alternatives,
            "semantic_candidates": semantic_candidates,
            "knowledge_evidence": knowledge_evidence,
            "requires_review": requires_review,
            "evidence": evidence,
            "llm_supported": settings.enable_llm_support and bool(getattr(get_or_create_settings(db, LLMSettings, company_id), "api_key_encrypted", None)),
        }

    def _semantic_product_candidates(self, db: Session, company_id: int, *, query: str) -> list[DecisionCandidate]:
        if not query.strip() or company_id in self._semantic_unavailable_company_ids:
            return []
        try:
            candidates = find_product_candidates(db, company_id=company_id, query=query, provider=self.semantic_provider, limit=5)
        except Exception as exc:  # noqa: BLE001
            self._semantic_unavailable_company_ids.add(company_id)
            logger.info("semantic_product_candidates_unavailable", extra={"company_id": company_id, "error_type": type(exc).__name__})
            return []
        return [
            DecisionCandidate(
                candidate.name,
                "semantic_candidate",
                round(candidate.similarity, 4),
                "Candidato semántico por descripción del pedido. Requiere validación determinista o humana.",
                product_id=candidate.product_id,
                metadata={
                    "reference": candidate.reference,
                    "similarity": round(candidate.similarity, 4),
                    "embedding_model": candidate.embedding_model,
                    "embedding_version": candidate.embedding_version,
                },
            )
            for candidate in candidates
        ]

    def _product_knowledge_evidence(self, db: Session, company_id: int, *, query: str, customer_id: int | None, product_ids: list[int]) -> list[DecisionCandidate]:
        if not query.strip():
            return []
        try:
            evidences = retrieve_product_knowledge(
                db,
                company_id=company_id,
                raw_description=query,
                customer_id=customer_id,
                candidate_product_ids=product_ids,
                provider=self.semantic_provider,
                limit=5,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("product_knowledge_unavailable", extra={"company_id": company_id, "error_type": type(exc).__name__})
            return []
        return [
            DecisionCandidate(
                evidence.content[:160],
                "business_knowledge",
                evidence.relevance,
                f"Evidencia {evidence.source_type}: {evidence.content[:180]}",
                product_id=evidence.product_id,
                metadata={
                    "knowledge_entry_id": evidence.id,
                    "source_type": evidence.source_type,
                    "source_id": evidence.source_id,
                    "scope": evidence.scope,
                    "customer_id": evidence.customer_id,
                    "product_id": evidence.product_id,
                    "relevance": evidence.relevance,
                },
            )
            for evidence in evidences
        ]


class ScoringService:
    def score_order(self, db: Session, order: Order) -> ScoringResult:
        settings = get_or_create_settings(db, ScoringSettings, order.company_id)
        breakdown = calculate_order_score(order, settings)
        result = ScoringResult(
            company_id=order.company_id,
            order_id=order.id,
            total_score=breakdown.total,
            customer_score=breakdown.customer,
            product_score=breakdown.products,
            confidence_score=breakdown.confidence,
            rule_score=breakdown.coherence,
            block_reason=None if breakdown.total >= settings.doubtful_threshold else "Bajo umbral configurado",
            details_json=json.dumps(
                {
                    "weights": {
                        "customer": settings.customer_weight,
                        "products": settings.products_weight,
                        "quantities": settings.quantities_weight,
                        "coherence": settings.coherence_weight,
                        "llm": settings.llm_weight,
                    },
                    "line_count": breakdown.line_count,
                    "validated_products": breakdown.validated_products,
                    "valid_quantities": breakdown.valid_quantities,
                },
                ensure_ascii=False,
            ),
        )
        db.add(result)
        db.flush()
        log_action(db, company_id=order.company_id, user=None, action="agent.scoring_recorded", entity_type="scoring_result", entity_id=result.id, message=f"Scoring guardado para pedido {order.id}: {breakdown.total}")
        return result

    def status_for_score(self, db: Session, company_id: int, score: float) -> str:
        return ORDER_STATE.status_for_score(db, company_id, score)


class ReviewService:
    def open_review(self, db: Session, order: Order, user=None, comments: str | None = None) -> OrderReview:
        review = OrderReview(
            company_id=order.company_id,
            order_id=order.id,
            reviewer_user_id=getattr(user, "id", None),
            status="pending",
            comments=comments,
        )
        db.add(review)
        db.flush()
        return review


class LearningService:
    def _knowledge_key(self, db: Session, company_id: int, customer_id: int, product_id: int) -> CustomerProductKnowledge | None:
        return db.scalar(
            select(CustomerProductKnowledge).where(
                CustomerProductKnowledge.company_id == company_id,
                CustomerProductKnowledge.customer_id == customer_id,
                CustomerProductKnowledge.product_id == product_id,
            )
        )

    def update_customer_product_knowledge(
        self,
        db: Session,
        *,
        company_id: int,
        customer: Customer | None,
        product: Product | None,
        quantity: float | None,
        unit: str | None,
        order: Order | None = None,
        order_at: datetime | None = None,
        source_context: str = "pedido_confirmado",
        customer_alias_used: str | None = None,
        comments: str | None = None,
        is_manual: bool = False,
        exported_at: datetime | None = None,
        delivery_note_at: datetime | None = None,
        force_habitual: bool = False,
    ) -> CustomerProductKnowledge | None:
        if not customer or not product:
            return None
        row = self._knowledge_key(db, company_id, customer.id, product.id)
        if not row:
            row = CustomerProductKnowledge(
                company_id=company_id,
                customer_id=customer.id,
                product_id=product.id,
                product_reference=product.reference,
                product_name=product.name,
                customer_alias_used=customer_alias_used,
                source_context=source_context,
                times_ordered=0,
                confirmed_count=0,
                manual_count=0,
                total_quantity=0,
                average_quantity=0,
                confidence=0.35 if is_manual else 0.5,
                status="pending",
            )
            db.add(row)
        if product.reference:
            row.product_reference = product.reference
        if product.name:
            row.product_name = product.name
        if customer_alias_used:
            row.customer_alias_used = customer_alias_used
        if source_context:
            row.source_context = source_context
        if order and order.id:
            row.last_order_id = order.id
        now = datetime.now(timezone.utc)
        counts_as_order = source_context in {"pedido_confirmado", "pedido_confirmado_auto", "pedido_exportado", "pedido_exportado_ftp", "correccion_linea"}
        if counts_as_order:
            row.times_ordered = (row.times_ordered or 0) + 1
            row.confirmed_count = (row.confirmed_count or 0) + 1
            row.last_order_at = order_at or (order.confirmed_at or order.exported_at or order.created_at if order else now)
            if exported_at:
                row.last_exported_at = exported_at
            if delivery_note_at:
                row.last_delivery_note_at = delivery_note_at
        if is_manual:
            row.manual_count = (row.manual_count or 0) + 1
        if quantity is not None and counts_as_order:
            row.last_quantity = quantity
            row.total_quantity = (row.total_quantity or 0) + quantity
            row.min_quantity = quantity if row.min_quantity is None else min(row.min_quantity, quantity)
            row.max_quantity = quantity if row.max_quantity is None else max(row.max_quantity, quantity)
            seen_count = row.confirmed_count + row.manual_count
            row.average_quantity = round((row.total_quantity or 0) / max(seen_count, 1), 3)
        if unit:
            row.usual_unit = unit
        if comments:
            comments = comments.strip()
            if comments:
                existing = row.comments_summary or ""
                row.comments_summary = comments if not existing else existing if comments in existing else f"{existing} | {comments}"[:1000]
        row.confidence = min(1.0, max(row.confidence or 0, 0.45 + min(row.times_ordered, 10) * 0.05 + min(row.manual_count, 5) * 0.05))
        habitual_threshold = 3 if not is_manual else 2
        row.is_habitual = force_habitual or (row.times_ordered >= habitual_threshold) or (row.times_ordered >= 2 and row.manual_count >= 1)
        row.status = "habitual" if row.is_habitual else "pending"
        row.updated_at = now
        db.flush()
        return row

    def penalize_customer_product_knowledge(
        self,
        db: Session,
        *,
        company_id: int,
        customer_id: int | None,
        product_id: int | None,
        reason: str,
        severity: float = 0.12,
        order_id: int | None = None,
    ) -> CustomerProductKnowledge | None:
        if not customer_id or not product_id:
            return None
        row = self._knowledge_key(db, company_id, customer_id, product_id)
        if not row:
            return None
        row.confidence = max(0.05, (row.confidence or 0) - severity)
        row.status = "conflict" if row.confidence < 0.45 else row.status
        row.updated_at = datetime.now(timezone.utc)
        self.record_knowledge_conflict(
            db,
            company_id=company_id,
            customer_id=customer_id,
            product_id=product_id,
            title="Conflicto de conocimiento detectado",
            message=reason,
            payload={"reason": reason, "severity": severity},
            order_id=order_id,
        )
        db.flush()
        return row

    def record_alias(
        self,
        db: Session,
        *,
        company_id: int,
        alias_type: str,
        alias: str,
        canonical_value: str,
        customer_id: int | None = None,
        product_id: int | None = None,
        source: str | None = None,
        confidence: float = 0.8,
    ) -> LearnedAlias:
        learned = LearnedAlias(
            company_id=company_id,
            alias_type=alias_type,
            alias=alias,
            canonical_value=canonical_value,
            customer_id=customer_id,
            product_id=product_id,
            source=source,
            confidence=confidence,
        )
        db.add(learned)
        db.flush()
        return learned

    def record_case(
        self,
        db: Session,
        *,
        company_id: int,
        summary: str,
        resolved_action: str,
        resolution_json: str | None = None,
        customer_id: int | None = None,
        order_id: int | None = None,
        inbound_message_id: int | None = None,
    ) -> RagCase:
        case = RagCase(
            company_id=company_id,
            customer_id=customer_id,
            order_id=order_id,
            inbound_message_id=inbound_message_id,
            summary=summary,
            resolved_action=resolved_action,
            resolution_json=resolution_json,
        )
        db.add(case)
        db.flush()
        return case

    def record_knowledge_conflict(
        self,
        db: Session,
        *,
        company_id: int,
        customer_id: int | None,
        product_id: int | None,
        title: str,
        message: str,
        payload: dict[str, Any] | None = None,
        order_id: int | None = None,
    ) -> Alert:
        alert = Alert(
            company_id=company_id,
            inbound_message_id=None,
            order_id=order_id,
            alert_type="knowledge_conflict",
            severity="medium",
            status="open",
            title=title,
            message=message,
            payload_json=json.dumps({"customer_id": customer_id, "product_id": product_id, **(payload or {})}, ensure_ascii=False),
        )
        db.add(alert)
        db.flush()
        return alert


class ExportService:
    def queue_export(self, db: Session, *, company_id: int, order_id: int, export_format: str = "csv", destination_type: str = "sftp", payload_json: str | None = None) -> ExportJob:
        job = ExportJob(
            company_id=company_id,
            order_id=order_id,
            export_format=export_format,
            destination_type=destination_type,
            payload_json=payload_json,
            status="pending",
        )
        db.add(job)
        db.flush()
        return job


class AlertService:
    def raise_alert(self, db: Session, *, company_id: int, alert_type: str, title: str, message: str, severity: str = "medium", order_id: int | None = None, inbound_message_id: int | None = None, payload_json: str | None = None) -> Alert:
        alert = Alert(
            company_id=company_id,
            alert_type=alert_type,
            title=title,
            message=message,
            severity=severity,
            order_id=order_id,
            inbound_message_id=inbound_message_id,
            payload_json=payload_json,
        )
        db.add(alert)
        db.flush()
        log_action(db, company_id=company_id, user=None, action="agent.alert_created", entity_type="alert", entity_id=alert.id, message=title)
        return alert


def _json_from_content(content: str) -> dict[str, Any]:
    import json
    import re

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


def _active_prompt(db: Session, company_id: int, purpose: str, fallback: str) -> str:
    template = db.scalar(select(PromptTemplate).where(PromptTemplate.company_id == company_id, PromptTemplate.purpose == purpose))
    if not template or not template.active_version_id:
        return fallback
    version = db.get(PromptVersion, template.active_version_id)
    return version.content if version else fallback


def _confidence(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number <= 1 else number / 100


def _parse_positive_quantity(value) -> tuple[float | None, str | None]:  # noqa: ANN001
    if value is None or str(value).strip() == "":
        return None, "Cantidad no detectada"
    try:
        quantity = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None, "Cantidad ambigua"
    if not isfinite(quantity) or quantity <= 0:
        return None, "La cantidad debe ser mayor que cero"
    return quantity, None


class UnifiedOrderPipelineService:
    def __init__(self) -> None:
        self.matching = CustomerMatchingService()
        self.product_matching = ProductMatchingService()
        self.decision = DecisionEngineService()
        self.scoring = ScoringService()
        self.review = ReviewService()
        self.learning = LearningService()
        self.exporter = ExportService()
        self.alerts = AlertService()

    def process_inbound_message(
        self,
        db: Session,
        inbound_message: InboundMessage,
        user=None,
        force_order: bool = False,
        email: Email | None = None,
        source_text_override: str | None = None,
        email_fast_path: bool = False,
    ) -> dict[str, Any]:
        self.decision.reset_runtime_state()

        if not force_order and inbound_message.order_id:
            order = db.scalar(
                select(Order).where(
                    Order.id == inbound_message.order_id,
                    Order.company_id == inbound_message.company_id,
                )
            )
            if order:
                return {
                    "ok": True,
                    "status": "order_detected",
                    "message": f"Pedido {order.id} ya habia sido creado.",
                    "order_id": order.id,
                    "score": order.score,
                }

        llm_settings = get_or_create_settings(db, LLMSettings, inbound_message.company_id)
        email_settings = get_or_create_settings(db, EmailSettings, inbound_message.company_id)
        if not llm_settings.agent_enabled or llm_settings.provider == "disabled" or llm_settings.agent_mode == "desactivado":
            return self._mark_error(db, inbound_message, user, "El agente no esta configurado o esta pausado.")
        if not llm_settings.api_key_encrypted:
            return self._mark_error(db, inbound_message, user, "API key OpenAI no configurada.")

        touch_message(db, inbound_message, status="processing", step="processing")
        inbound_message.processing_error = None
        db.commit()

        email = email or db.scalar(select(Email).where(Email.company_id == inbound_message.company_id, Email.external_id == inbound_message.source_external_id))
        try:
            source_text = source_text_override or self._input_text(inbound_message, email)
            if len(source_text.strip()) < 12:
                inbound_message.status = "doubtful"
                inbound_message.processing_step = "insufficient_text"
                inbound_message.processing_error = "No hay texto suficiente para procesar."
                db.commit()
                self.alerts.raise_alert(
                    db,
                    company_id=inbound_message.company_id,
                    alert_type="insufficient_text",
                    title="Entrada con texto insuficiente",
                    message=inbound_message.processing_error,
                    severity="low",
                    inbound_message_id=inbound_message.id,
                )
                return {"ok": False, "message": inbound_message.processing_error}

            normalized = NormalizationService().normalize(source_text, preserve_lines=True)
            inbound_message.normalized_text = normalized
            db.add(
                NormalizedInput(
                    company_id=inbound_message.company_id,
                    inbound_message_id=inbound_message.id,
                    normalized_text=normalized,
                    metadata_json=None,
                )
            )
            db.flush()

            if email_fast_path:
                if not llm_settings.can_extract_order and not force_order:
                    inbound_message.status = "order_detected"
                    inbound_message.processing_step = "order_detected_no_extraction"
                    inbound_message.last_processed_at = datetime.now(timezone.utc)
                    db.commit()
                    return {"ok": True, "status": "order_detected", "message": "Pedido detectado. Extraccion desactivada por configuracion."}
                structured, fast_state, fast_message = self._extract_structured_fast_path(llm_settings, normalized, inbound_message)
                if fast_state == "no_order":
                    inbound_message.classification_json = json.dumps(
                        {
                            "tipo_correo": "no_pedido",
                            "confianza": 0.99,
                            "motivo": fast_message or "Extraccion estructurada no detecto pedido.",
                        },
                        ensure_ascii=False,
                    )
                    inbound_message.detected_type = "no_pedido"
                    inbound_message.status = "no_order"
                    inbound_message.processing_step = "classified_non_order"
                    inbound_message.last_processed_at = datetime.now(timezone.utc)
                    db.commit()
                    return {"ok": True, "message": fast_message or "Entrada clasificada como no pedido.", "status": "no_order"}
                if fast_state == "doubtful":
                    inbound_message.classification_json = json.dumps(
                        {
                            "tipo_correo": "pedido",
                            "confianza": 0.62,
                            "motivo": fast_message or "Pedido detectado pero no se pudieron extraer lineas validas.",
                        },
                        ensure_ascii=False,
                    )
                    inbound_message.detected_type = "pedido"
                    inbound_message.status = "doubtful"
                    inbound_message.processing_step = "structured_doubtful"
                    inbound_message.processing_error = fast_message or "Pedido detectado pero no se pudieron extraer lineas validas."
                    inbound_message.last_processed_at = datetime.now(timezone.utc)
                    db.commit()
                    self.alerts.raise_alert(
                        db,
                        company_id=inbound_message.company_id,
                        alert_type="doubtful_classification",
                        title="Entrada dudosa",
                        message=inbound_message.processing_error,
                        severity="medium",
                        inbound_message_id=inbound_message.id,
                    )
                    return {"ok": False, "message": inbound_message.processing_error, "status": "doubtful"}
                if fast_state == "processing_error":
                    inbound_message.classification_json = json.dumps(
                        {
                            "tipo_correo": "pedido",
                            "confianza": 0.0,
                            "motivo": fast_message or "Error de extraccion estructurada.",
                        },
                        ensure_ascii=False,
                    )
                    inbound_message.detected_type = "pedido"
                    inbound_message.status = "error"
                    inbound_message.processing_step = "structured_extraction_error"
                    inbound_message.processing_error = fast_message or "Error de extraccion estructurada."
                    inbound_message.last_processed_at = datetime.now(timezone.utc)
                    db.commit()
                    return {"ok": False, "message": inbound_message.processing_error, "status": "error"}
                extraction = self._legacy_payload_from_structured(structured)
                extraction_source = (
                    (extraction.get("_extraction_meta") or {}).get("source")
                    if isinstance(extraction.get("_extraction_meta"), dict)
                    else None
                ) or "structured_order_extraction"
                tipo = "pedido"
                confidence = 0.99
                classification = {
                    "tipo_correo": tipo,
                    "confianza": confidence,
                    "motivo": "Extraccion estructurada de correo procesada sin clasificacion adicional.",
                    "_extraction_source": extraction_source,
                }
            else:
                classification = self._classify(db, llm_settings, inbound_message.company_id, normalized)
                tipo = str(classification.get("tipo_correo") or classification.get("type") or classification.get("tipo") or "").lower()
                confidence = _confidence(classification.get("confianza") or classification.get("confidence"), 0.0)
                if not force_order and (tipo in {"no_pedido", "consulta", "incidencia"} or ("pedido" not in tipo and confidence < 0.75)):
                    inbound_message.status = "no_order"
                    inbound_message.processing_step = "classified_non_order"
                    inbound_message.last_processed_at = datetime.now(timezone.utc)
                    db.commit()
                    return {"ok": True, "message": "Entrada clasificada como no pedido.", "status": "no_order"}

                if not force_order and (tipo == "dudoso" or confidence < 0.45):
                    inbound_message.status = "doubtful"
                    inbound_message.processing_step = "classified_doubtful"
                    inbound_message.processing_error = classification.get("motivo") or "Clasificacion dudosa."
                    inbound_message.last_processed_at = datetime.now(timezone.utc)
                    db.commit()
                    self.alerts.raise_alert(
                        db,
                        company_id=inbound_message.company_id,
                        alert_type="doubtful_classification",
                        title="Entrada dudosa",
                        message=inbound_message.processing_error,
                        severity="medium",
                        inbound_message_id=inbound_message.id,
                    )
                    return {"ok": False, "message": inbound_message.processing_error}

                if not llm_settings.can_extract_order and not force_order:
                    inbound_message.status = "order_detected"
                    inbound_message.processing_step = "order_detected_no_extraction"
                    inbound_message.last_processed_at = datetime.now(timezone.utc)
                    db.commit()
                    return {"ok": True, "status": "order_detected", "message": "Pedido detectado. Extraccion desactivada por configuracion."}

                extraction = self._extract(db, llm_settings, inbound_message.company_id, normalized, inbound_message)
                extraction_source = (extraction.get("_extraction_meta") or {}).get("source") if isinstance(extraction.get("_extraction_meta"), dict) else None
            extraction_meta = extraction.get("_extraction_meta") if isinstance(extraction.get("_extraction_meta"), dict) else {}
            inbound_message.classification_json = json.dumps(classification, ensure_ascii=False)
            inbound_message.detected_type = tipo or None
            log_action(db, company_id=inbound_message.company_id, user=user, action="agent.classification_completed", entity_type="inbound_message", entity_id=inbound_message.id, message=f"Clasificacion: {tipo or 'sin tipo'} ({confidence:.2f})")
            log_action(
                db,
                company_id=inbound_message.company_id,
                user=user,
                action="agent.extraction_completed",
                entity_type="inbound_message",
                entity_id=inbound_message.id,
                message=f"Extraccion: {extraction_meta.get('source') or extraction_source or 'legacy_extraction'}",
            )
            existing_order = None
            if force_order and inbound_message.order_id:
                existing_order = db.scalar(
                    select(Order).where(
                        Order.id == inbound_message.order_id,
                        Order.company_id == inbound_message.company_id,
                    )
                )
                if existing_order and (existing_order.confirmed_at or existing_order.exported_at):
                    return {
                        "ok": False,
                        "status": "reprocess_blocked",
                        "message": "No se puede reprocesar un pedido confirmado o exportado.",
                        "order_id": existing_order.id,
                    }

            if email_fast_path:
                order = self._create_order(
                    db,
                    inbound_message,
                    email,
                    extraction,
                    normalized,
                    existing_order=existing_order,
                    fast_path=True,
                )
            else:
                order = self._create_order(
                    db,
                    inbound_message,
                    email,
                    extraction,
                    normalized,
                    existing_order=existing_order,
                )
            score_result = self.scoring.score_order(db, order)
            order.score = score_result.total_score
            order.status = self.scoring.status_for_score(db, inbound_message.company_id, order.score)

            scoring_settings = get_or_create_settings(
                db,
                ScoringSettings,
                inbound_message.company_id,
            )
            decision_settings = get_or_create_settings(
                db,
                DecisionSettings,
                inbound_message.company_id,
            )

            if existing_order:
                pending_reviews = db.scalars(
                    select(OrderReview).where(
                        OrderReview.company_id == order.company_id,
                        OrderReview.order_id == order.id,
                        OrderReview.status == "pending",
                    )
                ).all()
                for previous_review in pending_reviews:
                    previous_review.status = "superseded"
                    previous_review.reviewed_at = datetime.now(timezone.utc)

            from app.orders.service import confirm_order_with_effects

            is_email_input = bool(email is not None or inbound_channel_key(inbound_message) == "email")
            auto_confirm_allowed = (
                llm_settings.allow_auto_confirm
                and not decision_settings.always_human_review
                and (not is_email_input or not email_settings.always_human_review)
                and order.score >= scoring_settings.safe_threshold
            )

            confirmation = None
            if auto_confirm_allowed:
                confirmation = confirm_order_with_effects(
                    db,
                    order=order,
                    company_id=inbound_message.company_id,
                    scoring=scoring_settings,
                    user=user,
                    source_context="pedido_confirmado_auto",
                    when=datetime.now(timezone.utc),
                )

            review = None
            if not confirmation or not confirmation["confirmed"]:
                review = self.review.open_review(
                    db,
                    order,
                    user=user,
                    comments=inbound_message.processing_error,
                )

            inbound_message.status = "order_detected"
            inbound_message.processing_step = "completed"
            inbound_message.order_id = order.id
            inbound_message.customer_id = order.customer_id
            inbound_message.score = order.score
            inbound_message.extraction_json = json.dumps(extraction, ensure_ascii=False)
            inbound_message.last_processed_at = datetime.now(timezone.utc)
            db.commit()

            if existing_order:
                log_action(
                    db,
                    company_id=inbound_message.company_id,
                    user=user,
                    action="agent.order_reprocessed",
                    entity_type="order",
                    entity_id=order.id,
                    message=f"Pedido reprocesado desde entrada {inbound_message.id}",
                )
            else:
                log_action(
                    db,
                    company_id=inbound_message.company_id,
                    user=user,
                    action="agent.order_created",
                    entity_type="order",
                    entity_id=order.id,
                    message=f"Pedido creado desde entrada {inbound_message.id}",
                )

            if confirmation and confirmation["confirmed"]:
                log_action(
                    db,
                    company_id=inbound_message.company_id,
                    user=user,
                    action="agent.order_auto_confirmed",
                    entity_type="order",
                    entity_id=order.id,
                    message=f"Pedido auto-confirmado con scoring {order.score}.",
                )
                if confirmation["email_learning_result"] == "conflict":
                    log_action(
                        db,
                        company_id=inbound_message.company_id,
                        user=user,
                        action="customer.email_learning.conflict",
                        entity_type="order",
                        entity_id=order.id,
                        message="El email remitente ya estaba asociado a otro cliente y no se ha sobrescrito.",
                    )

            if (
                llm_settings.allow_auto_export
                and order.status in {"pedido_confirmado", "pedido_validado"}
            ):
                self.exporter.queue_export(
                    db,
                    company_id=inbound_message.company_id,
                    order_id=order.id,
                    payload_json=None,
                )
                db.commit()

            message = f"Pedido {order.id} reprocesado." if existing_order else f"Pedido {order.id} creado."
            return {
                "ok": True,
                "status": "order_detected",
                "message": message,
                "order_id": order.id,
                "review_id": review.id if review else None,
                "score": order.score,
                "auto_confirmed": bool(confirmation and confirmation["confirmed"]),
            }
        except Exception as exc:
            db.rollback()
            return self._mark_error(db, inbound_message, user, str(exc))

    def _mark_error(self, db: Session, inbound_message: InboundMessage, user, message: str) -> dict[str, Any]:
        touch_message(db, inbound_message, status="error", step="processing_error")
        inbound_message.processing_error = message
        db.commit()
        log_action(db, company_id=inbound_message.company_id, user=user, action="agent.processing_error", entity_type="inbound_message", entity_id=inbound_message.id, message=message[:500])
        return {"ok": False, "message": message}

    def _input_text(self, inbound_message: InboundMessage, email: Email | None = None) -> str:
        source_parts: list[str] = []
        seen_parts: set[str] = set()

        def add_part(value: str | None, *, label: str | None = None) -> None:
            normalized = str(value or "").strip()
            if not normalized:
                return
            fingerprint = normalized
            if fingerprint in seen_parts:
                return
            seen_parts.add(fingerprint)
            source_parts.append(f"[{label}]\n{normalized}" if label else normalized)

        add_part(inbound_message.original_content or inbound_message.normalized_text)
        for attachment in inbound_message.attachments or []:
            if attachment.extracted_text:
                add_part(attachment.extracted_text, label=f"Adjunto: {attachment.filename}")
            elif attachment.ocr_text:
                add_part(attachment.ocr_text, label=f"Adjunto: {attachment.filename}")
            elif attachment.transcription_text:
                add_part(attachment.transcription_text, label=f"Adjunto: {attachment.filename}")
        if email:
            add_part(email.body or email.extracted_text)
            for attachment in email.attachments or []:
                if attachment.extracted_text:
                    add_part(attachment.extracted_text, label=f"Adjunto: {attachment.filename}")
        source = "\n\n".join(source_parts)
        subject = inbound_message.subject or (email.subject if email else "")
        sender = inbound_message.sender or (email.sender if email else "")
        return f"Asunto: {subject}\nRemitente: {sender}\n\n{source}".strip()

    def _classify(self, db: Session, settings: LLMSettings, company_id: int, text: str) -> dict[str, Any]:
        prompt = _active_prompt(
            db,
            company_id,
            "classification",
            "Clasifica la entrada como pedido, no_pedido, consulta, incidencia o dudoso. Responde solo JSON con tipo_correo, confianza y motivo.",
        )
        result = classify_sample(db, settings, company_id, text[:12000], prompt)
        if not result.get("ok"):
            raise RuntimeError(result.get("message") or "Error llamando al proveedor IA.")
        return _json_from_content(result.get("content", ""))

    def _extract(self, db: Session, settings: LLMSettings, company_id: int, text: str, inbound_message: InboundMessage | None = None) -> dict[str, Any]:
        structured, structured_error = self._extract_structured(settings, text, inbound_message)
        if structured:
            return self._legacy_payload_from_structured(structured)
        legacy = self._extract_legacy(db, settings, company_id, text)
        legacy.setdefault(
            "_extraction_meta",
            {
                "source": "legacy_extraction",
                "schemaVersion": None,
                "model": resolve_openai_runtime_model(settings.extraction_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK),
                "structuredFallbackReason": structured_error,
            },
        )
        return legacy

    def _extract_structured(self, settings: LLMSettings, text: str, inbound_message: InboundMessage | None = None) -> tuple[OrderExtractionResult | None, str | None]:
        if not settings.api_key_encrypted:
            return None, "missing_api_key"
        try:
            result = extract_order(
                OrderExtractionInput(
                    text=text[:16000],
                    sourceType=self._source_type_for_extraction(inbound_message),
                    sourceId=str(inbound_message.id) if inbound_message and inbound_message.id else None,
                ),
                model=resolve_openai_runtime_model(settings.extraction_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK),
                api_key=decrypt_secret(settings.api_key_encrypted),
                base_url=settings.base_url or "https://api.openai.com/v1",
                timeout_seconds=settings.timeout_seconds,
            )
        except Exception as exc:
            return None, exc.__class__.__name__
        if not result.extracted_data.is_order or not result.extracted_data.lines:
            return None, "structured_no_order_or_without_lines"
        return result, None

    def _extract_structured_fast_path(
        self,
        settings: LLMSettings,
        text: str,
        inbound_message: InboundMessage | None = None,
    ) -> tuple[OrderExtractionResult | None, str, str | None]:
        if not settings.api_key_encrypted:
            return None, "processing_error", "API key OpenAI no configurada."
        try:
            result = extract_order(
                OrderExtractionInput(
                    text=text[:16000],
                    sourceType=self._source_type_for_extraction(inbound_message),
                    sourceId=str(inbound_message.id) if inbound_message and inbound_message.id else None,
                ),
                model=resolve_openai_runtime_model(settings.extraction_model, fallback=LEGACY_OPENAI_MODEL_FALLBACK),
                api_key=decrypt_secret(settings.api_key_encrypted),
                base_url=settings.base_url or "https://api.openai.com/v1",
                timeout_seconds=settings.timeout_seconds,
            )
        except Exception as exc:
            message = str(exc) or "Error llamando al proveedor IA."
            lowered = message.lower()
            if "un pedido debe incluir al menos una linea" in lowered or "pedido debe incluir al menos una linea" in lowered:
                return None, "doubtful", "Pedido detectado pero no se pudieron extraer lineas validas."
            return None, "processing_error", f"Extraccion estructurada fallida: {message[:180]}"
        if not result.extracted_data.is_order:
            return result, "no_order", "La entrada no es un pedido."
        if not result.extracted_data.lines:
            return None, "doubtful", "Pedido detectado pero no se pudieron extraer lineas validas."
        return result, "order", None

    def _source_type_for_extraction(self, inbound_message: InboundMessage | None) -> str:
        if not inbound_message:
            return "unknown"
        if inbound_channel_key(inbound_message) == "whatsapp":
            return "whatsapp"
        raw = " ".join(
            value
            for value in [
                inbound_message.provider,
                inbound_message.content_type,
                inbound_message.source_mailbox,
            ]
            if value
        ).lower()
        if "whatsapp" in raw:
            return "whatsapp"
        if "email" in raw or "imap" in raw or inbound_message.source_mailbox:
            return "email"
        if "pdf" in raw or inbound_message.has_pdf:
            return "pdf"
        if "audio" in raw or inbound_message.has_audio:
            return "voice"
        if "social" in raw:
            return "social"
        return "unknown"

    def _legacy_payload_from_structured(self, result: OrderExtractionResult) -> dict[str, Any]:
        extracted = result.extracted_data
        extraction_payload = extracted.model_dump(by_alias=True)
        notes = list(extracted.notes)
        lines: list[dict[str, Any]] = []
        for line in extracted.lines:
            line_uncertainties = [uncertainty.model_dump() for uncertainty in line.uncertainties]
            if line.notes:
                notes.extend(line.notes)
            confidence = 0.92
            if line.requires_review:
                confidence = 0.62
            if line.quantity is None or line.raw_description is None:
                confidence = min(confidence, 0.5)
            lines.append(
                {
                    "texto_original": line.raw_text,
                    "referencia_detectada": line.reference,
                    "producto_detectado": line.raw_description,
                    "cantidad": line.quantity,
                    "unidad": line.unit,
                    "confianza_extraccion": confidence,
                    "requires_review": line.requires_review,
                    "uncertainties": line_uncertainties,
                    "source_fields": {
                        "rawDescription": line.raw_description_source,
                        "reference": line.reference_source,
                        "quantity": line.quantity_source,
                        "unit": line.unit_source,
                    },
                }
            )
        return {
            "cliente": {
                "nombre_detectado": extracted.customer.raw_name,
                "source_fields": {"rawName": extracted.customer.raw_name_source},
            },
            "pedido": {
                "observaciones": "\n".join(dict.fromkeys(note for note in notes if note)),
                "lineas": lines,
            },
            "requiere_revision_humana": extracted.requires_review,
            "motivos_revision": [uncertainty.reason for uncertainty in extracted.uncertainties],
            "_extraction_meta": {
                "source": "structured_order_extraction",
                "schemaVersion": result.schema_version,
                "model": result.model,
                "timestamp": result.timestamp.isoformat(),
                "payload": extraction_payload,
            },
        }

    def _extract_legacy(self, db: Session, settings: LLMSettings, company_id: int, text: str) -> dict[str, Any]:
        prompt = _active_prompt(
            db,
            company_id,
            "extraction",
            "Extrae un pedido en JSON valido con cliente y pedido.lineas. Cada linea debe incluir texto_original, referencia_detectada, producto_detectado, cantidad, unidad y confianza_extraccion.",
        )
        result = extract_sample(db, settings, company_id, text[:16000], prompt)
        if not result.get("ok"):
            raise RuntimeError(result.get("message") or "Error llamando al proveedor IA.")
        data = _json_from_content(result.get("content", ""))
        lines = self._lines_from_extraction(data)
        if not lines:
            raise ValueError("No se ha podido crear el pedido porque no se detectaron lineas.")
        return data

    def _customer_from_extraction(self, data: dict[str, Any]) -> dict[str, Any]:
        customer = data.get("cliente") or data.get("customer") or {}
        if isinstance(customer, str):
            customer = {"nombre_detectado": customer}
        return customer

    def _order_from_extraction(self, data: dict[str, Any]) -> dict[str, Any]:
        return data.get("pedido") or data.get("order") or data

    def _lines_from_extraction(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        order_data = self._order_from_extraction(data)
        lines = order_data.get("lineas") or order_data.get("lines") or data.get("lineas") or []
        return lines if isinstance(lines, list) else []

    def _create_order(
        self,
        db: Session,
        inbound_message: InboundMessage,
        email: Email | None,
        extracted: dict[str, Any],
        source_text: str,
        *,
        existing_order: Order | None = None,
        fast_path: bool = False,
    ) -> Order:
        customer_data = self._customer_from_extraction(extracted)
        order_data = self._order_from_extraction(extracted)
        sender = inbound_message.sender or (email.sender if email else "") or ""
        detected_name = customer_data.get("nombre_detectado") or customer_data.get("name") or customer_data.get("nombre") or ""
        detected_code = customer_data.get("codigo_cliente_detectado") or customer_data.get("codigo") or customer_data.get("code")
        tax_id = customer_data.get("cif") or customer_data.get("tax_id") or customer_data.get("nif")

        detected_customer_is_tenant = _detected_customer_is_tenant(
            db,
            inbound_message.company_id,
            detected_name=detected_name or None,
            tax_id=tax_id or None,
        )
        matching_detected_name = None if detected_customer_is_tenant else (detected_name or None)
        matching_tax_id = None if detected_customer_is_tenant else (tax_id or None)

        if fast_path:
            customer, method, customer_score = self.matching.match(
                db,
                inbound_message.company_id,
                sender=sender,
                detected_name=matching_detected_name,
                detected_code=detected_code,
            )
            customer_decision = {
                "selected": None,
                "alternatives": [],
                "requires_review": customer is None or customer_score < 0.9,
                "evidence": [],
            }
        else:
            customer_decision = self.decision.customer_decision(
                db,
                inbound_message.company_id,
                detected_name=matching_detected_name,
                detected_code=detected_code or None,
                sender=sender or None,
                tax_id=matching_tax_id,
                text=source_text,
            )
            customer, method, customer_score = self.matching.match(
                db,
                inbound_message.company_id,
                sender=sender,
                detected_name=matching_detected_name,
                detected_code=detected_code,
            )
            if customer_decision["selected"] and customer_decision["selected"].customer_id:
                candidate_customer = db.get(Customer, customer_decision["selected"].customer_id)
                candidate_confidence = customer_decision["selected"].confidence
                if (
                    candidate_customer
                    and not customer_decision["requires_review"]
                    and (not customer or candidate_confidence >= customer_score)
                ):
                    customer = candidate_customer
                    method = customer_decision["selected"].source
                    customer_score = candidate_confidence

            if customer_decision["requires_review"]:
                customer = None
                method = customer_decision["selected"].source if customer_decision["selected"] else method
                customer_score = customer_decision["selected"].confidence if customer_decision["selected"] else customer_score
        if existing_order:
            order = existing_order
            order.lines.clear()
            db.flush()
            order.conversation_id = inbound_message.conversation_id
            order.email_id = email.id if email else order.email_id
            order.customer_id = customer.id if customer else None
            order.validated_customer_id = customer.id if customer else None
            order.customer_detected_name = detected_name or None
            order.customer_identification_method = method
            order.customer_score = round(customer_score * 100, 2)
            order.order_date = order_data.get("fecha_pedido") or order_data.get("order_date")
            order.requested_delivery_date = order_data.get("fecha_entrega_solicitada") or order_data.get("requested_delivery_date")
            order.notes = order_data.get("observaciones") or order_data.get("notes") or ""
            order.status = "pedido_pendiente_revision"
            order.review_reasons = None
        else:
            order = Order(
                company_id=inbound_message.company_id,
                conversation_id=inbound_message.conversation_id,
                email_id=email.id if email else None,
                customer_id=customer.id if customer else None,
                validated_customer_id=customer.id if customer else None,
                customer_detected_name=detected_name or None,
                customer_identification_method=method,
                customer_score=round(customer_score * 100, 2),
                order_date=order_data.get("fecha_pedido") or order_data.get("order_date"),
                requested_delivery_date=order_data.get("fecha_entrega_solicitada") or order_data.get("requested_delivery_date"),
                notes=order_data.get("observaciones") or order_data.get("notes") or "",
                status="pedido_pendiente_revision",
            )
            db.add(order)
            db.flush()
        review_reasons: list[str] = list(extracted.get("motivos_revision") or [])
        if detected_customer_is_tenant:
            review_reasons.append(
                "La empresa detectada corresponde al receptor del pedido y se ha descartado como cliente."
            )
        if extracted.get("requiere_revision_humana") and not review_reasons:
            review_reasons.append("La extraccion requiere revision humana")
        if not customer:
            review_reasons.append("Cliente no identificado")
            if customer_decision["selected"]:
                review_reasons.append(
                    f"Cliente candidato por {customer_decision['selected'].source}: "
                    f"{customer_decision['selected'].reason}. Requiere validacion humana"
                )
        elif customer_decision["selected"]:
            review_reasons.append(f"Cliente elegido por {customer_decision['selected'].source}: {customer_decision['selected'].reason}")
        elif customer_decision["evidence"]:
            review_reasons.append(f"Cliente con evidencia parcial: {customer_decision['evidence'][0]['reason']}")
        for raw_line in self._lines_from_extraction(extracted):
            for uncertainty in raw_line.get("uncertainties") or []:
                if isinstance(uncertainty, dict) and uncertainty.get("reason"):
                    review_reasons.append(str(uncertainty["reason"]))
            if raw_line.get("requires_review"):
                review_reasons.append("Linea marcada para revision por extraccion")
            product_name = raw_line.get("producto_detectado") or raw_line.get("producto") or raw_line.get("description") or raw_line.get("descripcion")
            reference = raw_line.get("referencia_detectada") or raw_line.get("referencia") or raw_line.get("reference")
            ean = raw_line.get("ean") or raw_line.get("sku")
            quantity = raw_line.get("cantidad") if "cantidad" in raw_line else raw_line.get("quantity")
            product, product_method, product_score = self.product_matching.match(db, inbound_message.company_id, reference=reference, detected_name=product_name)
            semantic_candidates = []
            if not fast_path:
                product_decision = self.decision.product_decision(
                    db,
                    inbound_message.company_id,
                    customer_id=customer.id if customer else None,
                    reference=reference or None,
                    detected_name=product_name or None,
                    ean=ean or None,
                    text=raw_line.get("texto_original") or raw_line.get("original_text") or source_text,
                )
                semantic_candidates = product_decision.get("semantic_candidates") or []
                if product_decision["selected"] and product_decision["selected"].product_id:
                    candidate_product = db.get(Product, product_decision["selected"].product_id)
                    candidate_confidence = product_decision["selected"].confidence
                    if candidate_product and (not product or candidate_confidence >= product_score):
                        product = candidate_product
                        product_method = product_decision["selected"].source
                        product_score = candidate_confidence
            confidence = _confidence(raw_line.get("confianza_extraccion") or raw_line.get("confidence"), 0.7)
            parsed_quantity, quantity_issue = _parse_positive_quantity(quantity)
            if fast_path:
                product_is_deterministic = product_method in {
                    "referencia_exacta",
                    "referencia_parcial_unica",
                    "alias",
                    "alias_aprendido",
                }
            else:
                selected_product = product_decision["selected"]
                deterministic_sources = {
                    "exact_reference",
                    "exact_ean",
                    "exact_sku",
                    "approved_alias",
                    "learned_alias",
                }
                product_is_deterministic = bool(
                    product
                    and selected_product
                    and selected_product.product_id == product.id
                    and not product_decision["requires_review"]
                    and selected_product.source in deterministic_sources
                )
                # Keep the legacy matcher's safe unique-reference behaviour when
                # the decision engine has no candidate (for example a partial
                # reference).
                product_is_deterministic = product_is_deterministic or product_method in {
                    "referencia_exacta",
                    "referencia_parcial_unica",
                    "alias",
                    "alias_aprendido",
                }
            doubt_parts: list[str] = []
            if not product:
                doubt_parts.append(f"Producto no encontrado por {product_method}")
            elif not product_is_deterministic:
                doubt_parts.append(f"Producto propuesto por {product_method}; requiere validacion humana")
            if raw_line.get("requires_review"):
                doubt_parts.append("Linea marcada para revision por extraccion")
                product_is_deterministic = False
            if quantity_issue:
                doubt_parts.append(quantity_issue)
            doubt = "; ".join(dict.fromkeys(doubt_parts))
            if doubt:
                review_reasons.append(doubt)
                if semantic_candidates:
                    top_semantic = semantic_candidates[0]
                    review_reasons.append(f"Candidato semantico sugerido: {top_semantic.label} ({top_semantic.confidence:.2f}). Requiere validacion humana.")
            db.add(
                OrderLine(
                    company_id=inbound_message.company_id,
                    order_id=order.id,
                    product_id=product.id if product else None,
                    validated_product_id=product.id if product_is_deterministic and parsed_quantity is not None else None,
                    original_text=raw_line.get("texto_original") or raw_line.get("original_text") or product_name or source_text[:180],
                    detected_reference=reference,
                    detected_product=product_name,
                    quantity=parsed_quantity,
                    unit=raw_line.get("unidad") or raw_line.get("unit") or "",
                    extraction_confidence=confidence,
                    line_score=round(product_score * 80 + confidence * 20, 2),
                    validation_status="validated" if product_is_deterministic and parsed_quantity is not None else "pending",
                    doubt_reason=doubt,
                )
            )
        db.flush()
        db.refresh(order)
        order.review_reasons = "; ".join(dict.fromkeys(review_reasons))
        return order


def touch_message(db: Session, message: InboundMessage, *, status: str | None = None, step: str | None = None, score: float | None = None) -> None:
    if status is not None:
        message.status = status
    if step is not None:
        message.processing_step = step
    if score is not None:
        message.score = score
    message.last_processed_at = datetime.now(timezone.utc)
