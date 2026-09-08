"""Generate deterministic KIBAK communications for local performance checks."""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.models import Company, Communication, Department, Mailbox, RoutingAction, RoutingDecision, utcnow


ALLOWED_COUNTS = (100, 500, 1000)
RESET_CONFIRMATION = "KIBAK_LOAD_RESET"


def _guard() -> None:
    settings = get_settings()
    if settings.app_slug.strip().lower() != "kibak":
        raise RuntimeError("El generador solo puede ejecutarse con APP_SLUG=kibak.")
    if settings.environment == "production":
        raise RuntimeError("El generador esta bloqueado en production.")


def _demo_company(db: Session, company_id: int):
    company = db.get(Company, company_id)
    if company is None or not any(token in (company.name or "").lower() for token in ("demo", "test", "kibak")):
        raise RuntimeError("El company_id no identifica una empresa demo KIBAK.")
    return company


def _get_departments(db: Session, company_id: int) -> list[Department]:
    names = ("Administracion", "Comercial", "Soporte")
    departments: list[Department] = []
    for index, name in enumerate(names):
        department = db.scalar(select(Department).where(Department.company_id == company_id, Department.name == name))
        if department is None:
            department = Department(
                company_id=company_id,
                name=name,
                description="Departamento demo para pruebas de volumen KIBAK.",
                destination_email=f"{name.lower()}@example.test",
                active=True,
            )
            db.add(department)
            db.flush()
        departments.append(department)
    return departments


def generate(db: Session, *, company_id: int, count: int, batch: str) -> dict[str, int | str]:
    company = _demo_company(db, company_id)
    mailbox = db.scalar(select(Mailbox).where(Mailbox.company_id == company.id).order_by(Mailbox.id))
    if mailbox is None:
        mailbox = Mailbox(
            company_id=company.id,
            name="Buzon demo KIBAK",
            email_address="demo-inbox@example.test",
            provider="synthetic",
            enabled=True,
            auto_sync_enabled=False,
        )
        db.add(mailbox)
        db.flush()
    departments = _get_departments(db, company.id)
    created = 0
    skipped = 0
    for index in range(1, count + 1):
        external_id = f"kibak-load:{batch}:{index}"
        communication = db.scalar(
            select(Communication).where(
                Communication.company_id == company.id,
                Communication.mailbox_id == mailbox.id,
                Communication.provider == "synthetic",
                Communication.external_message_id == external_id,
            )
        )
        if communication is not None:
            skipped += 1
            continue
        department = departments[(index - 1) % len(departments)]
        status = "pending_review" if index % 3 == 0 else "routed"
        communication = Communication(
            company_id=company.id,
            mailbox_id=mailbox.id,
            provider="synthetic",
            external_message_id=external_id,
            sender_email=f"sender-{index:04d}@example.test",
            sender_name=f"Remitente {index:04d}",
            subject=f"Consulta sintetica {index:04d}",
            body_text="Comunicacion sintetica para pruebas de rendimiento y recuperacion.",
            received_at=utcnow() - timedelta(minutes=index),
            processing_status="processed",
            routing_status=status,
            metadata_json='{"source":"kibak-load","external_calls":false}',
        )
        db.add(communication)
        db.flush()
        decision = RoutingDecision(
            company_id=company.id,
            communication_id=communication.id,
            department_id=department.id,
            category="synthetic",
            confidence=0.96 if index % 3 else 0.62,
            requires_review=index % 3 == 0,
            reason="Caso sintetico determinista para pruebas.",
            status=status,
            source="synthetic",
            analysis_number=1,
        )
        db.add(decision)
        db.flush()
        if index % 10 == 0:
            db.add(
                RoutingAction(
                    company_id=company.id,
                    communication_id=communication.id,
                    routing_decision_id=decision.id,
                    department_id=department.id,
                    action_type="forward",
                    source="simulation",
                    destination_email=department.destination_email,
                    status="simulated",
                    idempotency_key=f"kibak-load-forward:{batch}:{index}",
                )
            )
        created += 1
    db.commit()
    return {"company_id": company.id, "batch": batch, "created": created, "skipped": skipped}


def reset(db: Session, *, company_id: int, batch: str) -> int:
    company = _demo_company(db, company_id)
    communications = select(Communication.id).where(
        Communication.company_id == company.id,
        Communication.provider == "synthetic",
        Communication.external_message_id.like(f"kibak-load:{batch}:%"),
    )
    ids = list(db.scalars(communications))
    if ids:
        db.execute(delete(RoutingAction).where(RoutingAction.company_id == company.id, RoutingAction.communication_id.in_(ids)))
        db.execute(delete(RoutingDecision).where(RoutingDecision.company_id == company.id, RoutingDecision.communication_id.in_(ids)))
        db.execute(delete(Communication).where(Communication.company_id == company.id, Communication.id.in_(ids)))
    db.commit()
    return len(ids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Carga sintetica segura para KIBAK")
    parser.add_argument("--count", type=int, choices=ALLOWED_COUNTS, default=100)
    parser.add_argument("--company-id", type=int, default=1)
    parser.add_argument("--batch", default="default")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--confirm-reset", default="")
    args = parser.parse_args(argv)
    _guard()
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as db:
        if args.reset:
            if args.confirm_reset != RESET_CONFIRMATION:
                raise SystemExit(f"Reset requiere --confirm-reset {RESET_CONFIRMATION}.")
            print({"deleted": reset(db, company_id=args.company_id, batch=args.batch)})
        else:
            print(generate(db, company_id=args.company_id, count=args.count, batch=args.batch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
