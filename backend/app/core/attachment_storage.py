from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from app.core.observability import tenant_id_var
from app.core.storage import resolve_temp_storage_dir


def _is_vercel_runtime() -> bool:
    return os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))


def _use_vercel_blob() -> bool:
    return bool(
        _is_vercel_runtime()
        and os.getenv("BLOB_READ_WRITE_TOKEN")
    )


def storage_backend() -> str:
    """Return the selected backend without contacting the provider."""
    # Resolve this small switch without constructing the full application settings
    # object, so legacy/serverless storage guards remain independently testable.
    configured = os.getenv("STORAGE_BACKEND")
    if configured:
        return configured.strip().lower()
    try:
        configured = getattr(get_settings(), "storage_backend", None)
    except Exception:
        configured = None
    return str(configured or "local").strip().lower()


def validate_storage_configuration() -> dict[str, object]:
    """Validate storage configuration without creating buckets or network calls."""
    backend = storage_backend()
    if backend == "local":
        return {"ok": True, "backend": "local", "persistent": False}
    settings = get_settings()
    missing = [
        name
        for name, value in {
            "S3_BUCKET": getattr(settings, "s3_bucket", None),
            "S3_ACCESS_KEY_ID": getattr(settings, "s3_access_key_id", None),
            "S3_SECRET_ACCESS_KEY": getattr(settings, "s3_secret_access_key", None),
        }.items()
        if not value
    ]
    return {
        "ok": not missing,
        "backend": "s3",
        "persistent": True,
        "missing": missing,
    }


def _s3_client():
    settings = get_settings()
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised by deployment checks
        raise RuntimeError("S3 storage requires the boto3 dependency") from exc
    secret = getattr(settings, "s3_secret_access_key", None)
    return boto3.client(
        "s3",
        endpoint_url=getattr(settings, "s3_endpoint_url", None) or None,
        region_name=getattr(settings, "s3_region", "auto") or "auto",
        aws_access_key_id=getattr(settings, "s3_access_key_id", None),
        aws_secret_access_key=secret.get_secret_value() if secret else None,
    )


def _object_key(storage_name: str) -> str:
    settings = get_settings()
    tenant_id = tenant_id_var.get()
    tenant_part = f"tenant-{tenant_id}" if tenant_id is not None else "system"
    prefix = str(getattr(settings, "s3_prefix", "kibak") or "kibak").strip("/")
    return f"{prefix}/{tenant_part}/{storage_name.lstrip('/')}"


def _save_s3(*, storage_name: str, payload: bytes, content_type: str | None = None) -> str:
    settings = get_settings()
    bucket = str(getattr(settings, "s3_bucket", "") or "").strip()
    if not bucket:
        raise RuntimeError("S3_BUCKET is not configured")
    key = _object_key(storage_name)
    client = _s3_client()
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType=content_type or "application/octet-stream",
        ServerSideEncryption="AES256",
    )
    return f"s3://{bucket}/{key}"


def _read_s3(storage_ref: str) -> bytes:
    bucket, _, key = storage_ref[5:].partition("/")
    if not bucket or not key:
        raise RuntimeError("Invalid S3 storage reference")
    result = _s3_client().get_object(Bucket=bucket, Key=key)
    body = result["Body"]
    try:
        return body.read()
    finally:
        body.close()


def save_attachment(
    *,
    filename: str,
    payload: bytes,
    content_type: str | None = None,
) -> str:
    safe_filename = Path(filename).name
    storage_name = f"attachments/{uuid4().hex}-{safe_filename}"

    if storage_backend() == "s3":
        return _save_s3(storage_name=storage_name, payload=payload, content_type=content_type)

    if _use_vercel_blob():
        from vercel.blob import BlobClient

        client = BlobClient()
        try:
            result = client.put(
                storage_name,
                payload,
                access="private",
                content_type=content_type or "application/octet-stream",
                overwrite=False,
            )
            return result.url
        finally:
            client.close()

    if _is_vercel_runtime():
        raise RuntimeError("Persistent attachment storage is not configured for Vercel.")

    root = resolve_temp_storage_dir("attachments")
    root.mkdir(parents=True, exist_ok=True)

    path = root / storage_name.replace("attachments/", "", 1)
    path.write_bytes(payload)
    return str(path)


def read_attachment(storage_ref: str) -> bytes:
    if storage_ref.startswith("s3://"):
        return _read_s3(storage_ref)
    if storage_ref.startswith(("https://", "http://")):
        from vercel.blob import BlobClient

        client = BlobClient()
        try:
            result = client.get(storage_ref, access="private")
            return result.content
        finally:
            client.close()

    return Path(storage_ref).read_bytes()
