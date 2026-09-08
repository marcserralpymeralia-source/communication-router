import json
import re
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from sqlalchemy.orm import Session

from sqlalchemy import select

from app.agent.platform import UnifiedOrderPipelineService, _parse_positive_quantity
from app.orders.scoring import calculate_order_score
from app.channels.service import get_or_create_channel
from app.core.storage import resolve_temp_storage_dir
from app.db.models import Company, Customer, CustomerAlias, CustomerContactPoint, CustomerDomain, Email, EmailAttachment, EmailSettings, InboundMessage, InputChannel, LLMSettings, Order, OrderLine, Product, ProductAlias, PromptTemplate, PromptVersion, ScoringSettings
from app.messages.service import NormalizedMessage, persist_normalized_message
from app.orders.state import ORDER_STATE
from app.logs.service import log_action
from app.settings.integrations import classify_sample, extract_sample
from app.settings.service import get_or_create_settings


class EmailService:
    def test_connection(self) -> bool:
        return True


class LLMService:
    def extract_order(self, text: str) -> dict:
        return {
            "tipo_correo": "pedido",
            "confianza_tipo_correo": 0.94,
            "cliente": {
                "nombre_detectado": "Anchi Demo SL",
                "codigo_cliente_detectado": None,
                "cliente_id_validado": None,
                "metodo_identificacion": "nombre_en_pdf",
                "confianza": 0.91,
            },
            "pedido": {
                "fecha_pedido": date.today().isoformat(),
                "fecha_entrega_solicitada": None,
                "observaciones": "Pedido mock generado para probar el flujo.",
                "lineas": [
                    {
                        "texto_original": "10 unidades producto demo",
                        "referencia_detectada": None,
                        "producto_detectado": "Articulo Demo",
                        "cantidad": 10,
                        "unidad": "cajas",
                        "confianza_extraccion": 0.93,
                    }
                ],
            },
            "requiere_revision_humana": True,
            "motivos_revision": [],
        }


class PDFExtractionService:
    def extract_text(self, path: str) -> str:
        return ""


class MatchingService:
    def find_customer(self, db: Session, company_id: int, *, sender: str, detected_name: str | None, detected_code: str | None = None) -> tuple[Customer | None, str, float]:
        if detected_code:
            customer = db.query(Customer).filter(Customer.company_id == company_id, Customer.code == detected_code).one_or_none()
            if customer:
                return customer, "codigo", 1.0
        if sender:
            sender_clean = sender.strip().lower()
            exact_point = db.query(CustomerContactPoint).filter(
                CustomerContactPoint.company_id == company_id,
                CustomerContactPoint.active == True,  # noqa: E712
                CustomerContactPoint.value.ilike(sender_clean),
            ).one_or_none()
            if exact_point:
                customer = db.get(Customer, exact_point.customer_id)
                if customer:
                    return customer, "punto_contacto", 0.98
        if sender and "@" in sender:
            sender_clean = sender.strip().lower()
            domain = sender_clean.split("@", 1)[1]
            match = db.query(CustomerContactPoint).filter(
                CustomerContactPoint.company_id == company_id,
                CustomerContactPoint.type == "email",
                CustomerContactPoint.active == True,  # noqa: E712
                CustomerContactPoint.value.ilike(sender_clean),
            ).one_or_none()
            if not match:
                match = db.query(CustomerContactPoint).filter(
                    CustomerContactPoint.company_id == company_id,
                    CustomerContactPoint.type == "domain",
                    CustomerContactPoint.active == True,  # noqa: E712
                    CustomerContactPoint.value == domain,
                ).one_or_none()
            if not match:
                match = db.query(CustomerDomain).filter(CustomerDomain.company_id == company_id, CustomerDomain.domain == domain).one_or_none()
            if match:
                return db.get(Customer, match.customer_id), "contact_point" if isinstance(match, CustomerContactPoint) else "dominio_email", 0.97 if isinstance(match, CustomerContactPoint) else 0.95
        if detected_name:
            contact_match = db.query(CustomerContactPoint).filter(
                CustomerContactPoint.company_id == company_id,
                CustomerContactPoint.active == True,  # noqa: E712
                CustomerContactPoint.value.ilike(detected_name),
            ).one_or_none()
            if contact_match:
                customer = db.get(Customer, contact_match.customer_id)
                if customer:
                    return customer, "punto_contacto", 0.95
            alias = db.query(CustomerAlias).filter(CustomerAlias.company_id == company_id, CustomerAlias.alias.ilike(detected_name)).one_or_none()
            if alias:
                return db.get(Customer, alias.customer_id), "alias", 0.92
            customers = db.query(Customer).filter(Customer.company_id == company_id).all()
            best = max(customers, key=lambda c: SequenceMatcher(None, detected_name.lower(), c.fiscal_name.lower()).ratio(), default=None)
            if best:
                score = SequenceMatcher(None, detected_name.lower(), best.fiscal_name.lower()).ratio()
                if score >= 0.65:
                    return best, "nombre_aproximado", score
        return None, "sin_identificar", 0

    def find_product(self, db: Session, company_id: int, *, reference: str | None, detected_name: str | None) -> tuple[Product | None, str, float]:
        if reference:
            product = db.query(Product).filter(Product.company_id == company_id, Product.reference == reference).one_or_none()
            if product and _product_name_is_compatible(detected_name, product):
                return product, "referencia_exacta", 1.0
        if detected_name:
            alias = db.query(ProductAlias).filter(ProductAlias.company_id == company_id, ProductAlias.alias.ilike(detected_name)).one_or_none()
            if alias:
                return db.get(Product, alias.product_id), "alias", 0.92
            products = db.query(Product).filter(Product.company_id == company_id).all()
            best = max(products, key=lambda p: _match_text_similarity(detected_name, p.name), default=None)
            if best:
                score = _match_text_similarity(detected_name, best.name)
                if score >= 0.6:
                    return best, "nombre_aproximado", score
        return None, "sin_referencia", 0


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


class ScoringService:
    def score_order(self, db: Session, order: Order, *, use_proposals: bool = False) -> float:
        settings = get_or_create_settings(db, ScoringSettings, order.company_id)
        return calculate_order_score(order, settings, use_proposals=use_proposals).total

    def status_for_score(self, db: Session, company_id: int, score: float) -> str:
        return ORDER_STATE.status_for_score(db, company_id, score)


def _json_from_content(content: str) -> dict:
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


class AgentProcessingService:
    def __init__(self) -> None:
        self.pipeline = UnifiedOrderPipelineService()

    def process_email(self, db: Session, email: Email, user=None, force_order: bool = False) -> dict:
        return self._process_email(db, email, user=user, force_order=force_order, fast_path=False)

    def process_email_fast(self, db: Session, email: Email, user=None, force_order: bool = False) -> dict:
        return self._process_email(db, email, user=user, force_order=force_order, fast_path=True)

    def _process_email(self, db: Session, email: Email, user=None, force_order: bool = False, *, fast_path: bool) -> dict:
        channel = get_or_create_channel(
            db,
            email.company_id,
            "email",
        )
        if not channel.is_active:
            raise ValueError("Email channel is disabled for this tenant")

        normalized_message = NormalizedMessage(
            company_id=email.company_id,
            channel_key="email",
            provider="imap",
            external_id=email.external_id,
            sender=email.sender,
            subject=email.subject,
            text_content=email.body or email.extracted_text or "",
            received_at=getattr(email, "received_at", None),
            metadata={"email_id": email.id},
        )
        inbound_message, conversation = persist_normalized_message(
            db,
            normalized_message,
            content_type="email",
            has_attachments=bool(email.attachments),
            has_pdf=bool(email.has_pdf),
        )
        email.conversation_id = conversation.id
        db.flush()
        if not force_order:
            if inbound_message.order_id:
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
            if inbound_message.status == "no_order":
                return {"ok": True, "message": "Entrada ya clasificada como no pedido.", "status": "no_order"}
            if inbound_message.status == "doubtful":
                return {"ok": False, "message": inbound_message.processing_error or "Entrada dudosa.", "status": "doubtful"}
        pipeline_kwargs = {
            "user": user,
            "force_order": force_order,
            "email": email,
        }
        if fast_path:
            pipeline_kwargs["email_fast_path"] = True
        result = self.pipeline.process_inbound_message(
            db,
            inbound_message,
            **pipeline_kwargs,
        )
        if result.get("order_id") or result.get("status") == "order_detected":
            email.agent_status = "processed_order_detected"
            email.status = "pedido_detectado"
            email.detected_type = "pedido"
        elif result.get("status") == "no_order":
            email.agent_status = "processed_no_order"
            email.status = "no_pedido"
            email.detected_type = "no_pedido"
        elif email.status == "dudoso":
            email.agent_status = "processed_doubtful"
        elif result.get("ok") is False:
            email.agent_status = "processing_error"
            email.status = "error_processing"
        email.processing_result_json = json.dumps(result, ensure_ascii=False)
        email.last_processed_at = datetime.now(timezone.utc)
        db.commit()
        return result

    def _mark_error(self, db: Session, email: Email, user, message: str) -> dict:
        email.agent_status = "processing_error"
        email.status = "error_processing"
        email.processing_error = message
        email.last_processed_at = datetime.now(timezone.utc)
        db.commit()
        log_action(db, company_id=email.company_id, user=user, action="agent.processing_error", entity_type="email", entity_id=email.id, message=message[:500])
        return {"ok": False, "message": message}

    def _input_text(self, email: Email) -> str:
        source_parts: list[str] = []
        seen_parts: set[str] = set()

        def add_part(value: str | None, *, label: str | None = None) -> None:
            normalized = str(value or "").strip()
            if not normalized or normalized in seen_parts:
                return
            seen_parts.add(normalized)
            source_parts.append(f"[{label}]\n{normalized}" if label else normalized)

        add_part(email.body or email.extracted_text)
        for attachment in email.attachments or []:
            add_part(attachment.extracted_text, label=f"Adjunto: {attachment.filename}")
        source = "\n\n".join(source_parts)
        return f"Asunto: {email.subject}\nRemitente: {email.sender}\n\n{source}".strip()

    def _classify(self, db: Session, settings: LLMSettings, company_id: int, text: str) -> dict:
        prompt = _active_prompt(
            db,
            company_id,
            "classification",
            "Clasifica el correo como pedido, no_pedido, consulta, incidencia o dudoso. Responde solo JSON con tipo_correo, confianza y motivo.",
        )
        result = classify_sample(db, settings, company_id, text[:12000], prompt)
        if not result.get("ok"):
            raise RuntimeError(result.get("message") or "Error llamando al proveedor IA.")
        return _json_from_content(result.get("content", ""))

    def _extract(self, db: Session, settings: LLMSettings, company_id: int, text: str) -> dict:
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

    def _customer_from_extraction(self, data: dict) -> dict:
        customer = data.get("cliente") or data.get("customer") or {}
        if isinstance(customer, str):
            customer = {"nombre_detectado": customer}
        return customer

    def _order_from_extraction(self, data: dict) -> dict:
        return data.get("pedido") or data.get("order") or data

    def _lines_from_extraction(self, data: dict) -> list[dict]:
        order_data = self._order_from_extraction(data)
        lines = order_data.get("lineas") or order_data.get("lines") or data.get("lineas") or []
        return lines if isinstance(lines, list) else []

    def _create_order(self, db: Session, email: Email, extracted: dict, source_text: str) -> Order:
        customer_data = self._customer_from_extraction(extracted)
        order_data = self._order_from_extraction(extracted)
        detected_name = customer_data.get("nombre_detectado") or customer_data.get("name") or customer_data.get("nombre") or ""
        detected_code = customer_data.get("codigo_cliente_detectado") or customer_data.get("codigo") or customer_data.get("code")
        customer, method, customer_score = self.matching.find_customer(db, email.company_id, sender=email.sender, detected_name=detected_name, detected_code=detected_code)
        order = Order(
            company_id=email.company_id,
            conversation_id=email.conversation_id,
            email_id=email.id,
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
        review_reasons: list[str] = []
        if not customer:
            review_reasons.append("Cliente no identificado")
        for raw_line in self._lines_from_extraction(extracted):
            product_name = raw_line.get("producto_detectado") or raw_line.get("producto") or raw_line.get("description") or raw_line.get("descripcion")
            reference = raw_line.get("referencia_detectada") or raw_line.get("referencia") or raw_line.get("reference")
            quantity = raw_line.get("cantidad") if "cantidad" in raw_line else raw_line.get("quantity")
            product, product_method, product_score = self.matching.find_product(db, email.company_id, reference=reference, detected_name=product_name)
            confidence = _confidence(raw_line.get("confianza_extraccion") or raw_line.get("confidence"), 0.7)
            parsed_quantity, quantity_issue = _parse_positive_quantity(quantity)
            product_is_deterministic = product_method in {"referencia_exacta", "alias"}
            if raw_line.get("requires_review"):
                product_is_deterministic = False
            doubt_parts: list[str] = []
            if not product:
                doubt_parts.append(f"Producto no encontrado por {product_method}")
            elif not product_is_deterministic:
                doubt_parts.append(f"Producto propuesto por {product_method}; requiere validacion humana")
            if raw_line.get("requires_review"):
                doubt_parts.append("Linea marcada para revision por extraccion")
            if quantity_issue:
                doubt_parts.append(quantity_issue)
            doubt = "; ".join(dict.fromkeys(doubt_parts))
            if doubt:
                review_reasons.append(doubt)
            db.add(OrderLine(
                company_id=email.company_id,
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
            ))
        db.flush()
        db.refresh(order)
        order.review_reasons = "; ".join(dict.fromkeys(review_reasons))
        order.score = self.scoring.score_order(db, order)
        order.status = self.scoring.status_for_score(db, email.company_id, order.score)
        log_action(db, company_id=email.company_id, user=None, action="order.scoring_calculated", entity_type="order", entity_id=order.id, message=f"Scoring calculado: {order.score}")
        return order


class MockAgentService:
    def __init__(self) -> None:
        self.llm = LLMService()
        self.matching = MatchingService()
        self.scoring = ScoringService()

    def _create_mock_pdf(self, order_key: str) -> Path:
        storage_dir = resolve_temp_storage_dir("attachments")
        storage_dir.mkdir(parents=True, exist_ok=True)
        path = storage_dir / f"{order_key}.pdf"
        if path.exists():
            return path
        pdf = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 188 >>
stream
BT
/F1 18 Tf
72 760 Td
(Pedido de prueba - Anchi Demo SL) Tj
/F1 12 Tf
0 -42 Td
(Linea 1: 10 unidades Producto Demo) Tj
0 -24 Td
(Fecha solicitada: pendiente) Tj
0 -24 Td
(Este PDF mock permite comparar documento y pedido extraido.) Tj
ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000241 00000 n 
0000000479 00000 n 
trailer
<< /Root 1 0 R /Size 6 >>
startxref
549
%%EOF
"""
        path.write_bytes(pdf)
        return path

    def create_mock_order(self, db: Session, company_id: int) -> Order:
        company = db.scalar(select(Company).where(Company.id == company_id))
        company_name = company.name if company else f"Tenant {company_id}"
        order_key = f"mock-{company_id}-{date.today().isoformat()}"
        email = Email(
            company_id=company_id,
            external_id=order_key,
            sender=f"compras+{company_id}@example.local",
            subject="Pedido de prueba",
            body="Adjuntamos pedido de prueba.",
            extracted_text=f"10 unidades producto demo para {company_name}",
            status="pedido_detectado",
            detected_type="pedido",
        )
        db.add(email)
        db.flush()
        pdf_path = self._create_mock_pdf(order_key)
        db.add(
            EmailAttachment(
                company_id=company_id,
                email_id=email.id,
                filename=pdf_path.name,
                content_type="application/pdf",
                storage_path=str(pdf_path),
                extracted_text=email.extracted_text,
            )
        )
        extracted = self.llm.extract_order(email.extracted_text or "")
        customer_data = extracted["cliente"]
        customer, method, customer_score = self.matching.find_customer(
            db,
            company_id,
            sender=email.sender,
            detected_name=customer_data["nombre_detectado"],
            detected_code=customer_data["codigo_cliente_detectado"],
        )
        order = Order(
            company_id=company_id,
            email_id=email.id,
            customer_id=customer.id if customer else None,
            validated_customer_id=customer.id if customer else None,
            customer_detected_name=customer_data["nombre_detectado"],
            customer_identification_method=method,
            customer_score=round(customer_score * 100, 2),
            order_date=extracted["pedido"]["fecha_pedido"],
            requested_delivery_date=extracted["pedido"]["fecha_entrega_solicitada"],
            notes=extracted["pedido"]["observaciones"],
            review_reasons=", ".join(extracted["motivos_revision"]),
        )
        db.add(order)
        db.flush()
        for raw_line in extracted["pedido"]["lineas"]:
            product, product_method, product_score = self.matching.find_product(
                db,
                company_id,
                reference=raw_line["referencia_detectada"],
                detected_name=raw_line["producto_detectado"],
            )
            product_is_deterministic = product_method in {"referencia_exacta", "alias"}
            line = OrderLine(
                company_id=company_id,
                order_id=order.id,
                product_id=product.id if product else None,
                validated_product_id=product.id if product_is_deterministic else None,
                original_text=raw_line["texto_original"],
                detected_reference=raw_line["referencia_detectada"],
                detected_product=raw_line["producto_detectado"],
                quantity=raw_line["cantidad"],
                unit=raw_line["unidad"],
                extraction_confidence=raw_line["confianza_extraccion"],
                line_score=round(product_score * 80 + raw_line["confianza_extraccion"] * 20, 2),
                validation_status="validated" if product_is_deterministic else "pending",
                doubt_reason="" if product_is_deterministic else (
                    f"Producto no encontrado por {product_method}" if not product else
                    f"Producto propuesto por {product_method}; requiere validacion humana"
                ),
            )
            db.add(line)
        db.flush()
        db.refresh(order)
        order.score = self.scoring.score_order(db, order)
        order.status = self.scoring.status_for_score(db, company_id, order.score)
        db.commit()
        db.refresh(order)
        return order
