from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth.client_ip import resolve_client_ip
from app.auth.throttle import AuthThrottleStore
from app.master.models import AuthThrottle, MasterBase


class AuthThrottleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tempdir.name, 'master.sqlite')}")
        MasterBase.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.settings = SimpleNamespace(auth_secret="test-throttle-secret", trusted_proxy_ips=[])

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def request(self, *, peer: str = "203.0.113.10", forwarded: str = ""):
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": forwarded})

    def test_distributed_subject_limit_is_persistent_and_does_not_store_email(self):
        request = self.request()
        with patch("app.auth.throttle.get_settings", return_value=self.settings), patch(
            "app.auth.client_ip.get_settings", return_value=self.settings
        ):
            db = self.Session()
            try:
                store = AuthThrottleStore(db, request)
                for _ in range(8):
                    store.record_failure("Pilot@Example.com")
                    db.commit()
                decision = store.check("pilot@example.com")
                rows = db.scalars(select(AuthThrottle)).all()
                self.assertFalse(decision.allowed)
                self.assertTrue(rows)
                self.assertNotIn("pilot@example.com", {row.key_hash for row in rows})
            finally:
                db.close()

    def test_success_resets_identity_but_does_not_clear_ip_budget(self):
        request = self.request()
        with patch("app.auth.throttle.get_settings", return_value=self.settings), patch(
            "app.auth.client_ip.get_settings", return_value=self.settings
        ):
            db = self.Session()
            try:
                store = AuthThrottleStore(db, request)
                store.record_failure("pilot@example.com")
                db.commit()
                store.record_success("pilot@example.com")
                db.commit()
                scopes = {row.scope for row in db.scalars(select(AuthThrottle)).all()}
                self.assertEqual(scopes, {"ip"})
            finally:
                db.close()

    def test_forwarded_for_is_ignored_unless_peer_is_trusted(self):
        request = self.request(peer="203.0.113.10", forwarded="198.51.100.20")
        with patch("app.auth.client_ip.get_settings", return_value=self.settings):
            self.assertEqual(resolve_client_ip(request), "203.0.113.10")
        trusted = SimpleNamespace(trusted_proxy_ips=["203.0.113.0/24"])
        with patch("app.auth.client_ip.get_settings", return_value=trusted):
            self.assertEqual(resolve_client_ip(request), "198.51.100.20")


if __name__ == "__main__":
    unittest.main()
