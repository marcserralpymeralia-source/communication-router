from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.auth.client_ip import resolve_client_ip
from app.core.config import get_settings
from app.master.models import AuthThrottle


IP_SCOPE = "ip"
SUBJECT_SCOPE = "subject"
WINDOW = timedelta(minutes=15)
IP_LIMIT = 20
SUBJECT_LIMIT = 8
BLOCK_DURATION = timedelta(minutes=15)
CLEANUP_AFTER = timedelta(days=1)


class AuthThrottleUnavailable(RuntimeError):
    """Raised when the distributed limiter cannot safely evaluate a request."""


@dataclass(frozen=True)
class ThrottleDecision:
    allowed: bool
    retry_after_seconds: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if not value:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _key_hash(scope: str, value: str) -> str:
    secret = get_settings().auth_secret.encode("utf-8")
    return hmac.new(secret, f"auth-throttle:{scope}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _keys(request, email: str) -> tuple[tuple[str, str], ...]:  # noqa: ANN001
    normalized_email = " ".join(email.strip().lower().split())
    return (
        (IP_SCOPE, resolve_client_ip(request)),
        (SUBJECT_SCOPE, normalized_email),
    )


def _insert_do_nothing(db: Session, *, scope: str, key_hash: str, now: datetime) -> None:
    values = {
        "scope": scope,
        "key_hash": key_hash,
        "attempt_count": 0,
        "window_started_at": now,
        "last_attempt_at": now,
        "created_at": now,
        "updated_at": now,
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgres_insert(AuthThrottle).values(**values).on_conflict_do_nothing(index_elements=["scope", "key_hash"])
    elif dialect == "sqlite":
        statement = sqlite_insert(AuthThrottle).values(**values).on_conflict_do_nothing(index_elements=["scope", "key_hash"])
    else:  # pragma: no cover - supported databases are PostgreSQL and SQLite
        statement = AuthThrottle.__table__.insert().values(**values)
    try:
        db.execute(statement)
    except Exception as exc:  # noqa: BLE001
        raise AuthThrottleUnavailable("No se pudo inicializar el control de autenticación") from exc


def _locked_row(db: Session, *, scope: str, key_hash: str, now: datetime) -> AuthThrottle:
    row = db.scalar(
        select(AuthThrottle)
        .where(AuthThrottle.scope == scope, AuthThrottle.key_hash == key_hash)
        .with_for_update()
    )
    if row is None:
        _insert_do_nothing(db, scope=scope, key_hash=key_hash, now=now)
        row = db.scalar(
            select(AuthThrottle)
            .where(AuthThrottle.scope == scope, AuthThrottle.key_hash == key_hash)
            .with_for_update()
        )
    if row is None:
        raise AuthThrottleUnavailable("No se pudo leer el control de autenticación")
    return row


class AuthThrottleStore:
    def __init__(self, db: Session, request) -> None:  # noqa: ANN001
        self.db = db
        self.request = request

    def _rows(self, email: str, now: datetime) -> list[tuple[str, AuthThrottle, int]]:
        limits = {IP_SCOPE: IP_LIMIT, SUBJECT_SCOPE: SUBJECT_LIMIT}
        return [
            (scope, _locked_row(self.db, scope=scope, key_hash=_key_hash(scope, value), now=now), limits[scope])
            for scope, value in _keys(self.request, email)
        ]

    def check(self, email: str) -> ThrottleDecision:
        now = _now()
        retry_after = 0
        for _scope, row, _limit in self._rows(email, now):
            blocked_until = _aware(row.blocked_until)
            if blocked_until and blocked_until > now:
                retry_after = max(retry_after, int((blocked_until - now).total_seconds()))
        return ThrottleDecision(allowed=retry_after == 0, retry_after_seconds=retry_after)

    def record_failure(self, email: str) -> None:
        now = _now()
        for _scope, row, limit in self._rows(email, now):
            window_started = _aware(row.window_started_at) or now
            if now - window_started >= WINDOW:
                row.window_started_at = now
                row.attempt_count = 0
                row.blocked_until = None
            row.attempt_count = int(row.attempt_count or 0) + 1
            row.last_attempt_at = now
            row.updated_at = now
            if row.attempt_count >= limit:
                row.blocked_until = now + BLOCK_DURATION
        self._cleanup(now)

    def record_success(self, email: str) -> None:
        subject_hash = _key_hash(SUBJECT_SCOPE, " ".join(email.strip().lower().split()))
        self.db.execute(delete(AuthThrottle).where(AuthThrottle.scope == SUBJECT_SCOPE, AuthThrottle.key_hash == subject_hash))
        self._cleanup(_now())

    def _cleanup(self, now: datetime) -> None:
        cutoff = now - CLEANUP_AFTER
        self.db.execute(delete(AuthThrottle).where(AuthThrottle.updated_at < cutoff, AuthThrottle.blocked_until.is_(None)))


def throttling_enabled() -> bool:
    return bool(get_settings().auth_throttling_enabled)
