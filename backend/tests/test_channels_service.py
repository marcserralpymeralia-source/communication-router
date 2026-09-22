from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.channels.service import is_channel_enabled


class ChannelServiceTests(unittest.TestCase):
    def test_kibak_postgresql_email_does_not_require_legacy_channel_table(self):
        db = MagicMock()
        db.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        with patch(
            "app.channels.service.get_settings",
            return_value=SimpleNamespace(app_slug="kibak"),
        ):
            self.assertTrue(is_channel_enabled(db, 1, "email"))

        db.scalar.assert_not_called()

    def test_other_runtimes_keep_legacy_channel_gate(self):
        db = MagicMock()
        db.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        db.scalar.return_value = SimpleNamespace(is_active=False)

        with patch(
            "app.channels.service.get_settings",
            return_value=SimpleNamespace(app_slug="other"),
        ):
            self.assertFalse(is_channel_enabled(db, 1, "email"))

        db.scalar.assert_called_once()


if __name__ == "__main__":
    unittest.main()
