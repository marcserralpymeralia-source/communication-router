from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.core.encryption import decrypt_secret, encrypt_secret
from app.master.database import MasterBase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EncryptedDatabaseUrl(TypeDecorator[str]):
    """Store tenant URLs encrypted while exposing plaintext only in Python."""

    impl = Text
    cache_ok = True

    @staticmethod
    def _is_local_url(value: str) -> bool:
        # Local SQLite fixtures contain a filesystem path, not connection secrets.
        return value.lower().startswith("sqlite")

    def process_bind_param(self, value: str | None, dialect) -> str | None:  # noqa: ARG002
        if value and self._is_local_url(value):
            return value
        return encrypt_secret(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:  # noqa: ARG002
        if not value:
            return value
        if self._is_local_url(value):
            return value
        decrypted = decrypt_secret(value)
        if decrypted is not None:
            return decrypted
        # Existing pre-baseline rows may still contain a plaintext URL.
        if not value.startswith("gAAAA"):
            return value
        raise ValueError("Cannot decrypt tenant database URL; check ENCRYPTION_KEY")


class MasterCompany(MasterBase):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    default_language: Mapped[str] = mapped_column(String(20), default="es")
    default_timezone: Mapped[str] = mapped_column(String(80), default="Europe/Madrid")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    memberships: Mapped[list["CompanyMembership"]] = relationship(back_populates="company")
    tenant_databases: Mapped[list["MasterTenantDatabase"]] = relationship(back_populates="company")


class MasterUser(MasterBase):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    memberships: Mapped[list["CompanyMembership"]] = relationship(back_populates="user")


class CompanyMembership(MasterBase):
    __tablename__ = "memberships"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    role_key: Mapped[str] = mapped_column(String(80), default="Administrador")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[MasterUser] = relationship(back_populates="memberships")
    company: Mapped[MasterCompany] = relationship(back_populates="memberships")

    __table_args__ = (UniqueConstraint("user_id", "company_id"),)


class MasterTenantDatabase(MasterBase):
    __tablename__ = "tenant_databases"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    database_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    database_url: Mapped[str] = mapped_column(EncryptedDatabaseUrl)
    database_type: Mapped[str] = mapped_column(String(30), default="sqlite")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_status: Mapped[str] = mapped_column(String(30), default="unknown")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[MasterCompany] = relationship(back_populates="tenant_databases")

    def get_database_url(self) -> str | None:
        """Return the usable URL; the mapped column is encrypted at rest."""
        return self.database_url


class EmailSyncState(MasterBase):
    __tablename__ = "email_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    channel_key: Mapped[str] = mapped_column(String(80), default="email", index=True)
    mailbox: Mapped[str | None] = mapped_column(String(255))
    uidvalidity: Mapped[str | None] = mapped_column(String(120))
    source_provider: Mapped[str | None] = mapped_column(String(50))
    source_host: Mapped[str | None] = mapped_column(String(255))
    source_username: Mapped[str | None] = mapped_column(String(255))
    source_connected_email: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    frequency_seconds: Mapped[int] = mapped_column(Integer, default=60)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(120))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(50), default="idle")
    sync_status: Mapped[str] = mapped_column(String(50), default="idle")
    listener_status: Mapped[str] = mapped_column(String(50), default="inactive")
    listener_owner: Mapped[str | None] = mapped_column(String(120))
    listener_last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listener_last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listener_last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listener_last_error_message: Mapped[str | None] = mapped_column(Text)
    lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lock_owner: Mapped[str | None] = mapped_column(String(120))
    last_seen_uid: Mapped[str | None] = mapped_column(String(120))
    last_checkpoint_uid: Mapped[str | None] = mapped_column(String(120))
    backfill_status: Mapped[str] = mapped_column(String(50), default="idle")
    backfill_total: Mapped[int] = mapped_column(Integer, default=0)
    backfill_processed: Mapped[int] = mapped_column(Integer, default=0)
    backfill_created: Mapped[int] = mapped_column(Integer, default=0)
    backfill_duplicates: Mapped[int] = mapped_column(Integer, default=0)
    backfill_errors: Mapped[int] = mapped_column(Integer, default=0)
    backfill_last_uid: Mapped[str | None] = mapped_column(String(120))
    backfill_checkpoint_json: Mapped[str | None] = mapped_column(Text)
    backfill_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_last_checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[MasterCompany] = relationship()

    __table_args__ = (UniqueConstraint("company_id", "channel_key"),)


class MailboxSyncState(MasterBase):
    __tablename__ = "mailbox_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    mailbox_id: Mapped[int] = mapped_column(Integer, index=True)
    uidvalidity: Mapped[str | None] = mapped_column(String(120))
    source_provider: Mapped[str | None] = mapped_column(String(50))
    source_host: Mapped[str | None] = mapped_column(String(255))
    source_username: Mapped[str | None] = mapped_column(String(255))
    source_connected_email: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    frequency_seconds: Mapped[int] = mapped_column(Integer, default=60)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(120))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(50), default="idle")
    sync_status: Mapped[str] = mapped_column(String(50), default="idle")
    listener_status: Mapped[str] = mapped_column(String(50), default="inactive")
    listener_owner: Mapped[str | None] = mapped_column(String(120))
    listener_last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listener_last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listener_last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listener_last_error_message: Mapped[str | None] = mapped_column(Text)
    lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lock_owner: Mapped[str | None] = mapped_column(String(120))
    last_seen_uid: Mapped[str | None] = mapped_column(String(120))
    last_checkpoint_uid: Mapped[str | None] = mapped_column(String(120))
    backfill_status: Mapped[str] = mapped_column(String(50), default="idle")
    backfill_total: Mapped[int] = mapped_column(Integer, default=0)
    backfill_processed: Mapped[int] = mapped_column(Integer, default=0)
    backfill_created: Mapped[int] = mapped_column(Integer, default=0)
    backfill_duplicates: Mapped[int] = mapped_column(Integer, default=0)
    backfill_errors: Mapped[int] = mapped_column(Integer, default=0)
    backfill_last_uid: Mapped[str | None] = mapped_column(String(120))
    backfill_checkpoint_json: Mapped[str | None] = mapped_column(Text)
    backfill_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_last_checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[MasterCompany] = relationship()

    __table_args__ = (UniqueConstraint("company_id", "mailbox_id"),)


class MasterSchemaMigration(MasterBase):
    __tablename__ = "schema_migrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(80), default="0")
    name: Mapped[str] = mapped_column(String(180), default="unregistered")
    checksum: Mapped[str | None] = mapped_column(String(120))
    execution_ms: Mapped[int] = mapped_column(Integer, default=0)
    application_version: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="missing")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
