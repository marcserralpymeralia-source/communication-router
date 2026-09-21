from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("APP_ENV", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import run_mailbox_backfill_once as runner  # noqa: E402
from app.settings.integrations import _update_sync_checkpoint  # noqa: E402


class OneShotMailboxBackfillTests(unittest.TestCase):
    def test_production_confirmation_is_required_before_any_run(self):
        with patch.object(runner, "run_backfill_once") as run:
            status = runner.main(["--company-slug", "kibak-pilot", "--since", "2026-09-14"])
        self.assertEqual(status, 2)
        run.assert_not_called()

    def test_canonical_call_is_unbounded_and_does_not_enable_processing(self):
        mailbox = SimpleNamespace(auto_process_on_fetch=False)
        with patch.object(runner, "backfill_imap_emails", return_value={"ok": True, "routing_jobs_enqueued": 0}) as backfill:
            result = runner._execute_canonical_backfill(
                object(),
                mailbox,
                1,
                since="2026-09-14",
                to="2026-09-21",
                sync_state=object(),
                master_db=object(),
                mailbox_id=2,
            )
        self.assertTrue(result["ok"])
        call = backfill.call_args
        self.assertIsNone(call.kwargs["limit"])
        self.assertEqual(call.kwargs["batch_size"], 25)
        self.assertFalse(call.kwargs["resume"])
        self.assertFalse(call.kwargs["stop_after_batch"])
        self.assertTrue(call.kwargs["unbounded"])
        self.assertTrue(call.kwargs["preserve_normal_cursor"])
        self.assertEqual(call.kwargs["mailbox_id"], 2)
        self.assertEqual(result["routing_jobs_enqueued"], 0)

    def test_pilot_checkpoint_preserves_normal_sync_cursor(self):
        state = SimpleNamespace(
            mailbox=None,
            uidvalidity=None,
            source_provider=None,
            source_host=None,
            source_username=None,
            source_connected_email=None,
            last_seen_uid="10",
            last_checkpoint_uid="10",
            last_sync_at=None,
            sync_status="idle",
            status="idle",
            backfill_status="idle",
            backfill_started_at=None,
            last_success_at=None,
            last_successful_sync_at=None,
            last_error_at=None,
            last_error_message=None,
            last_error_type=None,
            backfill_completed_at=None,
            backfill_processed=0,
            backfill_total=0,
            backfill_created=0,
            backfill_duplicates=0,
            backfill_errors=0,
            backfill_last_uid=None,
            backfill_last_checkpoint_at=None,
            backfill_checkpoint_json=None,
        )
        session = SimpleNamespace(commit=lambda: None)
        _update_sync_checkpoint(
            state,
            session,
            mailbox="INBOX",
            uidvalidity="7",
            last_uid="20",
            saved=1,
            duplicates=0,
            attachments_saved=0,
            found=1,
            status="idle",
            preserve_normal_cursor=True,
        )
        self.assertEqual(state.last_seen_uid, "10")
        self.assertEqual(state.last_checkpoint_uid, "20")
        self.assertEqual(state.backfill_last_uid, "20")

    def test_disabled_mailbox_is_allowed_without_mutating_safety_flags(self):
        mailbox = SimpleNamespace(
            provider="microsoft365",
            connection_method="oauth2",
            refresh_token_encrypted="ciphertext",
            enabled=False,
            auto_sync_enabled=False,
            mark_as_read_after_import=False,
            smtp_enabled=False,
            move_after_processing=False,
            auto_process_on_fetch=False,
        )
        runner.validate_mailbox_safety(mailbox)
        self.assertFalse(mailbox.enabled)
        self.assertFalse(mailbox.auto_sync_enabled)
        self.assertFalse(mailbox.mark_as_read_after_import)

    def test_unsafe_mailbox_is_rejected_instead_of_repaired(self):
        mailbox = SimpleNamespace(
            provider="microsoft365",
            connection_method="oauth2",
            refresh_token_encrypted="ciphertext",
            enabled=True,
            auto_sync_enabled=False,
            mark_as_read_after_import=False,
            smtp_enabled=False,
            move_after_processing=False,
            auto_process_on_fetch=False,
        )
        with self.assertRaises(RuntimeError):
            runner.validate_mailbox_safety(mailbox)
        self.assertTrue(mailbox.enabled)

    def test_storage_reference_requires_tenant_prefix(self):
        self.assertTrue(
            runner._storage_reference_is_tenant_scoped(
                "s3://private/kibak/tenants/1/attachments/item.txt", 1, "kibak"
            )
        )
        self.assertFalse(
            runner._storage_reference_is_tenant_scoped(
                "s3://private/kibak/tenants/1/attachments/item.txt", 2, "kibak"
            )
        )


if __name__ == "__main__":
    unittest.main()
