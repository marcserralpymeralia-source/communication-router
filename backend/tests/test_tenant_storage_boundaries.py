from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.attachment_storage import TenantStorageError, delete_attachment, read_attachment, save_attachment
from app.settings.branding import branding_asset_storage_ref, branding_asset_url, is_internal_brand_asset, store_brand_asset


class TenantStorageTests(unittest.TestCase):
    def test_local_storage_requires_tenant_and_rejects_cross_tenant_access(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(os.environ, {"STORAGE_BACKEND": "local"}, clear=False), patch(
            "app.core.attachment_storage.resolve_temp_storage_dir", return_value=Path(tempdir) / "attachments"
        ):
            reference = save_attachment(tenant_id=10, filename="../invoice.txt", payload=b"tenant-a")
            self.assertIn("tenant-10", reference)
            self.assertEqual(read_attachment(reference, tenant_id=10), b"tenant-a")
            with self.assertRaises(TenantStorageError):
                read_attachment(reference, tenant_id=11)
            with self.assertRaises(TenantStorageError):
                delete_attachment(reference, tenant_id=11)
            delete_attachment(reference, tenant_id=10)
            self.assertFalse(Path(reference).exists())

    def test_s3_namespace_and_delete_are_tenant_scoped(self):
        settings = SimpleNamespace(
            storage_backend="s3",
            s3_bucket="private-bucket",
            s3_prefix="kibak",
            s3_endpoint_url="https://objects.example.test",
            s3_region="auto",
            s3_access_key_id="access",
            s3_secret_access_key=SimpleNamespace(get_secret_value=lambda: "secret"),
        )
        calls: list[tuple[str, str]] = []

        class Body:
            def read(self):
                return b"tenant-a"

            def close(self):
                return None

        class Client:
            def put_object(self, **kwargs):
                calls.append(("put", kwargs["Key"]))

            def get_object(self, **kwargs):
                calls.append(("get", kwargs["Key"]))
                return {"Body": Body()}

            def delete_object(self, **kwargs):
                calls.append(("delete", kwargs["Key"]))

        with patch("app.core.attachment_storage.get_settings", return_value=settings), patch(
            "app.core.attachment_storage._s3_client", return_value=Client()
        ):
            reference = save_attachment(tenant_id=10, filename="invoice.txt", payload=b"tenant-a")
            self.assertIn("/kibak/tenants/10/", reference)
            self.assertEqual(read_attachment(reference, tenant_id=10), b"tenant-a")
            with self.assertRaises(TenantStorageError):
                read_attachment(reference, tenant_id=11)
            with self.assertRaises(TenantStorageError):
                read_attachment("s3://private-bucket/kibak/tenants/10/../11/file", tenant_id=10)
            delete_attachment(reference, tenant_id=10)
        self.assertEqual([kind for kind, _ in calls], ["put", "get", "delete"])

    def test_missing_tenant_fails_closed(self):
        with self.assertRaises(TenantStorageError):
            save_attachment(tenant_id=None, filename="file.txt", payload=b"data")
        with self.assertRaises(TenantStorageError):
            read_attachment("/tmp/file.txt", tenant_id=None)


class BrandingStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_branding_upload_uses_tenant_storage_reference(self):
        class Upload:
            filename = "logo.svg"
            content_type = "image/svg+xml"

            async def read(self):
                return b"<svg></svg>"

        with patch("app.settings.branding.save_attachment", return_value="/tmp/attachments/tenant-10/logo.svg") as save:
            value = await store_brand_asset(10, Upload(), "logo-main")
        save.assert_called_once()
        self.assertEqual(branding_asset_storage_ref(value), "/tmp/attachments/tenant-10/logo.svg")
        self.assertTrue(is_internal_brand_asset(value))
        self.assertFalse(is_internal_brand_asset("https://cdn.example/logo.svg"))
        self.assertIn("/settings/branding/assets?ref=", branding_asset_url("/tmp/attachments/tenant-10/logo.svg"))


if __name__ == "__main__":
    unittest.main()
