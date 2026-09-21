"""Plan or apply reviewed routing Knowledge proposals for one KIBAK tenant.

Dry-run is the default. Applying requires an explicit confirmation token and
never creates missing departments or deletes existing Knowledge.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.master.database import MasterSessionLocal  # noqa: E402
from app.master.models import MasterCompany, MasterTenantDatabase  # noqa: E402
from app.routing.learning import apply_knowledge_proposals, plan_knowledge_import  # noqa: E402
from app.tenancy.database import tenant_db_session  # noqa: E402


CONFIRMATION = "KIBAK_ROUTING_KNOWLEDGE_IMPORT"


def _resolve_tenant(master_db, slug: str):  # noqa: ANN001
    tenant = master_db.scalar(
        select(MasterTenantDatabase)
        .join(MasterCompany)
        .options(selectinload(MasterTenantDatabase.company))
        .where(MasterCompany.slug == slug, MasterTenantDatabase.is_active.is_(True))
    )
    if tenant is None or not tenant.database_url:
        raise ValueError(f"Tenant KIBAK no encontrado: {slug}")
    return tenant


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposals", type=Path, required=True)
    parser.add_argument("--company-slug", default="kibak-pilot")
    parser.add_argument("--apply", action="store_true", help="Apply reviewed proposals instead of printing a dry-run")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    settings = get_settings()
    if settings.app_slug.strip().lower() != "kibak":
        raise SystemExit("APP_SLUG must be kibak")
    if settings.app_env.strip().lower() == "production":
        raise SystemExit("Knowledge import is blocked in production")
    if args.apply and args.confirm != CONFIRMATION:
        raise SystemExit(f"Applying requires --confirm {CONFIRMATION}")
    proposals = json.loads(args.proposals.read_text(encoding="utf-8"))

    master_db = MasterSessionLocal()
    tenant_db = None
    try:
        tenant = _resolve_tenant(master_db, args.company_slug)
        tenant_db = tenant_db_session(tenant.database_url)()
        plan = plan_knowledge_import(tenant_db, tenant.company_id, proposals)
        if not args.apply:
            print(json.dumps({"mode": "dry-run", "company_id": tenant.company_id, "operations": plan}, ensure_ascii=False, indent=2))
            return 0
        counters = apply_knowledge_proposals(tenant_db, tenant.company_id, proposals)
        tenant_db.commit()
        print(json.dumps({"mode": "apply", "company_id": tenant.company_id, "summary": counters}, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        if tenant_db is not None:
            tenant_db.rollback()
        raise
    finally:
        if tenant_db is not None:
            tenant_db.close()
        master_db.close()


if __name__ == "__main__":
    raise SystemExit(main())
