from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.master.database import MasterSessionLocal, init_master_db
from app.master.models import EmailSyncState, MasterTenantDatabase
from app.workers.jobs_worker import start_job_worker
from app.workers.email_worker import start_email_sync_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    settings = get_settings()
    master_db = None
    master_db_ready = True
    try:
        init_master_db()
        master_db = MasterSessionLocal()
        tenants = master_db.scalars(
            select(MasterTenantDatabase).where(
                MasterTenantDatabase.is_active.is_(True),
                MasterTenantDatabase.database_url.is_not(None),
            )
        ).all()
        if settings.app_slug.strip().lower() != "kibak":
            for tenant in tenants:
                state = master_db.scalar(
                    select(EmailSyncState).where(
                        EmailSyncState.company_id == tenant.company_id,
                        EmailSyncState.channel_key == "email",
                    )
                )
                if not state:
                    master_db.add(
                        EmailSyncState(
                            company_id=tenant.company_id,
                            channel_key="email",
                            enabled=True,
                            frequency_seconds=60,
                            status="idle",
                            next_run_at=datetime.now(timezone.utc),
                        )
                    )
                    master_db.commit()
        if settings.enable_legacy_sync:
            logger.info("Legacy sync enabled explicitly")
    except SQLAlchemyError:
        master_db_ready = False
        logger.exception("No se pudo inicializar la base master en el arranque; la app arrancará en modo degradado")
    finally:
        if master_db is not None:
            master_db.close()
    running_on_vercel = os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))
    if running_on_vercel or not master_db_ready:
        logger.info("Vercel runtime detected: workers disabled in this process")
    else:
        start_email_sync_worker()
        start_job_worker()
    yield
