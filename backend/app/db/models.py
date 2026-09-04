import re
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.agent.model_catalog import DEFAULT_OPENAI_MODEL
from app.db.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_DESTINATION_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_destination_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if not _DESTINATION_EMAIL_RE.fullmatch(normalized):
        raise ValueError("destination_email must be a valid email address")
    return normalized


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="Anchi Demo")
    legal_name: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    plan: Mapped[str | None] = mapped_column(String(80))
    logo_url: Mapped[str | None] = mapped_column(String(500))
    tax_id: Mapped[str | None] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(80))
    web: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(500))
    country: Mapped[str | None] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(10), default="EUR")
    notification_email: Mapped[str | None] = mapped_column(String(255))
    responsible_contact: Mapped[str | None] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(20), default="es")
    default_language: Mapped[str] = mapped_column(String(20), default="es")
    timezone: Mapped[str] = mapped_column(String(80), default="Europe/Madrid")
    date_format: Mapped[str] = mapped_column(String(30), default="%d/%m/%Y")
    decimal_separator: Mapped[str] = mapped_column(String(5), default=",")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    permissions: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (UniqueConstraint("company_id", "name"),)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    role: Mapped[Role] = relationship()

    __table_args__ = (UniqueConstraint("company_id", "id"),)


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    key: Mapped[str] = mapped_column(String(150))
    value: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("company_id", "key"),)


class BrandingSettings(Base):
    __tablename__ = "branding_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), unique=True, index=True, nullable=True)
    app_name: Mapped[str] = mapped_column(String(200), default="Anchi")
    company_name: Mapped[str] = mapped_column(String(200), default="Anchi Demo")
    primary_claim: Mapped[str] = mapped_column(String(255), default="Gestion inteligente de pedidos")
    secondary_claim: Mapped[str] = mapped_column(String(255), default="")
    short_description: Mapped[str] = mapped_column(Text, default="")
    logo_url: Mapped[str | None] = mapped_column(String(500))
    dark_logo_url: Mapped[str | None] = mapped_column(String(500))
    favicon_url: Mapped[str | None] = mapped_column(String(500))
    show_logo_sidebar: Mapped[bool] = mapped_column(Boolean, default=True)
    show_app_name_sidebar: Mapped[bool] = mapped_column(Boolean, default=True)
    show_claim_sidebar: Mapped[bool] = mapped_column(Boolean, default=True)
    show_claim_login: Mapped[bool] = mapped_column(Boolean, default=True)
    theme_json: Mapped[str] = mapped_column(Text, default="{}")
    microcopy_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmailSettings(Base):
    __tablename__ = "email_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), unique=True)
    provider: Mapped[str] = mapped_column(String(50), default="imap")
    connection_method: Mapped[str] = mapped_column(String(50), default="password")
    client_id: Mapped[str | None] = mapped_column(String(255))
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str | None] = mapped_column(String(255))
    redirect_uri: Mapped[str | None] = mapped_column(String(500))
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    connected_email: Mapped[str | None] = mapped_column(String(255))
    imap_host: Mapped[str | None] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    imap_security: Mapped[str] = mapped_column(String(30), default="ssl_tls")
    imap_username: Mapped[str | None] = mapped_column(String(255))
    imap_password_encrypted: Mapped[str | None] = mapped_column(Text)
    test_read_limit: Mapped[int] = mapped_column(Integer, default=10)
    oauth_scopes: Mapped[str | None] = mapped_column(Text)
    mailbox: Mapped[str | None] = mapped_column(String(255))
    inbox_folder: Mapped[str] = mapped_column(String(100), default="INBOX")
    processed_folder: Mapped[str | None] = mapped_column(String(100))
    error_folder: Mapped[str | None] = mapped_column(String(100))
    no_order_folder: Mapped[str | None] = mapped_column(String(100))
    doubtful_folder: Mapped[str | None] = mapped_column(String(100))
    read_limit: Mapped[int] = mapped_column(Integer, default=25)
    auto_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    read_unread_only: Mapped[bool] = mapped_column(Boolean, default=True)
    read_from_date: Mapped[str | None] = mapped_column(String(50))
    initial_history_mode: Mapped[str] = mapped_column(String(30), default="new")
    initial_history_limit: Mapped[int] = mapped_column(Integer, default=50)
    mark_as_read_after_import: Mapped[bool] = mapped_column(Boolean, default=False)
    move_after_processing: Mapped[bool] = mapped_column(Boolean, default=False)
    post_process_action: Mapped[str] = mapped_column(String(50), default="mark_read")
    polling_frequency_minutes: Mapped[int] = mapped_column(Integer, default=1)
    smtp_provider: Mapped[str] = mapped_column(String(50), default="smtp")
    smtp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    smtp_host: Mapped[str | None] = mapped_column(String(255))
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_security: Mapped[str] = mapped_column(String(30), default="starttls")
    smtp_username: Mapped[str | None] = mapped_column(String(255))
    smtp_password_encrypted: Mapped[str | None] = mapped_column(Text)
    from_email: Mapped[str | None] = mapped_column(String(255))
    from_name: Mapped[str | None] = mapped_column(String(255))
    reply_to: Mapped[str | None] = mapped_column(String(255))
    default_cc: Mapped[str | None] = mapped_column(Text)
    default_bcc: Mapped[str | None] = mapped_column(Text)
    save_internal_copy: Mapped[bool] = mapped_column(Boolean, default=True)
    preserve_thread_headers: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_process_on_fetch: Mapped[bool] = mapped_column(Boolean, default=False)
    process_only_with_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    process_only_with_pdf: Mapped[bool] = mapped_column(Boolean, default=False)
    process_without_attachments: Mapped[bool] = mapped_column(Boolean, default=True)
    process_read_emails: Mapped[bool] = mapped_column(Boolean, default=False)
    avoid_duplicates_by_message_id: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_reprocess: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_create_order_if_detected: Mapped[bool] = mapped_column(Boolean, default=True)
    always_human_review: Mapped[bool] = mapped_column(Boolean, default=True)
    mark_doubtful_below_threshold: Mapped[bool] = mapped_column(Boolean, default=True)
    mark_no_order_if_detected: Mapped[bool] = mapped_column(Boolean, default=True)
    action_order_detected: Mapped[str] = mapped_column(String(80), default="move_processed")
    action_no_order: Mapped[str] = mapped_column(String(80), default="move_no_order")
    action_doubtful: Mapped[str] = mapped_column(String(80), default="move_doubtful")
    action_error: Mapped[str] = mapped_column(String(80), default="move_error")
    minimum_score_auto_order: Mapped[int] = mapped_column(Integer, default=90)
    visible_states: Mapped[str] = mapped_column(Text, default="pending,processing,pedido,no_pedido,dudoso,error_processing,pending_reprocess,responded,closed")
    default_filter: Mapped[str] = mapped_column(String(80), default="all")
    default_date_range: Mapped[str] = mapped_column(String(80), default="today")
    default_page_size: Mapped[int] = mapped_column(Integer, default=25)
    default_sort: Mapped[str] = mapped_column(String(80), default="date_desc")
    show_summary_cards: Mapped[bool] = mapped_column(Boolean, default=True)
    show_score_column: Mapped[bool] = mapped_column(Boolean, default=True)
    show_customer_column: Mapped[bool] = mapped_column(Boolean, default=True)
    show_attachments_column: Mapped[bool] = mapped_column(Boolean, default=True)
    show_order_column: Mapped[bool] = mapped_column(Boolean, default=True)
    show_reply_button: Mapped[bool] = mapped_column(Boolean, default=True)
    show_process_button: Mapped[bool] = mapped_column(Boolean, default=True)
    signature_text: Mapped[str] = mapped_column(Text, default="Equipo de pedidos")
    signature_html: Mapped[str | None] = mapped_column(Text)
    use_signature: Mapped[bool] = mapped_column(Boolean, default=True)
    include_logo_in_signature: Mapped[bool] = mapped_column(Boolean, default=False)
    legal_footer: Mapped[str | None] = mapped_column(Text)
    last_imap_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_imap_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_imap_test_message: Mapped[str | None] = mapped_column(Text)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_sync_message: Mapped[str | None] = mapped_column(Text)
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    last_sync_new: Mapped[int] = mapped_column(Integer, default=0)
    last_sync_duplicates: Mapped[int] = mapped_column(Integer, default=0)
    last_smtp_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_smtp_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_smtp_test_message: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Mailbox(Base):
    __tablename__ = "mailboxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(150), default="Buzón de correo")
    email_address: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(50), default="imap")
    connection_method: Mapped[str] = mapped_column(String(50), default="password")
    client_id: Mapped[str | None] = mapped_column(String(255))
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str | None] = mapped_column(String(255))
    redirect_uri: Mapped[str | None] = mapped_column(String(500))
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    connected_email: Mapped[str | None] = mapped_column(String(255))
    imap_host: Mapped[str | None] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    imap_security: Mapped[str] = mapped_column(String(30), default="ssl_tls")
    imap_username: Mapped[str | None] = mapped_column(String(255))
    imap_password_encrypted: Mapped[str | None] = mapped_column(Text)
    mailbox: Mapped[str | None] = mapped_column(String(255))
    inbox_folder: Mapped[str] = mapped_column(String(100), default="INBOX")
    processed_folder: Mapped[str | None] = mapped_column(String(100))
    error_folder: Mapped[str | None] = mapped_column(String(100))
    no_order_folder: Mapped[str | None] = mapped_column(String(100))
    doubtful_folder: Mapped[str | None] = mapped_column(String(100))
    read_limit: Mapped[int] = mapped_column(Integer, default=25)
    polling_frequency_minutes: Mapped[int] = mapped_column(Integer, default=5)
    auto_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    read_unread_only: Mapped[bool] = mapped_column(Boolean, default=True)
    read_from_date: Mapped[str | None] = mapped_column(String(50))
    mark_as_read_after_import: Mapped[bool] = mapped_column(Boolean, default=False)
    move_after_processing: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_process_on_fetch: Mapped[bool] = mapped_column(Boolean, default=False)
    smtp_provider: Mapped[str] = mapped_column(String(50), default="smtp")
    smtp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    smtp_host: Mapped[str | None] = mapped_column(String(255))
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_security: Mapped[str] = mapped_column(String(30), default="starttls")
    smtp_username: Mapped[str | None] = mapped_column(String(255))
    smtp_password_encrypted: Mapped[str | None] = mapped_column(Text)
    from_email: Mapped[str | None] = mapped_column(String(255))
    from_name: Mapped[str | None] = mapped_column(String(255))
    reply_to: Mapped[str | None] = mapped_column(String(255))
    default_cc: Mapped[str | None] = mapped_column(Text)
    default_bcc: Mapped[str | None] = mapped_column(Text)
    save_internal_copy: Mapped[bool] = mapped_column(Boolean, default=True)
    preserve_thread_headers: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_imap_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_imap_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_imap_test_message: Mapped[str | None] = mapped_column(Text)
    last_smtp_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_smtp_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_smtp_test_message: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("company_id", "email_address"),)


class Communication(Base):
    __tablename__ = "communications"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    mailbox_id: Mapped[int] = mapped_column(ForeignKey("mailboxes.id"), index=True)
    external_message_id: Mapped[str] = mapped_column(String(255))
    thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    provider: Mapped[str] = mapped_column(String(50), default="imap")
    sender_email: Mapped[str | None] = mapped_column(String(255), index=True)
    sender_name: Mapped[str | None] = mapped_column(String(255))
    to_recipients: Mapped[str | None] = mapped_column(Text)
    cc_recipients: Mapped[str | None] = mapped_column(Text)
    bcc_recipients: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(String(500))
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    processing_status: Mapped[str] = mapped_column(String(50), default="received", index=True)
    routing_status: Mapped[str] = mapped_column(String(50), default="unclassified", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    attachments: Mapped[list["CommunicationAttachment"]] = relationship(
        back_populates="communication", cascade="all, delete-orphan"
    )
    routing_decisions: Mapped[list["RoutingDecision"]] = relationship(
        back_populates="communication", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("company_id", "mailbox_id", "provider", "external_message_id"),
        UniqueConstraint("company_id", "id"),
    )


class CommunicationAttachment(Base):
    __tablename__ = "communication_attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    communication_id: Mapped[int] = mapped_column(ForeignKey("communications.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_ref: Mapped[str | None] = mapped_column(String(500))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extraction_status: Mapped[str] = mapped_column(String(80), default="pending")
    extraction_error: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str | None] = mapped_column(String(64), index=True)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    communication: Mapped[Communication] = relationship(back_populates="attachments")


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str | None] = mapped_column(Text)
    destination_email: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    knowledge_items: Mapped[list["DepartmentKnowledge"]] = relationship(
        back_populates="department", passive_deletes=True
    )
    members: Mapped[list["DepartmentMember"]] = relationship(
        back_populates="department", passive_deletes=True
    )
    raci_assignments: Mapped[list["RaciAssignment"]] = relationship(
        back_populates="department", passive_deletes=True
    )

    __table_args__ = (UniqueConstraint("company_id", "name"), UniqueConstraint("company_id", "id"))

    @validates("destination_email")
    def validate_destination_email(self, _key: str, value: str | None) -> str | None:
        return _normalize_destination_email(value)


class DepartmentKnowledge(Base):
    __tablename__ = "department_knowledge"

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id", ondelete="RESTRICT"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    knowledge_type: Mapped[str] = mapped_column(String(30), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    department: Mapped[Department] = relationship(back_populates="knowledge_items")

    __table_args__ = (
        CheckConstraint(
            "knowledge_type IN ('responsibility', 'exclusion', 'example', 'exception', 'guideline')",
            name="ck_department_knowledge_type",
        ),
        UniqueConstraint("department_id", "title", "knowledge_type"),
    )


class DepartmentMember(Base):
    __tablename__ = "department_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    department_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    role: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    department: Mapped[Department] = relationship(back_populates="members")
    user: Mapped[User] = relationship(overlaps="department,members")

    __table_args__ = (
        ForeignKeyConstraint(
            ("company_id", "department_id"),
            ("departments.company_id", "departments.id"),
            name="fk_department_member_department_company",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("company_id", "user_id"),
            ("users.company_id", "users.id"),
            name="fk_department_member_user_company",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("company_id", "department_id", "user_id"),
    )


class RaciAssignment(Base):
    __tablename__ = "raci_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    department_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    scope: Mapped[str] = mapped_column(String(150), default="department")
    raci_role: Mapped[str] = mapped_column(String(15), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    department: Mapped[Department] = relationship(
        back_populates="raci_assignments", foreign_keys=[company_id, department_id]
    )
    user: Mapped[User | None] = relationship(
        foreign_keys=[company_id, user_id], overlaps="department,raci_assignments"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ("company_id", "department_id"),
            ("departments.company_id", "departments.id"),
            name="fk_raci_department_company",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("company_id", "user_id"),
            ("users.company_id", "users.id"),
            name="fk_raci_user_company",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "raci_role IN ('responsible', 'accountable', 'consulted', 'informed')",
            name="ck_raci_assignment_role",
        ),
        UniqueConstraint("company_id", "department_id", "user_id", "scope", "raci_role"),
    )

    __table_args__ += (
        Index(
            "uq_raci_assignments_null_user",
            "company_id",
            "department_id",
            "scope",
            "raci_role",
            unique=True,
            sqlite_where=text("user_id IS NULL"),
            postgresql_where=text("user_id IS NULL"),
        ),
    )


class RoutingDecision(Base):
    __tablename__ = "routing_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    communication_id: Mapped[int] = mapped_column(Integer, index=True)
    department_id: Mapped[int | None] = mapped_column(Integer, index=True)
    alternative_department_id: Mapped[int | None] = mapped_column(Integer, index=True)
    final_department_id: Mapped[int | None] = mapped_column(Integer, index=True)
    category: Mapped[str] = mapped_column(String(100))
    final_category: Mapped[str | None] = mapped_column(String(100))
    confidence: Mapped[float] = mapped_column(Float)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str] = mapped_column(Text)
    ambiguity_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="pending_review", index=True)
    source: Mapped[str] = mapped_column(String(30), default="agent")
    analysis_number: Mapped[int] = mapped_column(Integer, default=1)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    communication: Mapped[Communication] = relationship(back_populates="routing_decisions")

    __table_args__ = (
        ForeignKeyConstraint(
            ("company_id", "communication_id"),
            ("communications.company_id", "communications.id"),
            name="fk_routing_decision_communication_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("company_id", "department_id"),
            ("departments.company_id", "departments.id"),
            name="fk_routing_decision_department_company",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("company_id", "alternative_department_id"),
            ("departments.company_id", "departments.id"),
            name="fk_routing_decision_alternative_department_company",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("company_id", "final_department_id"),
            ("departments.company_id", "departments.id"),
            name="fk_routing_decision_final_department_company",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("company_id", "reviewed_by_user_id"),
            ("users.company_id", "users.id"),
            name="fk_routing_decision_reviewer_company",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("company_id", "communication_id", "analysis_number"),
    )


class RoutingCorrection(Base):
    __tablename__ = "routing_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    routing_decision_id: Mapped[int] = mapped_column(Integer, index=True)
    communication_id: Mapped[int] = mapped_column(Integer, index=True)
    original_department_id: Mapped[int | None] = mapped_column(Integer)
    corrected_department_id: Mapped[int] = mapped_column(Integer)
    original_category: Mapped[str | None] = mapped_column(String(100))
    corrected_category: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    corrected_by_user_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ("company_id", "routing_decision_id"),
            ("routing_decisions.company_id", "routing_decisions.id"),
            name="fk_routing_correction_decision_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("company_id", "communication_id"),
            ("communications.company_id", "communications.id"),
            name="fk_routing_correction_communication_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("company_id", "corrected_department_id"),
            ("departments.company_id", "departments.id"),
            name="fk_routing_correction_department_company",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("company_id", "corrected_by_user_id"),
            ("users.company_id", "users.id"),
            name="fk_routing_correction_user_company",
            ondelete="RESTRICT",
        ),
    )


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    key: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(200))
    template_type: Mapped[str] = mapped_column(String(80), default="other")
    subject_template: Mapped[str] = mapped_column(String(500), default="Re: {asunto_original}")
    body_template: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default_for_type: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (UniqueConstraint("company_id", "key"),)


class LLMSettings(Base):
    __tablename__ = "llm_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), unique=True)
    agent_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    agent_mode: Mapped[str] = mapped_column(String(80), default="semiautomatico")
    safety_level: Mapped[str] = mapped_column(String(50), default="equilibrado")
    provider: Mapped[str] = mapped_column(String(50), default="openai")
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(String(500))
    classification_model: Mapped[str] = mapped_column(String(100), default=DEFAULT_OPENAI_MODEL)
    extraction_model: Mapped[str] = mapped_column(String(100), default=DEFAULT_OPENAI_MODEL)
    validation_model: Mapped[str] = mapped_column(String(100), default=DEFAULT_OPENAI_MODEL)
    use_same_model_for_all: Mapped[bool] = mapped_column(Boolean, default=True)
    can_read_email: Mapped[bool] = mapped_column(Boolean, default=True)
    can_extract_pdf: Mapped[bool] = mapped_column(Boolean, default=True)
    can_classify_email: Mapped[bool] = mapped_column(Boolean, default=True)
    can_extract_order: Mapped[bool] = mapped_column(Boolean, default=True)
    can_suggest_customer: Mapped[bool] = mapped_column(Boolean, default=True)
    can_suggest_products: Mapped[bool] = mapped_column(Boolean, default=True)
    can_calculate_score: Mapped[bool] = mapped_column(Boolean, default=True)
    can_create_pending_order: Mapped[bool] = mapped_column(Boolean, default=True)
    can_mark_no_order: Mapped[bool] = mapped_column(Boolean, default=True)
    can_reply_customer: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_auto_confirm: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_auto_export: Mapped[bool] = mapped_column(Boolean, default=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.1)
    max_tokens: Mapped[int] = mapped_column(Integer, default=4000)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    retries: Mapped[int] = mapped_column(Integer, default=2)
    daily_cost_limit: Mapped[float] = mapped_column(Float, default=0)
    batch_limit: Mapped[int] = mapped_column(Integer, default=25)
    detailed_llm_logs: Mapped[bool] = mapped_column(Boolean, default=False)
    store_llm_payloads: Mapped[bool] = mapped_column(Boolean, default=False)
    anonymize_llm_logs: Mapped[bool] = mapped_column(Boolean, default=True)
    debug_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    organization_id: Mapped[str | None] = mapped_column(String(255))
    project_id: Mapped[str | None] = mapped_column(String(255))
    azure_deployment_name: Mapped[str | None] = mapped_column(String(255))
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_message: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_response_ms: Mapped[int | None] = mapped_column(Integer)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FTPSettings(Base):
    __tablename__ = "ftp_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), unique=True)
    connection_type: Mapped[str] = mapped_column(String(20), default="ftps_explicit")
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=21)
    username: Mapped[str | None] = mapped_column(String(255))
    password_encrypted: Mapped[str | None] = mapped_column(Text)
    private_key_encrypted: Mapped[str | None] = mapped_column(Text)
    destination_path: Mapped[str] = mapped_column(String(500), default="/")
    passive_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    overwrite_files: Mapped[bool] = mapped_column(Boolean, default=False)
    retries: Mapped[int] = mapped_column(Integer, default=2)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)


class ExportSettings(Base):
    __tablename__ = "export_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), unique=True)
    file_type: Mapped[str] = mapped_column(String(30), default="csv")
    csv_separator: Mapped[str] = mapped_column(String(10), default=";")
    encoding: Mapped[str] = mapped_column(String(50), default="utf-8")
    include_header: Mapped[bool] = mapped_column(Boolean, default=True)
    date_format: Mapped[str] = mapped_column(String(30), default="%Y-%m-%d")
    decimal_separator: Mapped[str] = mapped_column(String(5), default=",")
    filename_template: Mapped[str] = mapped_column(String(300), default="PEDIDO_{codigo_cliente}_{fecha}_{id_pedido}.csv")
    header_fields: Mapped[str] = mapped_column(Text, default="order_id,customer_code,order_date")
    line_fields: Mapped[str] = mapped_column(Text, default="reference,quantity,unit,description")


class ScoringSettings(Base):
    __tablename__ = "scoring_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), unique=True)
    safe_threshold: Mapped[int] = mapped_column(Integer, default=90)
    review_threshold: Mapped[int] = mapped_column(Integer, default=75)
    doubtful_threshold: Mapped[int] = mapped_column(Integer, default=50)
    blocked_threshold: Mapped[int] = mapped_column(Integer, default=49)
    customer_weight: Mapped[int] = mapped_column(Integer, default=25)
    products_weight: Mapped[int] = mapped_column(Integer, default=40)
    quantities_weight: Mapped[int] = mapped_column(Integer, default=20)
    coherence_weight: Mapped[int] = mapped_column(Integer, default=10)
    llm_weight: Mapped[int] = mapped_column(Integer, default=5)
    block_without_customer: Mapped[bool] = mapped_column(Boolean, default=True)
    block_without_reference: Mapped[bool] = mapped_column(Boolean, default=True)
    block_without_quantity: Mapped[bool] = mapped_column(Boolean, default=True)
    block_below_threshold: Mapped[bool] = mapped_column(Boolean, default=True)


class DecisionSettings(Base):
    __tablename__ = "decision_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), unique=True)
    enable_exact_match: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_alias_match: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_relation_match: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_history_match: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_rag_match: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_llm_support: Mapped[bool] = mapped_column(Boolean, default=True)
    exact_priority: Mapped[int] = mapped_column(Integer, default=1)
    alias_priority: Mapped[int] = mapped_column(Integer, default=2)
    relation_priority: Mapped[int] = mapped_column(Integer, default=3)
    history_priority: Mapped[int] = mapped_column(Integer, default=4)
    rag_priority: Mapped[int] = mapped_column(Integer, default=5)
    llm_priority: Mapped[int] = mapped_column(Integer, default=6)
    customer_weight: Mapped[int] = mapped_column(Integer, default=20)
    product_weight: Mapped[int] = mapped_column(Integer, default=35)
    quantities_weight: Mapped[int] = mapped_column(Integer, default=15)
    history_weight: Mapped[int] = mapped_column(Integer, default=10)
    coherence_weight: Mapped[int] = mapped_column(Integer, default=10)
    rag_weight: Mapped[int] = mapped_column(Integer, default=5)
    llm_weight: Mapped[int] = mapped_column(Integer, default=5)
    min_alias_confidence: Mapped[float] = mapped_column(Float, default=0.85)
    min_history_frequency: Mapped[int] = mapped_column(Integer, default=3)
    min_product_frequency: Mapped[int] = mapped_column(Integer, default=2)
    max_doubtful_lines: Mapped[int] = mapped_column(Integer, default=1)
    learning_mode: Mapped[str] = mapped_column(String(50), default="supervisado")
    always_human_review: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_approve_aliases: Mapped[bool] = mapped_column(Boolean, default=False)
    block_new_customer: Mapped[bool] = mapped_column(Boolean, default=False)
    block_conflicting_aliases: Mapped[bool] = mapped_column(Boolean, default=True)
    block_missing_quantity: Mapped[bool] = mapped_column(Boolean, default=True)
    block_missing_reference: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    code: Mapped[str] = mapped_column(String(80), index=True)
    fiscal_name: Mapped[str] = mapped_column(String(255))
    commercial_name: Mapped[str | None] = mapped_column(String(255))
    primary_email: Mapped[str | None] = mapped_column(String(255))
    delegation: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(80))
    address: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(150))
    province: Mapped[str | None] = mapped_column(String(150))
    country: Mapped[str | None] = mapped_column(String(150))
    tax_id: Mapped[str | None] = mapped_column(String(80))
    assigned_salesperson: Mapped[str | None] = mapped_column(String(180))
    accounting_code: Mapped[str | None] = mapped_column(String(100))
    company_inactive: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="active")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    aliases: Mapped[list["CustomerAlias"]] = relationship(cascade="all, delete-orphan")
    domains: Mapped[list["CustomerDomain"]] = relationship(cascade="all, delete-orphan")
    contacts: Mapped[list["CustomerContact"]] = relationship(cascade="all, delete-orphan")
    contact_points: Mapped[list["CustomerContactPoint"]] = relationship(cascade="all, delete-orphan")


class CustomerAlias(Base):
    __tablename__ = "customer_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    alias: Mapped[str] = mapped_column(String(255), index=True)


class CustomerDomain(Base):
    __tablename__ = "customer_domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    domain: Mapped[str] = mapped_column(String(255), index=True)


class CustomerContact(Base):
    __tablename__ = "customer_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    contact_type: Mapped[str] = mapped_column(String(50), default="email")
    name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(80), index=True)
    position: Mapped[str | None] = mapped_column(String(120))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CustomerContactPoint(Base):
    __tablename__ = "customer_contact_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    type: Mapped[str] = mapped_column(String(50), index=True)
    value: Mapped[str] = mapped_column(String(255), index=True)
    label: Mapped[str | None] = mapped_column(String(255))
    contact_name: Mapped[str | None] = mapped_column(String(255))
    contact_role: Mapped[str | None] = mapped_column(String(120))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source: Mapped[str] = mapped_column(String(50), default="manual")
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("company_id", "type", "value"),)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    reference: Mapped[str] = mapped_column(String(100), index=True)
    alternative_code: Mapped[str | None] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(150))
    usual_supplier: Mapped[str | None] = mapped_column(String(255))
    family: Mapped[str | None] = mapped_column(String(150))
    subfamily: Mapped[str | None] = mapped_column(String(150))
    format: Mapped[str | None] = mapped_column(String(100))
    sale_unit: Mapped[str | None] = mapped_column(String(80))
    sale_price: Mapped[float | None] = mapped_column(Float)
    discount_percent: Mapped[float | None] = mapped_column(Float)
    size_group: Mapped[str | None] = mapped_column(String(120))
    colors: Mapped[str | None] = mapped_column(String(255))
    entry_date: Mapped[str | None] = mapped_column(String(50))
    obsolete: Mapped[bool] = mapped_column(Boolean, default=False)
    article_type: Mapped[str | None] = mapped_column(String(120))
    description_cont: Mapped[str | None] = mapped_column(Text)
    warehouse_location_code: Mapped[str | None] = mapped_column(String(120))
    replenishment_warehouse: Mapped[str | None] = mapped_column(String(120))
    ean: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(50), default="active")
    notes: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    aliases: Mapped[list["ProductAlias"]] = relationship(cascade="all, delete-orphan")


class ProductAlias(Base):
    __tablename__ = "product_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    alias: Mapped[str] = mapped_column(String(255), index=True)


class ProductEmbedding(Base):
    __tablename__ = "product_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    embedding_json: Mapped[str] = mapped_column(Text)
    embedding_text: Mapped[str] = mapped_column(Text)
    embedding_model: Mapped[str] = mapped_column(String(120), index=True)
    embedding_version: Mapped[str] = mapped_column(String(50), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    dimensions: Mapped[int] = mapped_column(Integer, default=0)
    embedded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped[Product] = relationship()

    __table_args__ = (UniqueConstraint("company_id", "product_id", "embedding_model", "embedding_version"),)


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(80), index=True)
    source_id: Mapped[str] = mapped_column(String(120), index=True)
    scope: Mapped[str] = mapped_column(String(40), default="global", index=True)
    content: Mapped[str] = mapped_column(Text)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    embedding_json: Mapped[str | None] = mapped_column(Text)
    embedding_text: Mapped[str | None] = mapped_column(Text)
    embedding_model: Mapped[str | None] = mapped_column(String(120), index=True)
    embedding_version: Mapped[str | None] = mapped_column(String(50), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    dimensions: Mapped[int] = mapped_column(Integer, default=0)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("company_id", "source_type", "source_id"),)


class CustomerProductKnowledge(Base):
    __tablename__ = "customer_product_knowledge"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    product_reference: Mapped[str] = mapped_column(String(100))
    product_name: Mapped[str] = mapped_column(String(255))
    customer_alias_used: Mapped[str | None] = mapped_column(String(255))
    source_context: Mapped[str | None] = mapped_column(String(80))
    times_ordered: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_count: Mapped[int] = mapped_column(Integer, default=0)
    manual_count: Mapped[int] = mapped_column(Integer, default=0)
    last_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    last_order_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivery_note_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_quantity: Mapped[float | None] = mapped_column(Float)
    total_quantity: Mapped[float] = mapped_column(Float, default=0)
    average_quantity: Mapped[float] = mapped_column(Float, default=0)
    min_quantity: Mapped[float | None] = mapped_column(Float)
    max_quantity: Mapped[float | None] = mapped_column(Float)
    usual_unit: Mapped[str | None] = mapped_column(String(80))
    comments_summary: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    is_habitual: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("company_id", "customer_id", "product_id"),)


class InputChannel(Base):
    __tablename__ = "input_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    key: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(150))
    channel_type: Mapped[str] = mapped_column(String(50), default="message")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_text: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_attachments: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_documents: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_images: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("company_id", "key"),)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("input_channels.id"), index=True)
    provider: Mapped[str] = mapped_column(String(50), default="imap")
    external_thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(50), default="open")
    subject: Mapped[str | None] = mapped_column(String(500))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    messages: Mapped[list["InboundMessage"]] = relationship(back_populates="conversation")
    orders: Mapped[list["Order"]] = relationship(back_populates="conversation")

    __table_args__ = (UniqueConstraint("company_id", "channel_id", "provider", "external_thread_id"),)


class ChannelSetting(Base):
    __tablename__ = "channel_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("input_channels.id"), index=True)
    key: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(50), default="string")
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("company_id", "channel_id", "key"),)


class InboundMessage(Base):
    __tablename__ = "inbound_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("input_channels.id"), index=True)
    provider: Mapped[str] = mapped_column(String(50), default="imap")
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    source_external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    source_thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    source_message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    source_mailbox: Mapped[str | None] = mapped_column(String(255), index=True)
    source_uidvalidity: Mapped[str | None] = mapped_column(String(120), index=True)
    source_uid: Mapped[str | None] = mapped_column(String(120), index=True)
    mailbox_id: Mapped[int | None] = mapped_column(Integer, index=True)
    direction: Mapped[str] = mapped_column(String(30), default="inbound")
    sender: Mapped[str | None] = mapped_column(String(255))
    recipient: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(500))
    original_content: Mapped[str | None] = mapped_column(Text)
    raw_payload_json: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(80))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(80), default="received")
    processing_step: Mapped[str] = mapped_column(String(80), default="received")
    detected_type: Mapped[str | None] = mapped_column(String(80))
    normalized_text: Mapped[str | None] = mapped_column(Text)
    classification_json: Mapped[str | None] = mapped_column(Text)
    extraction_json: Mapped[str | None] = mapped_column(Text)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    score: Mapped[float] = mapped_column(Float, default=0)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    has_pdf: Mapped[bool] = mapped_column(Boolean, default=False)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    processing_error: Mapped[str | None] = mapped_column(Text)
    last_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation | None] = relationship(back_populates="messages")
    attachments: Mapped[list["MessageAttachment"]] = relationship(cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("company_id", "channel_id", "provider", "source_external_id"),)


class MessageAttachment(Base):
    __tablename__ = "message_attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    inbound_message_id: Mapped[int] = mapped_column(ForeignKey("inbound_messages.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str | None] = mapped_column(String(500))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    transcription_text: Mapped[str | None] = mapped_column(Text)
    is_pdf: Mapped[bool] = mapped_column(Boolean, default=False)
    is_image: Mapped[bool] = mapped_column(Boolean, default=False)
    is_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    extraction_status: Mapped[str] = mapped_column(String(80), default="pending")
    extraction_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NormalizedInput(Base):
    __tablename__ = "normalized_inputs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    inbound_message_id: Mapped[int] = mapped_column(ForeignKey("inbound_messages.id"), index=True)
    normalized_text: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrderReview(Base):
    __tablename__ = "order_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    reviewer_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(80), default="pending")
    comments: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ManualCorrection(Base):
    __tablename__ = "manual_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    inbound_message_id: Mapped[int | None] = mapped_column(ForeignKey("inbound_messages.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    order_line_id: Mapped[int | None] = mapped_column(ForeignKey("order_lines.id"))
    entity_type: Mapped[str] = mapped_column(String(80))
    field_name: Mapped[str] = mapped_column(String(120))
    original_value: Mapped[str | None] = mapped_column(Text)
    corrected_value: Mapped[str | None] = mapped_column(Text)
    agent_value: Mapped[str | None] = mapped_column(Text)
    corrected_entity_id: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    should_learn: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LearnedAlias(Base):
    __tablename__ = "learned_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    alias_type: Mapped[str] = mapped_column(String(50), index=True)
    alias: Mapped[str] = mapped_column(String(255), index=True)
    canonical_value: Mapped[str] = mapped_column(String(255))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    source_correction_id: Mapped[int | None] = mapped_column(ForeignKey("manual_corrections.id"))
    confidence: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str | None] = mapped_column(String(120))
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("company_id", "alias_type", "alias"),)


class RagDocument(Base):
    __tablename__ = "rag_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(80))
    source_entity: Mapped[str] = mapped_column(String(80))
    source_entity_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    content_text: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    embedding_status: Mapped[str] = mapped_column(String(80), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RagCase(Base):
    __tablename__ = "rag_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    inbound_message_id: Mapped[int | None] = mapped_column(ForeignKey("inbound_messages.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    summary: Mapped[str] = mapped_column(Text)
    resolved_action: Mapped[str] = mapped_column(String(120))
    resolution_json: Mapped[str | None] = mapped_column(Text)
    similarity_score: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScoringResult(Base):
    __tablename__ = "scoring_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    inbound_message_id: Mapped[int | None] = mapped_column(ForeignKey("inbound_messages.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    total_score: Mapped[float] = mapped_column(Float, default=0)
    customer_score: Mapped[float] = mapped_column(Float, default=0)
    product_score: Mapped[float] = mapped_column(Float, default=0)
    confidence_score: Mapped[float] = mapped_column(Float, default=0)
    rule_score: Mapped[float] = mapped_column(Float, default=0)
    block_reason: Mapped[str | None] = mapped_column(Text)
    details_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    file_path: Mapped[str | None] = mapped_column(String(500))
    export_format: Mapped[str] = mapped_column(String(50), default="csv")
    destination_type: Mapped[str] = mapped_column(String(50), default="sftp")
    status: Mapped[str] = mapped_column(String(80), default="pending")
    status_message: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BackgroundJob(Base):
    __tablename__ = "background_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    job_type: Mapped[str] = mapped_column(String(80), index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    payload_json: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    lock_owner: Mapped[str | None] = mapped_column(String(120))
    lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(120))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    attempts: Mapped[list["JobAttempt"]] = relationship(cascade="all, delete-orphan", order_by="JobAttempt.attempt_number")

    __table_args__ = (UniqueConstraint("company_id", "job_type", "dedupe_key"),)


class JobAttempt(Base):
    __tablename__ = "job_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("background_jobs.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40), default="running")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("job_id", "attempt_number"),)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    inbound_message_id: Mapped[int | None] = mapped_column(ForeignKey("inbound_messages.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    alert_type: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(50), default="medium")
    status: Mapped[str] = mapped_column(String(50), default="open")
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("input_channels.id"))
    inbound_message_id: Mapped[int | None] = mapped_column(ForeignKey("inbound_messages.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    level: Mapped[str] = mapped_column(String(20), default="info")
    event: Mapped[str] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    imap_mailbox: Mapped[str | None] = mapped_column(String(255), index=True)
    imap_uidvalidity: Mapped[str | None] = mapped_column(String(120), index=True)
    imap_uid: Mapped[str | None] = mapped_column(String(120), index=True)
    mailbox_id: Mapped[int | None] = mapped_column(Integer, index=True)
    communication_id: Mapped[int | None] = mapped_column(ForeignKey("communications.id"), index=True)
    sender: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(80), default="pending")
    agent_status: Mapped[str] = mapped_column(String(80), default="not_processed")
    detected_type: Mapped[str | None] = mapped_column(String(80))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    has_pdf: Mapped[bool] = mapped_column(Boolean, default=False)
    processing_error: Mapped[str | None] = mapped_column(Text)
    processing_result_json: Mapped[str | None] = mapped_column(Text)
    last_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[Conversation | None] = relationship()
    communication: Mapped[Communication | None] = relationship()
    attachments: Mapped[list["EmailAttachment"]] = relationship(cascade="all, delete-orphan")


class EmailAttachment(Base):
    __tablename__ = "email_attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    is_pdf: Mapped[bool] = mapped_column(Boolean, default=False)
    extraction_status: Mapped[str] = mapped_column(String(80), default="pending")
    extraction_error: Mapped[str | None] = mapped_column(Text)
    storage_path: Mapped[str | None] = mapped_column(String(500))
    extracted_text: Mapped[str | None] = mapped_column(Text)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    validated_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    customer_detected_name: Mapped[str | None] = mapped_column(String(255))
    customer_identification_method: Mapped[str | None] = mapped_column(String(100))
    customer_score: Mapped[float] = mapped_column(Float, default=0)
    order_date: Mapped[str | None] = mapped_column(String(50))
    requested_delivery_date: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(80), default="pending_review")
    review_reasons: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    delete_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    email: Mapped[Email | None] = relationship()
    conversation: Mapped[Conversation | None] = relationship(back_populates="orders")
    customer: Mapped[Customer | None] = relationship(foreign_keys=[customer_id])
    validated_customer: Mapped[Customer | None] = relationship(foreign_keys=[validated_customer_id])
    lines: Mapped[list["OrderLine"]] = relationship(cascade="all, delete-orphan")


class OrderLine(Base):
    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    validated_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    original_text: Mapped[str | None] = mapped_column(Text)
    detected_reference: Mapped[str | None] = mapped_column(String(120))
    detected_product: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(80))
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0)
    line_score: Mapped[float] = mapped_column(Float, default=0)
    validation_status: Mapped[str] = mapped_column(String(80), default="pending")
    doubt_reason: Mapped[str | None] = mapped_column(Text)

    product: Mapped[Product | None] = relationship(foreign_keys=[product_id])
    validated_product: Mapped[Product | None] = relationship(foreign_keys=[validated_product_id])


class ImportJob(Base):
    __tablename__ = "imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(80), default="completed")
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_created: Mapped[int] = mapped_column(Integer, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, default=0)
    rows_ignored: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[str | None] = mapped_column(Text)
    mapping_used: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ImportMappingTemplate(Base):
    __tablename__ = "import_mapping_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(150))
    mapping_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("company_id", "entity_type", "name"),)


class ExportFile(Base):
    __tablename__ = "export_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    filename: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(80), default="generated")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    purpose: Mapped[str] = mapped_column(String(100))
    active_version_id: Mapped[int | None] = mapped_column(Integer)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("prompt_templates.id"))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PromptExecution(Base):
    __tablename__ = "prompt_executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    prompt_template_id: Mapped[int | None] = mapped_column(ForeignKey("prompt_templates.id"), index=True)
    prompt_name: Mapped[str] = mapped_column(String(150))
    prompt_purpose: Mapped[str] = mapped_column(String(100), index=True)
    prompt_version: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(100))
    parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    input_reference: Mapped[str | None] = mapped_column(String(255))
    output_status: Mapped[str] = mapped_column(String(50), default="pending")
    validation_errors_json: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    response_hash: Mapped[str | None] = mapped_column(String(120))
    response_excerpt: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LearningProposal(Base):
    __tablename__ = "learning_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(80), index=True)
    source_value: Mapped[str] = mapped_column(String(255))
    target_type: Mapped[str] = mapped_column(String(80), index=True)
    target_id: Mapped[int | None] = mapped_column(Integer)
    suggested_action: Mapped[str] = mapped_column(String(80))
    evidence_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="proposed")
    source_correction_id: Mapped[int | None] = mapped_column(Integer)
    conflict_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TenantSchemaMigration(Base):
    __tablename__ = "schema_migrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), unique=True, index=True)
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
