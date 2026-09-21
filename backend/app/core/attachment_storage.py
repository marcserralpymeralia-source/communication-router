from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from app.core.storage import resolve_temp_storage_dir


class TenantStorageError(RuntimeError):
    """Raised when a storage operation cannot prove its tenant boundary."""


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
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - exercised by deployment checks
        raise RuntimeError("S3 storage requires the boto3 dependency") from exc
    secret = getattr(settings, "s3_secret_access_key", None)
    return boto3.client(
        "s3",
        endpoint_url=getattr(settings, "s3_endpoint_url", None) or None,
        region_name=getattr(settings, "s3_region", "auto") or "auto",
        aws_access_key_id=getattr(settings, "s3_access_key_id", None),
        aws_secret_access_key=secret.get_secret_value() if secret else None,
        config=Config(s3={"addressing_style": "path"}),
    )


def _require_tenant_id(tenant_id: int | None) -> int:
    try:
        value = int(tenant_id) if tenant_id is not None else 0
    except (TypeError, ValueError) as exc:
        raise TenantStorageError("Storage requiere un tenant_id valido.") from exc
    if value <= 0:
        raise TenantStorageError("Storage requiere un tenant_id valido.")
    return value


def _safe_storage_name(storage_name: str) -> str:
    normalized = storage_name.replace("\\", "/").lstrip("/")
    path = Path(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise TenantStorageError("La referencia de storage no es segura.")
    return "/".join(path.parts)


def _object_prefix(tenant_id: int) -> str:
    settings = get_settings()
    prefix = str(getattr(settings, "s3_prefix", "kibak") or "kibak").strip("/")
    return f"{prefix}/tenants/{_require_tenant_id(tenant_id)}"


def _object_key(storage_name: str, *, tenant_id: int) -> str:
    return f"{_object_prefix(tenant_id)}/{_safe_storage_name(storage_name)}"


def _save_s3(*, storage_name: str, tenant_id: int, payload: bytes, content_type: str | None = None) -> str:
    settings = get_settings()
    bucket = str(getattr(settings, "s3_bucket", "") or "").strip()
    if not bucket:
        raise RuntimeError("S3_BUCKET is not configured")
    key = _object_key(storage_name, tenant_id=tenant_id)
    client = _s3_client()
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType=content_type or "application/octet-stream",
        ServerSideEncryption="AES256",
    )
    return f"s3://{bucket}/{key}"


def _parse_s3_reference(storage_ref: str, *, tenant_id: int) -> tuple[str, str]:
    bucket, _, key = storage_ref[5:].partition("/")
    configured_bucket = str(getattr(get_settings(), "s3_bucket", "") or "").strip()
    prefix = _object_prefix(tenant_id)
    if not bucket or not key or (configured_bucket and bucket != configured_bucket) or not key.startswith(f"{prefix}/"):
        raise TenantStorageError("La referencia S3 no pertenece al tenant.")
    if ".." in Path(key).parts:
        raise TenantStorageError("La referencia S3 no es segura.")
    return bucket, key


def _read_s3(storage_ref: str, *, tenant_id: int) -> bytes:
    bucket, key = _parse_s3_reference(storage_ref, tenant_id=tenant_id)
    result = _s3_client().get_object(Bucket=bucket, Key=key)
    body = result["Body"]
    try:
        return body.read()
    finally:
        body.close()


def save_attachment(
    *,
    tenant_id: int,
    filename: str,
    payload: bytes,
    content_type: str | None = None,
) -> str:
    tenant_id = _require_tenant_id(tenant_id)
    safe_filename = Path(filename).name
    storage_name = f"attachments/{uuid4().hex}-{safe_filename}"

    if storage_backend() == "s3":
        return _save_s3(storage_name=storage_name, tenant_id=tenant_id, payload=payload, content_type=content_type)

    if _use_vercel_blob():
        from vercel.blob import BlobClient

        client = BlobClient()
        try:
            result = client.put(
                f"tenants/{tenant_id}/{storage_name}",
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

    root = resolve_temp_storage_dir("attachments") / f"tenant-{tenant_id}"
    root.mkdir(parents=True, exist_ok=True)

    path = root / storage_name.replace("attachments/", "", 1)
    path.write_bytes(payload)
    return str(path)


def _local_path(storage_ref: str, *, tenant_id: int) -> Path:
    tenant_id = _require_tenant_id(tenant_id)
    root = resolve_temp_storage_dir("attachments").resolve()
    tenant_root = (root / f"tenant-{tenant_id}").resolve()
    if storage_ref.startswith("local://"):
        relative = storage_ref.removeprefix("local://")
        candidate = (root / relative).resolve()
    else:
        candidate = Path(storage_ref).expanduser().resolve()
    try:
        candidate.relative_to(tenant_root)
    except ValueError as exc:
        raise TenantStorageError("La referencia local no pertenece al tenant.") from exc
    return candidate


def read_attachment(storage_ref: str, *, tenant_id: int) -> bytes:
    _require_tenant_id(tenant_id)
    if storage_ref.startswith("s3://"):
        return _read_s3(storage_ref, tenant_id=tenant_id)
    if storage_ref.startswith(("https://", "http://")):
        if str(getattr(get_settings(), "environment", "") or "").strip().lower() in {"staging", "production"}:
            raise TenantStorageError("Las referencias HTTP legacy no están habilitadas en staging.")
        from vercel.blob import BlobClient

        client = BlobClient()
        try:
            result = client.get(storage_ref, access="private")
            return result.content
        finally:
            client.close()

    return _local_path(storage_ref, tenant_id=tenant_id).read_bytes()


def delete_attachment(storage_ref: str, *, tenant_id: int) -> None:
    tenant_id = _require_tenant_id(tenant_id)
    if storage_ref.startswith("s3://"):
        bucket, key = _parse_s3_reference(storage_ref, tenant_id=tenant_id)
        _s3_client().delete_object(Bucket=bucket, Key=key)
        return
    if storage_ref.startswith(("http://", "https://")):
        raise TenantStorageError("No se permite borrar referencias remotas legacy desde este adaptador.")
    path = _local_path(storage_ref, tenant_id=tenant_id)
    if path.exists():
        path.unlink()
