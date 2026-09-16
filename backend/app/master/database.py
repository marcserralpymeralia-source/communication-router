from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.master_database_url.startswith("sqlite") else {}
engine_options = {"connect_args": connect_args, "pool_pre_ping": True}
if settings.is_vercel_pilot:
    engine_options.update(pool_size=1, max_overflow=0, pool_timeout=30, pool_recycle=300)
engine = create_engine(settings.master_database_url, **engine_options)
MasterSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class MasterBase(DeclarativeBase):
    pass


def get_master_db() -> Generator[Session, None, None]:
    db = MasterSessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_master_db() -> None:
    from app.master import models  # noqa: F401

    if settings.app_slug.strip().lower() == "kibak":
        from app.migrations.kibak_baseline import KIBAK_MASTER_TABLES

        tables = [MasterBase.metadata.tables[name] for name in KIBAK_MASTER_TABLES]
        MasterBase.metadata.create_all(bind=engine, tables=tables)
        return
    MasterBase.metadata.create_all(bind=engine)
