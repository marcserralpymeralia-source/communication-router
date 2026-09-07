from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ImportStorageTests(unittest.TestCase):
    def test_resolve_temp_storage_dir_prefers_configured_and_vercel_roots(self):
        from app.core.storage import resolve_temp_storage_dir

        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": "/custom/anchi", "VERCEL": ""}, clear=False):
            self.assertEqual(resolve_temp_storage_dir("import_previews"), Path("/custom/anchi/import_previews"))

        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": "", "VERCEL": "1"}, clear=False):
            self.assertEqual(resolve_temp_storage_dir("import_previews"), Path("/tmp/kibak/import_previews"))

        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": "", "VERCEL": ""}, clear=False):
            expected = Path(__file__).resolve().parents[1] / "storage" / "import_previews"
            self.assertEqual(resolve_temp_storage_dir("import_previews"), expected)

    def test_imports_service_does_not_create_preview_dir_during_import(self):
        module_name = "app.imports.service"
        previous = sys.modules.pop(module_name, None)
        try:
            with patch.dict(os.environ, {"APP_ENV": "test", "VERCEL": "1", "TEMP_STORAGE_DIR": ""}, clear=False), patch(
                "pathlib.Path.mkdir",
                side_effect=AssertionError("mkdir should not be called during import"),
            ):
                module = importlib.import_module(module_name)
                self.assertEqual(str(module.PREVIEW_DIR), "/tmp/kibak/import_previews")
        finally:
            sys.modules.pop(module_name, None)
            if previous is not None:
                sys.modules[module_name] = previous

    def test_read_attachment_uses_blob_client_for_http_urls(self):
        from app.core.attachment_storage import read_attachment

        calls: dict[str, object] = {}

        class FakeBlobClient:
            def __init__(self) -> None:
                calls["init"] = True

            def get(self, storage_ref: str, access: str | None = None):  # noqa: ANN001
                calls["storage_ref"] = storage_ref
                calls["access"] = access
                return SimpleNamespace(content=b"blob-bytes")

            def close(self) -> None:
                calls["closed"] = True

        vercel_module = ModuleType("vercel")
        blob_module = ModuleType("vercel.blob")
        blob_module.BlobClient = FakeBlobClient
        vercel_module.blob = blob_module

        with patch.dict(sys.modules, {"vercel": vercel_module, "vercel.blob": blob_module}):
            content = read_attachment("https://blob.example.com/attachments/pedido.pdf")

        self.assertEqual(content, b"blob-bytes")
        self.assertTrue(calls["init"])
        self.assertEqual(calls["storage_ref"], "https://blob.example.com/attachments/pedido.pdf")
        self.assertEqual(calls["access"], "private")
        self.assertTrue(calls["closed"])

    def test_save_attachment_rejects_ephemeral_vercel_storage(self):
        from app.core.attachment_storage import save_attachment

        with patch.dict(
            os.environ,
            {"VERCEL": "1", "BLOB_READ_WRITE_TOKEN": "", "BLOB_STORE_ID": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "Persistent attachment storage"):
                save_attachment(filename="pedido.txt", payload=b"pedido", content_type="text/plain")


if __name__ == "__main__":
    unittest.main()
