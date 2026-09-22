from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import InputChannel
from app.core.config import get_settings


CHANNEL_DEFINITIONS = {
    "email": {
        "name": "Email",
        "channel_type": "message",
        "supports_text": True,
        "supports_attachments": True,
        "supports_audio": False,
        "supports_documents": True,
        "supports_images": False,
    },
    "whatsapp": {
        "name": "WhatsApp",
        "channel_type": "message",
        "supports_text": True,
        "supports_attachments": True,
        "supports_audio": True,
        "supports_documents": True,
        "supports_images": False,
    },
}


def get_or_create_channel(
    db: Session,
    company_id: int,
    key: str,
) -> InputChannel:
    channel = db.scalar(
        select(InputChannel).where(
            InputChannel.company_id == company_id,
            InputChannel.key == key,
        )
    )

    if channel:
        return channel

    definition = CHANNEL_DEFINITIONS.get(key)

    if not definition:
        raise ValueError(f"Unsupported channel: {key}")

    channel = InputChannel(
        company_id=company_id,
        key=key,
        is_active=False,
        is_default=False,
        **definition,
    )

    db.add(channel)
    db.flush()

    return channel


def is_channel_enabled(
    db: Session,
    company_id: int,
    key: str,
) -> bool:
    # The isolated KIBAK PostgreSQL schema routes email through Mailbox and
    # Communication directly; it intentionally has no legacy input_channels
    # table. Keep the legacy channel gate for other runtimes only.
    bind = db.get_bind()
    if (
        key == "email"
        and get_settings().app_slug.strip().lower() == "kibak"
        and getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"
    ):
        return True
    channel = get_or_create_channel(
        db,
        company_id,
        key,
    )

    return bool(channel.is_active)
