from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)

DEV_SECRET_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"kibak-local-development-key").digest()).decode()
ALLOWED_ENVIRONMENTS = {"development", "demo", "test", "production"}
LOCAL_ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
LOCAL_CORS_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:8001",
    "http://127.0.0.1:8001",
]
PROHIBITED_SECRET_KEY_VALUES = {
    "secret",
    "changeme",
    "dev",
    "development",
    "demo",
    "test",
    "password",
    "your-secret-key",
}
PROHIBITED_ADMIN_PASSWORDS = {
    "",
    "admin",
    "admin123",
    "changeme",
    "demo",
    "password",
    "test",
}


def _split_csv(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _derive_fernet_key(source: str) -> str:
    digest = hashlib.sha256(source.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


def _is_valid_fernet_key(value: str | None) -> bool:
    if not value:
        return False
    try:
        Fernet(value.encode())
    except (ValueError, TypeError):
        return False
    return True


def _looks_safe_secret_key(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip()
    lowered = normalized.lower()
    if len(normalized) < 32:
        return False
    if normalized == DEV_SECRET_KEY:
        return False
    if any(lowered == token or lowered.startswith(f"{token}-") or lowered.startswith(f"{token}_") for token in PROHIBITED_SECRET_KEY_VALUES):
        return False
    if normalized in {"secret", "changeme", "demo", "test", "password", "your-secret-key"}:
        return False
    return True


def _sanitize_database_url(url: str | None) -> str:
    if not url:
        return ""
    if "@" not in url:
        return url
    prefix, suffix = url.split("://", 1) if "://" in url else ("", url)
    if ":" not in suffix or "@" not in suffix:
        return url
    credentials, rest = suffix.split("@", 1)
    if ":" not in credentials:
        return url
    username, _password = credentials.split(":", 1)
    scheme = f"{prefix}://" if prefix else ""
    return f"{scheme}{username}:***@{rest}"


class Settings(BaseSettings):
    app_name: str = "KIBAK"
    app_slug: str = "kibak"
    kibak_isolated_routes: bool = Field(default=True, validation_alias="KIBAK_ISOLATED_ROUTES")
    database_url: str = "sqlite:///./kibak_local.db"
    master_database_url: str = "sqlite:///./kibak_master.db"
    tenant_db_mode: str = Field(default="sqlite", validation_alias=AliasChoices("TENANT_DB_MODE"))
    tenant_database_url: str | None = Field(default=None, validation_alias=AliasChoices("TENANT_DATABASE_URL"))
    tenant_runtime_database_host: str | None = Field(default=None, validation_alias=AliasChoices("TENANT_DATABASE_RUNTIME_HOST"))
    tenant_runtime_database_port: int | None = Field(default=None, validation_alias=AliasChoices("TENANT_DATABASE_RUNTIME_PORT"))
    app_secret_key: str = Field(default=DEV_SECRET_KEY, validation_alias=AliasChoices("SECRET_KEY", "APP_SECRET_KEY"))
    tenant_db_encryption_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ENCRYPTION_KEY", "APP_ENCRYPTION_KEY", "TENANT_DB_ENCRYPTION_KEY"),
    )
    auth_secret: str = DEV_SECRET_KEY
    cron_secret: str = ""
    enable_legacy_sync: bool = False
    branding_cache_ttl_seconds: int = 30
    email_worker_poll_seconds: int = 15
    job_worker_poll_seconds: int = 10
    job_poll_interval_seconds: int | None = Field(default=None, validation_alias="JOB_POLL_INTERVAL_SECONDS")
    job_max_attempts: int = Field(default=3, validation_alias="JOB_MAX_ATTEMPTS")
    job_retry_base_seconds: int = Field(default=15, validation_alias="JOB_RETRY_BASE_SECONDS")
    job_retry_max_seconds: int = Field(default=300, validation_alias="JOB_RETRY_MAX_SECONDS")
    job_stale_after_seconds: int = Field(default=900, validation_alias="JOB_STALE_AFTER_SECONDS")
    app_url: str = "http://127.0.0.1:8000"
    meta_app_id: str = Field(default="", validation_alias=AliasChoices("META_APP_ID", "FB_APP_ID"))
    meta_app_secret: str = Field(default="", validation_alias=AliasChoices("META_APP_SECRET", "FB_APP_SECRET"))
    meta_embedded_signup_config_id: str = Field(
        default="",
        validation_alias=AliasChoices("META_EMBEDDED_SIGNUP_CONFIG_ID", "FB_EMBEDDED_SIGNUP_CONFIG_ID"),
    )
    meta_graph_api_version: str = Field(default="v24.0", validation_alias=AliasChoices("META_GRAPH_API_VERSION", "FB_GRAPH_API_VERSION"))
    meta_embedded_signup_version: str = Field(default="v4", validation_alias="META_EMBEDDED_SIGNUP_VERSION")
    meta_whatsapp_registration_pin: str = Field(
        default="",
        validation_alias=AliasChoices("META_WHATSAPP_REGISTRATION_PIN", "FB_REG_PIN"),
    )
    meta_whatsapp_verify_token: str = Field(
        default="",
        validation_alias=AliasChoices("META_WHATSAPP_VERIFY_TOKEN", "FB_VERIFY_TOKEN"),
    )
    meta_oauth_redirect_uri: str = Field(default="", validation_alias="META_OAUTH_REDIRECT_URI")
    meta_request_timeout_seconds: int = Field(default=20, validation_alias="META_REQUEST_TIMEOUT_SECONDS")
    session_cookie: str = "kibak_session"
    session_cookie_secure: bool | None = Field(default=None, validation_alias="SESSION_COOKIE_SECURE")
    session_cookie_samesite: str | None = Field(default=None, validation_alias="SESSION_COOKIE_SAMESITE")
    session_max_age: int | None = Field(default=None, validation_alias="SESSION_MAX_AGE")
    session_cookie_domain: str | None = Field(default=None, validation_alias="SESSION_COOKIE_DOMAIN")
    cors_allowed_origins_raw: str | None = Field(default=None, validation_alias="CORS_ALLOWED_ORIGINS")
    allowed_hosts_raw: str | None = Field(default=None, validation_alias="ALLOWED_HOSTS")
    environment: str = Field(default="development", validation_alias=AliasChoices("APP_ENV", "ENVIRONMENT"))
    debug: bool | None = Field(default=None, validation_alias="DEBUG")
    default_company_name: str = "KIBAK Test"
    default_admin_email: str = "admin@kibak.local"
    default_admin_password: str = ""
    seed_demo_data: bool | None = Field(default=None, validation_alias=AliasChoices("ENABLE_DEMO_BOOTSTRAP", "SEED_DEMO_DATA"))
    branding_app_name: str = "KIBAK"
    branding_primary_claim: str = "Comunicaciones y routing inteligente"
    branding_secondary_claim: str = ""
    branding_logo_url: str = ""
    branding_dark_logo_url: str = ""
    branding_favicon_url: str = ""
    email_signature_text: str = "Equipo KIBAK"
    log_format: str = Field(default="json", validation_alias=AliasChoices("LOG_FORMAT", "APP_LOG_FORMAT"))
    log_level: str = Field(default="info", validation_alias=AliasChoices("LOG_LEVEL", "APP_LOG_LEVEL"))
    performance_profiling_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("PERFORMANCE_PROFILING_ENABLED", "ENABLE_PERFORMANCE_PROFILING"),
    )

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @model_validator(mode="after")
    def validate_runtime_configuration(self):
        self.environment = (self.environment or "development").strip().lower()
        if self.environment not in ALLOWED_ENVIRONMENTS:
            raise ValueError("APP_ENV must be development, demo, test or production")
        # The legacy suite runs against SQLite fixtures and needs its historical
        # route surface; development and deployed KIBAK remain isolated by default.
        if self.environment == "test":
            self.kibak_isolated_routes = False
        self.tenant_db_mode = (self.tenant_db_mode or "sqlite").strip().lower()
        if self.tenant_db_mode not in {"sqlite", "external"}:
            raise ValueError("TENANT_DB_MODE must be sqlite or external")
        if self.tenant_database_url is not None:
            self.tenant_database_url = self.tenant_database_url.strip() or None
        if self.tenant_database_url:
            self.database_url = self.tenant_database_url
        if self.tenant_runtime_database_host is not None:
            self.tenant_runtime_database_host = self.tenant_runtime_database_host.strip() or None
        if self.tenant_runtime_database_port is not None and not 1 <= self.tenant_runtime_database_port <= 65535:
            raise ValueError("TENANT_DATABASE_RUNTIME_PORT must be between 1 and 65535")
        running_on_vercel = os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))
        demo_runtime = self.environment == "demo" or running_on_vercel

        if self.debug is None:
            self.debug = False
        if self.seed_demo_data is None:
            self.seed_demo_data = self.environment in {"development", "demo"}
        elif self.environment == "demo":
            self.seed_demo_data = True
        if self.session_cookie_secure is None:
            self.session_cookie_secure = self.environment == "production"
        if self.session_cookie_samesite is None:
            self.session_cookie_samesite = "lax"
        self.session_cookie_samesite = self.session_cookie_samesite.strip().lower()
        if self.session_cookie_samesite not in {"lax", "strict", "none"}:
            raise ValueError("SESSION_COOKIE_SAMESITE must be lax, strict or none")
        if self.session_max_age is None:
            self.session_max_age = 60 * 60 * 24 * 7
        if self.session_max_age <= 0:
            raise ValueError("SESSION_MAX_AGE must be greater than zero")

        if self.job_poll_interval_seconds is not None:
            self.job_worker_poll_seconds = int(self.job_poll_interval_seconds)
        if self.job_worker_poll_seconds <= 0:
            raise ValueError("JOB_POLL_INTERVAL_SECONDS must be greater than zero")
        if self.job_max_attempts <= 0:
            raise ValueError("JOB_MAX_ATTEMPTS must be greater than zero")
        if self.job_retry_base_seconds <= 0:
            raise ValueError("JOB_RETRY_BASE_SECONDS must be greater than zero")
        if self.job_retry_max_seconds < self.job_retry_base_seconds:
            raise ValueError("JOB_RETRY_MAX_SECONDS must be greater than or equal to JOB_RETRY_BASE_SECONDS")
        if self.job_stale_after_seconds <= 0:
            raise ValueError("JOB_STALE_AFTER_SECONDS must be greater than zero")
        self.meta_graph_api_version = (self.meta_graph_api_version or "v24.0").strip().lower()
        if not re.fullmatch(r"v\d+\.\d+", self.meta_graph_api_version):
            raise ValueError("META_GRAPH_API_VERSION must use the vNN.N format")
        self.meta_embedded_signup_version = (self.meta_embedded_signup_version or "v4").strip().lower()
        if not re.fullmatch(r"v\d+(?:-[a-z0-9-]+)?", self.meta_embedded_signup_version):
            raise ValueError("META_EMBEDDED_SIGNUP_VERSION is invalid")
        self.meta_whatsapp_registration_pin = (self.meta_whatsapp_registration_pin or "").strip()
        if self.meta_whatsapp_registration_pin and not re.fullmatch(r"\d{6}", self.meta_whatsapp_registration_pin):
            raise ValueError("META_WHATSAPP_REGISTRATION_PIN must contain exactly 6 digits")
        if self.meta_request_timeout_seconds <= 0:
            raise ValueError("META_REQUEST_TIMEOUT_SECONDS must be greater than zero")
        self.log_format = (self.log_format or "json").strip().lower()
        if self.log_format not in {"json", "text"}:
            raise ValueError("LOG_FORMAT must be json or text")
        self.log_level = (self.log_level or "info").strip().lower()
        if self.environment == "production" and self.performance_profiling_enabled:
            raise ValueError("PERFORMANCE_PROFILING_ENABLED cannot be enabled in production")

        if self.tenant_db_encryption_key and not _is_valid_fernet_key(self.tenant_db_encryption_key):
            raise ValueError("ENCRYPTION_KEY must be a valid Fernet key")
        if not self.tenant_db_encryption_key and self.environment == "production":
            raise ValueError("ENCRYPTION_KEY is required in production")
        if demo_runtime:
            if not self.master_database_url:
                raise ValueError("MASTER_DATABASE_URL is required in demo or Vercel")
            if self.master_database_url.startswith("sqlite"):
                raise ValueError("MASTER_DATABASE_URL must point to an external database in demo or Vercel")
            if self.tenant_db_mode == "external":
                if not self.tenant_database_url:
                    raise ValueError("TENANT_DATABASE_URL is required when TENANT_DB_MODE=external")
                if self.database_url.startswith("sqlite"):
                    raise ValueError("TENANT_DATABASE_URL cannot use sqlite in demo or Vercel")
            elif self.database_url.startswith("sqlite"):
                raise ValueError("DATABASE_URL must point to an external database in demo or Vercel")

        if self.environment == "production":
            if not _looks_safe_secret_key(self.app_secret_key):
                raise ValueError("Unsafe SECRET_KEY for production")
            if self.debug:
                raise ValueError("DEBUG cannot be enabled in production")
            if self.seed_demo_data:
                raise ValueError("ENABLE_DEMO_BOOTSTRAP cannot be enabled in production")
            if not self.session_cookie_secure:
                raise ValueError("SESSION_COOKIE_SECURE must be enabled in production")
            if not self.allowed_hosts:
                raise ValueError("ALLOWED_HOSTS must be explicit in production")
            if "*" in self.allowed_hosts:
                raise ValueError("ALLOWED_HOSTS cannot contain * in production")
            if "*" in self.cors_allowed_origins:
                raise ValueError("CORS wildcard is not allowed with credentials")
            if self.database_url.startswith("sqlite"):
                raise ValueError("DATABASE_URL cannot use sqlite in production")
            if self.master_database_url.startswith("sqlite"):
                raise ValueError("MASTER_DATABASE_URL cannot use sqlite in production")
            if self.default_admin_email.strip().lower() == "admin@kibak.local":
                raise ValueError("DEFAULT_ADMIN_EMAIL must be customized in production")
            if self.default_admin_password.strip().lower() in PROHIBITED_ADMIN_PASSWORDS or len(self.default_admin_password.strip()) < 12:
                raise ValueError("DEFAULT_ADMIN_PASSWORD must be stronger than the demo value in production")

        return self

    @property
    def cors_allowed_origins(self) -> list[str]:
        origins = _split_csv(self.cors_allowed_origins_raw)
        if origins:
            return origins
        if self.environment in {"development", "test"}:
            return LOCAL_CORS_ORIGINS.copy()
        return []

    @property
    def allowed_hosts(self) -> list[str]:
        hosts = _split_csv(self.allowed_hosts_raw)
        if hosts:
            return hosts
        if self.environment in {"development", "test"}:
            return LOCAL_ALLOWED_HOSTS.copy()
        return []

    @property
    def encryption_key(self) -> str:
        if self.tenant_db_encryption_key:
            return self.tenant_db_encryption_key
        return _derive_fernet_key(self.app_secret_key)

    @property
    def meta_whatsapp_missing_configuration(self) -> list[str]:
        required = {
            "META_APP_ID": self.meta_app_id,
            "META_APP_SECRET": self.meta_app_secret,
            "META_EMBEDDED_SIGNUP_CONFIG_ID": self.meta_embedded_signup_config_id,
            "META_WHATSAPP_VERIFY_TOKEN": self.meta_whatsapp_verify_token,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if not (self.app_url or "").strip().lower().startswith("https://"):
            missing.append("APP_URL (HTTPS)")
        return missing

    @property
    def meta_whatsapp_embedded_signup_missing_configuration(self) -> list[str]:
        """Return only the settings required to complete Embedded Signup.

        Tenant webhooks receive a verify token generated during the signup and
        stored with that tenant.  The global token is kept for the legacy
        callback at ``/webhooks/whatsapp`` and must not make the tenant-scoped
        Embedded Signup unavailable.
        """
        required = {
            "META_APP_ID": self.meta_app_id,
            "META_APP_SECRET": self.meta_app_secret,
            "META_EMBEDDED_SIGNUP_CONFIG_ID": self.meta_embedded_signup_config_id,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if not (self.app_url or "").strip().lower().startswith("https://"):
            missing.append("APP_URL (HTTPS)")
        return missing

    @property
    def meta_whatsapp_embedded_signup_ready(self) -> bool:
        return not self.meta_whatsapp_embedded_signup_missing_configuration

    def __repr__(self) -> str:
        parts = {
            "app_name": self.app_name,
            "app_slug": self.app_slug,
            "environment": self.environment,
            "database_url": _sanitize_database_url(self.database_url),
            "master_database_url": _sanitize_database_url(self.master_database_url),
            "session_cookie": self.session_cookie,
            "session_cookie_secure": self.session_cookie_secure,
            "session_cookie_samesite": self.session_cookie_samesite,
            "session_max_age": self.session_max_age,
            "debug": self.debug,
            "seed_demo_data": self.seed_demo_data,
            "tenant_db_mode": self.tenant_db_mode,
            "tenant_database_url": _sanitize_database_url(self.tenant_database_url),
            "allowed_hosts": self.allowed_hosts,
            "cors_allowed_origins": self.cors_allowed_origins,
            "app_secret_key": "[redacted]",
            "tenant_db_encryption_key": "[redacted]" if self.tenant_db_encryption_key else "",
            "auth_secret": "[redacted]" if self.auth_secret else "",
            "cron_secret": "[redacted]" if self.cron_secret else "",
            "meta_app_id": self.meta_app_id,
            "meta_app_secret": "[redacted]" if self.meta_app_secret else "",
            "meta_embedded_signup_config_id": self.meta_embedded_signup_config_id,
            "meta_graph_api_version": self.meta_graph_api_version,
            "meta_embedded_signup_version": self.meta_embedded_signup_version,
            "meta_whatsapp_registration_pin": "[redacted]" if self.meta_whatsapp_registration_pin else "",
            "meta_whatsapp_verify_token": "[redacted]" if self.meta_whatsapp_verify_token else "",
            "meta_oauth_redirect_uri": self.meta_oauth_redirect_uri,
            "default_company_name": self.default_company_name,
            "default_admin_email": self.default_admin_email,
            "default_admin_password": "[redacted]" if self.default_admin_password else "",
        }
        return f"Settings({parts!r})"

    __str__ = __repr__


@lru_cache
def get_settings() -> Settings:
    if "APP_ENV" not in os.environ and "ENVIRONMENT" not in os.environ:
        logger.warning("APP_ENV no definido; usando development por compatibilidad local.")
    return Settings()
