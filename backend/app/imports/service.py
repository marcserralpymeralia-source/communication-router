import json
import re
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from difflib import SequenceMatcher

import pandas as pd
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
try:
    from docx import Document as DocxDocument
except Exception:  # pragma: no cover - optional dependency fallback
    DocxDocument = None

from app.core.storage import ensure_directory, resolve_temp_storage_dir

PREVIEW_DIR = resolve_temp_storage_dir("import_previews")
PREVIEW_MAX_AGE_SECONDS = 24 * 60 * 60

CUSTOMER_FIELDS = {
    "code": "Codigo cliente",
    "fiscal_name": "Razon social",
    "commercial_name": "Nombre comercial",
    "tax_id": "CIF/DNI",
    "primary_email": "Email principal",
    "additional_emails": "Emails adicionales",
    "associated_emails": "Emails asociados",
    "associated_phones": "Telefonos asociados",
    "contact_name": "Persona de contacto",
    "contact_role": "Cargo/contacto",
    "habitual_channel": "Canal habitual",
    "domains": "Dominios",
    "aliases": "Alias",
    "phone": "Telefono",
    "address": "Direccion",
    "city": "Municipio",
    "province": "Provincia",
    "country": "Pais",
    "delegation": "Delegacion",
    "assigned_salesperson": "Comercial asignado",
    "accounting_code": "Codigo contable",
    "category": "Categoria",
    "company_inactive": "Baja empresa",
    "status": "Estado",
    "notes": "Observaciones",
    "payment_terms": "Forma de pago",
    "tariff": "Tarifa",
    "customer_group": "Grupo de cliente",
    "internal_notes": "Notas internas",
    "conditions": "Condiciones iniciales",
    "useful_comments": "Comentarios utiles",
}

PRODUCT_FIELDS = {
    "reference": "Articulo/referencia",
    "alternative_code": "Codigo alternativo",
    "name": "Descripcion",
    "description_cont": "Descripcion completa",
    "brand": "Marca",
    "usual_supplier": "Proveedor habitual",
    "family": "Familia",
    "subfamily": "Subfamilia",
    "sale_unit": "Unidad venta",
    "sale_price": "Precio venta",
    "discount_percent": "Descuento",
    "size_group": "Grupo talla",
    "colors": "Colores",
    "entry_date": "Fecha alta",
    "obsolete": "Obsoleto",
    "article_type": "Tipo articulo",
    "replenishment_warehouse": "Almacen reposicion",
    "aliases": "Alias",
    "status": "Estado",
    "notes": "Observaciones",
}

CUSTOMER_KNOWLEDGE_ARTICLE_FIELDS = {
    "exercise": "Ejercicio",
    "series": "Serie",
    "document_number": "Albarán",
    "order_date": "Fecha",
    "reference": "Artículo",
    "name": "Descripción",
    "quantity": "Unidades",
    "sale_price": "Precio",
}

FIELD_ALIASES = {
    "customers": {
        "code": ["cód. cliente", "cod. cliente", "codigo cliente", "codcli", "codigo", "code"],
        "fiscal_name": ["razón social", "razon social", "razonsocial", "nombre fiscal", "nombre", "cliente"],
        "tax_id": ["cif/dni", "cif/nif", "cif", "dni", "nif"],
        "primary_email": ["email principal", "mail", "email", "correo"],
        "additional_emails": ["emails asociados", "emails adicionales", "otros emails", "email 2", "email_2"],
        "associated_emails": ["emails asociados", "emails adicionales", "email_1", "email_2", "email_3"],
        "associated_phones": ["telefonos asociados", "teléfonos asociados", "telefono_1", "telefono_2", "telefono 2"],
        "contact_name": ["persona de contacto", "contacto", "nombre contacto"],
        "contact_role": ["cargo", "puesto", "rol contacto", "contacto cargo"],
        "habitual_channel": ["canal habitual", "canal"],
        "domains": ["dominios", "dominios asociados", "domain"],
        "aliases": ["alias", "aliases"],
        "delegation": ["deleg.", "delegacion", "delegación"],
        "phone": ["teléfono", "telefono", "phone"],
        "city": ["municipio", "poblacion", "población", "city"],
        "province": ["provincia", "province"],
        "assigned_salesperson": ["comercial asig.", "comercial asignado"],
        "accounting_code": ["cód. contable", "cod. contable", "codigo contable"],
        "company_inactive": ["baja empresa", "baja"],
        "category": ["categoría", "categoria", "category"],
        "payment_terms": ["forma de pago", "condiciones de pago"],
        "tariff": ["tarifa"],
        "customer_group": ["grupo de cliente", "grupo"],
        "internal_notes": ["notas internas", "observaciones internas"],
        "conditions": ["condiciones iniciales", "condiciones"],
        "useful_comments": ["comentarios utiles", "comentarios útiles", "comentarios", "nota"],
    },
    "products": {
        "reference": ["artículo", "articulo", "referencia", "reference", "ref"],
        "name": ["descripción", "descripcion", "nombre producto", "producto", "name"],
        "brand": ["marca", "brand"],
        "usual_supplier": ["proveedor habitual", "proveedor"],
        "alternative_code": ["cód. alternativo", "cod. alternativo", "codigo alternativo"],
        "family": ["familia", "family"],
        "subfamily": ["subfamilia", "subfamily"],
        "sale_price": ["precio venta", "precio", "sale_price"],
        "discount_percent": ["%dto.", "%dto", "dto", "descuento"],
        "size_group": ["gr.talla", "grupo talla"],
        "colors": ["colores", "colors"],
        "entry_date": ["fecha alta", "fecha"],
        "obsolete": ["obsoleto", "obsolete"],
        "article_type": ["tipo artículo", "tipo articulo"],
        "description_cont": ["descripción (cont.)", "descripcion (cont.)"],
        "replenishment_warehouse": ["alm. reposición", "alm. reposicion"],
    },
    "customer_knowledge_articles": {
        "exercise": ["ejercicio", "año", "anio"],
        "series": ["serie", "series"],
        "document_number": ["albarán", "albaran", "nº albarán", "numero albaran", "número albarán", "documento", "albaran nº"],
        "order_date": ["fecha", "fecha albarán", "fecha albaran", "fecha pedido", "date"],
        "reference": ["artículo", "articulo", "referencia", "reference", "ref", "código artículo", "codigo articulo"],
        "name": ["descripción", "descripcion", "producto", "artículo desc", "article", "name"],
        "quantity": ["unidades", "cantidad", "qty", "uds", "ud"],
        "sale_price": ["precio", "importe", "pvp", "precio unitario", "price"],
    },
}


@dataclass
class ImportValidation:
    rows_total: int
    rows_new: int
    rows_update: int
    duplicates: int
    rows_error: int
    errors: list[str]
    warnings: list[str]
    invalid_emails: int = 0
    invalid_phones: int = 0


def read_table_from_bytes(content: bytes, name: str, encoding: str = "utf-8") -> pd.DataFrame:
    lower_name = name.lower()
    if lower_name.endswith((".xlsx", ".xls")):
        return pd.read_excel(BytesIO(content)).fillna("")
    if lower_name.endswith(".pdf"):
        lines = _extract_lines_from_pdf_bytes(content)
        return pd.DataFrame({"texto": lines or [""]})
    if lower_name.endswith(".docx"):
        lines = _extract_lines_from_docx_bytes(content)
        return pd.DataFrame({"texto": lines or [""]})
    if lower_name.endswith(".txt"):
        text = content.decode(encoding, errors="ignore")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return pd.DataFrame({"texto": lines or [""]})
    return pd.read_csv(BytesIO(content), sep=None, engine="python", encoding=encoding).fillna("")


async def read_table(file: UploadFile) -> pd.DataFrame:
    content = await file.read()
    return read_table_from_bytes(content, file.filename or "import.csv")


def _extract_lines_from_pdf_bytes(content: bytes) -> list[str]:
    raw = content.decode("latin-1", errors="ignore")
    chunks: list[str] = []
    for match in re.finditer(r"\((.*?)\)\s*Tj", raw, re.S):
        chunks.append(_decode_pdf_text(match.group(1)))
    for match in re.finditer(r"\[(.*?)\]\s*TJ", raw, re.S):
        chunks.extend(_decode_pdf_text(item) for item in re.findall(r"\((.*?)\)", match.group(1), re.S))
    text = re.sub(r"[ \t]+", " ", "\n".join(item for item in chunks if item.strip()))
    return [line.strip() for line in text.splitlines() if line.strip()]


def _extract_lines_from_docx_bytes(content: bytes) -> list[str]:
    if DocxDocument is None:
        return []
    document = DocxDocument(BytesIO(content))
    return [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text and paragraph.text.strip()]


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _split_multi_values(value: str) -> list[str]:
    raw = (value or "").replace("\n", ",").replace("\r", ",").replace(";", ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _normalize_phone(value: str) -> str:
    return re.sub(r"[\s\-.()]+", "", value.strip())


def _is_valid_email(value: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value.strip()))


def _is_valid_phone(value: str) -> bool:
    digits = re.sub(r"\D+", "", value)
    return len(digits) >= 7


def _name_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_name(left), normalize_name(right)).ratio()


def fields_for(entity_type: str) -> dict[str, str]:
    if entity_type == "customers":
        return CUSTOMER_FIELDS
    if entity_type == "products":
        return PRODUCT_FIELDS
    if entity_type == "customer_knowledge_articles":
        return CUSTOMER_KNOWLEDGE_ARTICLE_FIELDS
    return PRODUCT_FIELDS


def _customer_name_is_mapped(mapping: dict[str, str]) -> bool:
    return any(field in {"fiscal_name", "commercial_name"} for field in mapping.values())


def _parse_date_value(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed


def guess_mapping(entity_type: str, columns: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    normalized_columns = {normalize_name(column): column for column in columns}
    for field, names in FIELD_ALIASES[entity_type].items():
        for name in names:
            if normalize_name(name) in normalized_columns:
                mapping[normalized_columns[normalize_name(name)]] = field
                break
    return mapping


def dataframe_to_preview(df: pd.DataFrame, limit: int = 5) -> list[dict[str, str]]:
    return df.head(limit).astype(str).to_dict(orient="records")


async def create_preview(
    file: UploadFile,
    entity_type: str,
    encoding: str = "utf-8",
    *,
    customer_id: int | None = None,
    import_kind: str = "",
) -> dict:
    content = await file.read()
    token = uuid.uuid4().hex
    suffix = Path(file.filename or "import.csv").suffix.lower() or ".csv"
    cleanup_expired_previews()
    ensure_directory(PREVIEW_DIR)
    (PREVIEW_DIR / f"{token}{suffix}").write_bytes(content)
    df = read_table_from_bytes(content, file.filename or "import.csv", encoding=encoding)
    columns = [str(column) for column in df.columns]
    return {
        "token": token,
        "filename": file.filename or "import",
        "entity_type": entity_type,
        "columns": columns,
        "rows_total": len(df),
        "preview_rows": dataframe_to_preview(df),
        "guessed_mapping": guess_mapping(entity_type, columns),
        "fields": fields_for(entity_type),
        "customer_id": customer_id,
        "import_kind": import_kind,
    }


def read_preview(token: str, filename: str, encoding: str = "utf-8") -> pd.DataFrame:
    matches = list(PREVIEW_DIR.glob(f"{token}.*"))
    if not matches:
        raise FileNotFoundError("No se encontro la previsualizacion de importacion.")
    return read_table_from_bytes(matches[0].read_bytes(), filename or matches[0].name, encoding=encoding)


def cleanup_expired_previews(*, max_age_seconds: int = PREVIEW_MAX_AGE_SECONDS) -> int:
    """Remove only stale files created under the dedicated temporary directory."""
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds debe ser no negativo")
    if not PREVIEW_DIR.exists():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for candidate in PREVIEW_DIR.iterdir():
        try:
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "si", "sí", "s", "yes", "true", "x", "baja", "obsoleto", "inactive", "inactivo"}


def as_float(value: str) -> float | None:
    if not value:
        return None
    normalized = value.replace("%", "").strip()
    if "," in normalized and "." in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def mapped_row(row, mapping: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for column, field in mapping.items():
        if field and field != "__skip__":
            value = row.get(column, "")
            result[field] = "" if value is None else str(value).strip()
    return result


def _customer_lookup(db: Session, company_id: int, data: dict[str, str]) -> tuple[object | None, str]:
    from app.db.models import Customer, CustomerContactPoint, CustomerDomain

    code = (data.get("code") or "").strip()
    tax_id = (data.get("tax_id") or "").strip()
    primary_email = _normalize_email(data.get("primary_email") or "") if data.get("primary_email") else ""
    phones = [_normalize_phone(value) for value in _split_multi_values(data.get("phone") or "") if value]
    domains = [value.lower() for value in _split_multi_values(data.get("domains") or "")]
    name = (data.get("fiscal_name") or data.get("commercial_name") or "").strip()
    if code:
        customer = db.query(Customer).filter(Customer.company_id == company_id, Customer.code == code).one_or_none()
        if customer:
            return customer, "code"
    if tax_id:
        customer = db.query(Customer).filter(Customer.company_id == company_id, Customer.tax_id == tax_id).one_or_none()
        if customer:
            return customer, "tax_id"
    if primary_email:
        customer = (
            db.query(Customer)
            .join(CustomerContactPoint, CustomerContactPoint.customer_id == Customer.id)
            .filter(
                Customer.company_id == company_id,
                CustomerContactPoint.company_id == company_id,
                CustomerContactPoint.type == "email",
                CustomerContactPoint.value == primary_email,
            )
            .one_or_none()
        )
        if customer:
            return customer, "email"
    for domain in domains:
        customer = (
            db.query(Customer)
            .join(CustomerDomain, CustomerDomain.customer_id == Customer.id)
            .filter(Customer.company_id == company_id, CustomerDomain.company_id == company_id, CustomerDomain.domain == domain)
            .one_or_none()
        )
        if customer:
            return customer, "domain"
    for phone in phones:
        customer = (
            db.query(Customer)
            .join(CustomerContactPoint, CustomerContactPoint.customer_id == Customer.id)
            .filter(
                Customer.company_id == company_id,
                CustomerContactPoint.company_id == company_id,
                CustomerContactPoint.type.in_(["phone", "whatsapp"]),
                CustomerContactPoint.value == phone,
            )
            .one_or_none()
        )
        if customer:
            return customer, "phone"
    if name:
        candidates = db.query(Customer).filter(Customer.company_id == company_id).limit(200).all()
        for customer in candidates:
            if _name_similarity(customer.fiscal_name, name) >= 0.92 or (customer.commercial_name and _name_similarity(customer.commercial_name, name) >= 0.92):
                return customer, "name"
    return None, ""


def validate_import(
    db: Session,
    *,
    company_id: int,
    entity_type: str,
    df: pd.DataFrame,
    mapping: dict[str, str],
    customer_id: int | None = None,
    import_kind: str = "",
) -> ImportValidation:
    from app.db.models import Customer, CustomerProductKnowledge, Product
    from app.master_data.service import find_product_match

    rows_new = rows_update = duplicates = rows_error = invalid_emails = invalid_phones = 0
    errors: list[str] = []
    warnings: list[str] = []
    seen_keys: set[str] = set()
    seen_names: list[str] = []
    if entity_type == "customers" and not _customer_name_is_mapped(mapping):
        return ImportValidation(
            rows_total=len(df),
            rows_new=0,
            rows_update=0,
            duplicates=0,
            rows_error=len(df) or 1,
            errors=["Debes mapear 'Razon social' o 'Nombre comercial' antes de importar clientes."],
            warnings=[],
        )
    for index, row in df.iterrows():
        data = mapped_row(row, mapping)
        if entity_type == "customers":
            name = data.get("fiscal_name") or data.get("commercial_name")
            key = data.get("code") or name
            if not name:
                rows_error += 1
                errors.append(f"Fila {index + 2}: falta razon social o nombre comercial.")
                continue
            emails = [value for value in _split_multi_values(data.get("primary_email", "")) + _split_multi_values(data.get("associated_emails", "")) if value]
            phones = [value for value in _split_multi_values(data.get("phone", "")) + _split_multi_values(data.get("associated_phones", "")) if value]
            if any(not _is_valid_email(email) for email in emails):
                invalid_emails += sum(1 for email in emails if not _is_valid_email(email))
                warnings.append(f"Fila {index + 2}: email con formato dudoso.")
            if any(not _is_valid_phone(phone) for phone in phones):
                invalid_phones += sum(1 for phone in phones if not _is_valid_phone(phone))
                warnings.append(f"Fila {index + 2}: telefono con formato dudoso.")
            existing, match_reason = _customer_lookup(db, company_id, data)
        elif entity_type == "customer_knowledge_articles":
            key = " | ".join(
                item
                for item in [
                    data.get("reference") or data.get("name") or "",
                    data.get("document_number") or "",
                    data.get("order_date") or "",
                ]
                if item
            )
            if not customer_id:
                rows_error += 1
                errors.append("Falta el cliente destino para importar el histórico de artículos.")
                continue
            if not (data.get("reference") or data.get("name")):
                rows_error += 1
                errors.append(f"Fila {index + 2}: falta articulo/referencia o descripcion.")
                continue
            product, match_reason = find_product_match(db, company_id, data)
            if not product:
                rows_error += 1
                errors.append(f"Fila {index + 2}: no se encontró producto para {data.get('reference') or data.get('name')}.")
                continue
            existing = db.scalar(
                select(CustomerProductKnowledge).where(
                    CustomerProductKnowledge.company_id == company_id,
                    CustomerProductKnowledge.customer_id == customer_id,
                    CustomerProductKnowledge.product_id == product.id,
                )
            )
            match_reason = "knowledge" if existing else ""
        else:
            key = data.get("reference") or data.get("name")
            if not key:
                rows_error += 1
                errors.append(f"Fila {index + 2}: falta articulo/referencia o descripcion.")
                continue
            existing, match_reason = find_product_match(db, company_id, data)
        if key in seen_keys:
            duplicates += 1
            warnings.append(f"Fila {index + 2}: posible duplicado en el archivo ({key}).")
        seen_keys.add(key)
        if entity_type == "customers":
            name = data.get("fiscal_name") or data.get("commercial_name") or ""
            if name:
                for prev_name in seen_names:
                    if _name_similarity(prev_name, name) >= 0.94:
                        duplicates += 1
                        warnings.append(f"Fila {index + 2}: posible duplicado por nombre similar ({name}).")
                        break
                seen_names.append(name)
        elif entity_type == "customer_knowledge_articles":
            article_name = data.get("name") or data.get("reference") or ""
            if article_name:
                for prev_name in seen_names:
                    if _name_similarity(prev_name, article_name) >= 0.96:
                        duplicates += 1
                        warnings.append(f"Fila {index + 2}: posible duplicado por artículo similar ({article_name}).")
                        break
                seen_names.append(article_name)
        if existing:
            rows_update += 1
            if entity_type == "customers" and match_reason in {"email", "domain", "phone", "name"}:
                warnings.append(f"Fila {index + 2}: posible cliente existente por {match_reason}.")
        else:
            rows_new += 1
    return ImportValidation(len(df), rows_new, rows_update, duplicates, rows_error, errors[:50], warnings[:50], invalid_emails, invalid_phones)


def apply_customer_data(customer, data: dict[str, str]) -> None:
    for field in [
        "code",
        "fiscal_name",
        "commercial_name",
        "tax_id",
        "primary_email",
        "phone",
        "address",
        "city",
        "province",
        "country",
        "delegation",
        "assigned_salesperson",
        "accounting_code",
        "category",
        "notes",
    ]:
        if data.get(field):
            setattr(customer, field, data[field])
    if data.get("company_inactive"):
        customer.company_inactive = as_bool(data["company_inactive"])
    if data.get("status"):
        customer.status = data["status"]
    elif customer.company_inactive:
        customer.status = "inactive"
    notes_fragments = []
    for key, label in [
        ("payment_terms", "Forma de pago"),
        ("tariff", "Tarifa"),
        ("customer_group", "Grupo"),
        ("internal_notes", "Notas internas"),
        ("conditions", "Condiciones"),
        ("useful_comments", "Comentarios"),
        ("habitual_channel", "Canal habitual"),
        ("contact_name", "Contacto"),
        ("contact_role", "Cargo"),
    ]:
        if data.get(key):
            notes_fragments.append(f"{label}: {data[key]}")
    if notes_fragments:
        current_notes = (customer.notes or "").strip()
        extra = " | ".join(notes_fragments)
        customer.notes = f"{current_notes} | {extra}".strip(" |") if current_notes else extra


def apply_product_data(product, data: dict[str, str]) -> None:
    for field in ["reference", "alternative_code", "name", "description_cont", "brand", "usual_supplier", "family", "subfamily", "sale_unit", "size_group", "colors", "entry_date", "article_type", "replenishment_warehouse", "notes"]:
        if data.get(field):
            setattr(product, field, data[field])
    if data.get("name"):
        product.description = data["name"]
    if data.get("sale_price"):
        product.sale_price = as_float(data["sale_price"])
    if data.get("discount_percent"):
        product.discount_percent = as_float(data["discount_percent"])
    if data.get("obsolete"):
        product.obsolete = as_bool(data["obsolete"])
    if data.get("status"):
        product.status = data["status"]
    elif product.obsolete:
        product.status = "inactive"


def confirm_import(
    db: Session,
    *,
    company_id: int,
    user,
    entity_type: str,
    filename: str,
    df: pd.DataFrame,
    mapping: dict[str, str],
    mode: str,
    customer_id: int | None = None,
    import_kind: str = "",
    save_template: bool = False,
    template_name: str = "",
):
    from app.agent.platform import LearningService
    from app.db.models import Customer, CustomerProductKnowledge, ImportJob, ImportMappingTemplate, Product, User
    from app.master_data.service import (
        find_customer_match,
        find_product_match,
        normalize_conflict_policy,
        upsert_customer,
        upsert_product,
    )

    created = updated = ignored = rows_error = 0
    errors: list[str] = []
    conflict_policy = normalize_conflict_policy(mode)
    if entity_type == "customers" and not _customer_name_is_mapped(mapping):
        raise ValueError("Debes mapear 'Razon social' o 'Nombre comercial' antes de importar clientes.")
    knowledge_service = LearningService() if entity_type == "customer_knowledge_articles" else None
    customer = db.get(Customer, customer_id) if customer_id else None
    if entity_type == "customer_knowledge_articles" and not customer:
        raise ValueError("Debes seleccionar un cliente antes de importar el histórico de artículos.")
    for row_index, row in df.iterrows():
        data = mapped_row(row, mapping)
        if entity_type == "customers":
            name = data.get("fiscal_name") or data.get("commercial_name")
            key = data.get("code") or name
            if not name:
                rows_error += 1
                errors.append(f"Fila {row_index + 2}: falta razon social o nombre comercial")
                continue
            if not key:
                ignored += 1
                continue
            existing, match_reason = find_customer_match(db, company_id, data)
            if existing and conflict_policy in {"skip_existing", "create_only"}:
                ignored += 1
                continue
            if existing and conflict_policy == "error_on_conflict":
                rows_error += 1
                errors.append(f"Fila {row_index + 2}: conflicto existente para {match_reason or 'cliente'}")
                continue
            try:
                outcome = upsert_customer(
                    db,
                    company_id=company_id,
                    data=data,
                    source="import",
                    actor_id=user.id,
                    customer_id=existing.id if existing else None,
                    conflict_policy=conflict_policy,
                )
            except ValueError as exc:
                rows_error += 1
                errors.append(f"Fila {row_index + 2}: {exc}")
                continue
            if outcome.action == "created":
                created += 1
            elif outcome.action == "updated":
                updated += 1
            else:
                ignored += 1
                continue
        elif entity_type == "customer_knowledge_articles":
            reference = data.get("reference") or ""
            name = data.get("name") or ""
            if not reference and not name:
                rows_error += 1
                errors.append(f"Fila {row_index + 2}: falta articulo/referencia o descripcion")
                continue
            product, match_reason = find_product_match(db, company_id, data)
            if not product:
                rows_error += 1
                errors.append(f"Fila {row_index + 2}: no se encontró producto para {reference or name}")
                continue
            quantity = as_float(data.get("quantity") or "")
            parsed_date = _parse_date_value(data.get("order_date") or "")
            extra_bits = [
                f"Ejercicio: {data.get('exercise')}" if data.get("exercise") else "",
                f"Serie: {data.get('series')}" if data.get("series") else "",
                f"Albarán: {data.get('document_number')}" if data.get("document_number") else "",
                f"Fecha: {data.get('order_date')}" if data.get("order_date") else "",
                f"Precio: {data.get('sale_price')}" if data.get("sale_price") else "",
            ]
            comments = " | ".join(bit for bit in extra_bits if bit)
            existing = db.scalar(
                select(CustomerProductKnowledge).where(
                    CustomerProductKnowledge.company_id == company_id,
                    CustomerProductKnowledge.customer_id == customer.id,
                    CustomerProductKnowledge.product_id == product.id,
                )
            )
            if existing and conflict_policy in {"skip_existing", "create_only"}:
                ignored += 1
                continue
            if existing and conflict_policy == "error_on_conflict":
                rows_error += 1
                errors.append(f"Fila {row_index + 2}: conflicto existente para {match_reason or 'producto'}")
                continue
            try:
                knowledge_service.update_customer_product_knowledge(
                    db,
                    company_id=company_id,
                    customer=customer,
                    product=product,
                    quantity=quantity,
                    unit=product.sale_unit or "",
                    order=None,
                    order_at=parsed_date.to_pydatetime() if parsed_date is not None else None,
                    source_context="pedido_confirmado",
                    customer_alias_used=None,
                    comments=comments or None,
                    is_manual=False,
                    exported_at=None,
                    delivery_note_at=None,
                    force_habitual=False,
                )
            except ValueError as exc:
                rows_error += 1
                errors.append(f"Fila {row_index + 2}: {exc}")
                continue
            if existing:
                updated += 1
            else:
                created += 1
        else:
            key = data.get("reference")
            name = data.get("name")
            if not key and not name:
                ignored += 1
                continue
            existing, match_reason = find_product_match(db, company_id, data)
            if existing and conflict_policy in {"skip_existing", "create_only"}:
                ignored += 1
                continue
            if existing and conflict_policy == "error_on_conflict":
                rows_error += 1
                errors.append(f"Fila {row_index + 2}: conflicto existente para {match_reason or 'producto'}")
                continue
            try:
                outcome = upsert_product(
                    db,
                    company_id=company_id,
                    data=data,
                    source="import",
                    actor_id=user.id,
                    product_id=existing.id if existing else None,
                    conflict_policy=conflict_policy,
                )
            except ValueError as exc:
                rows_error += 1
                errors.append(f"Fila {row_index + 2}: {exc}")
                continue
            if outcome.action == "created":
                created += 1
            elif outcome.action == "updated":
                updated += 1
            else:
                ignored += 1
    if save_template and template_name:
        existing = db.query(ImportMappingTemplate).filter(
            ImportMappingTemplate.company_id == company_id,
            ImportMappingTemplate.entity_type == entity_type,
            ImportMappingTemplate.name == template_name,
        ).one_or_none()
        if existing:
            existing.mapping_json = json.dumps(mapping)
        else:
            db.add(ImportMappingTemplate(company_id=company_id, entity_type=entity_type, name=template_name, mapping_json=json.dumps(mapping)))
    job = ImportJob(
        company_id=company_id,
        user_id=user.id,
        entity_type=entity_type,
        filename=filename,
        rows_total=len(df),
        rows_created=created,
        rows_updated=updated,
        rows_ignored=ignored,
        errors=json.dumps(errors[:50]) if errors else None,
        mapping_used=json.dumps(mapping),
    )
    db.add(job)
    db.commit()
    return job


def import_customers(db: Session, *, company_id: int, filename: str, df: pd.DataFrame):
    from app.db.models import User

    mapping = guess_mapping("customers", [str(column) for column in df.columns])
    validation_user = User(id=0, company_id=company_id, role_id=0, email="system", name="system", password_hash="")
    return confirm_import(db, company_id=company_id, user=validation_user, entity_type="customers", filename=filename, df=df, mapping=mapping, mode="update_existing")


def import_products(db: Session, *, company_id: int, filename: str, df: pd.DataFrame):
    from app.db.models import User

    mapping = guess_mapping("products", [str(column) for column in df.columns])
    validation_user = User(id=0, company_id=company_id, role_id=0, email="system", name="system", password_hash="")
    return confirm_import(db, company_id=company_id, user=validation_user, entity_type="products", filename=filename, df=df, mapping=mapping, mode="update_existing")
