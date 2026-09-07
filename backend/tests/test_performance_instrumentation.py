from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

_bootstrap_root = Path(tempfile.gettempdir()) / "kibak-performance-tests"
_bootstrap_root.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MASTER_DATABASE_URL", f"sqlite:///{(_bootstrap_root / 'master.sqlite').as_posix()}")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(_bootstrap_root / 'tenant.sqlite').as_posix()}")

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.performance import (  # noqa: E402
    PerformanceCollector,
    current_performance,
    normalize_sql_statement,
    performance_scope,
    record_template_render,
    start_performance,
)
from scripts.measure_performance import run_benchmark  # noqa: E402
from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


@contextmanager
def temp_env(**updates):
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update({key: value for key, value in updates.items() if value is not None})
    get_settings.cache_clear()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


class PerformanceInstrumentationTests(unittest.TestCase):
    def test_profiling_disabled_returns_none(self):
        with temp_env(PERFORMANCE_PROFILING_ENABLED="false", ENABLE_PERFORMANCE_PROFILING="false"):
            self.assertIsNone(start_performance(request_id="req-1", endpoint="/", method="GET"))

    def test_sql_counting_duplicate_detection_and_sanitizing(self):
        collector = PerformanceCollector(request_id="req-1", endpoint="/orders", method="GET")
        engine = create_engine("sqlite:///:memory:")
        with performance_scope(collector):
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                connection.execute(text("SELECT 1"))
                connection.execute(text("SELECT 'secret@example.com', 42"))

        payload = collector.to_dict()
        self.assertEqual(payload["sql_query_count"], 3)
        self.assertEqual(payload["sql_duplicate_count"], 1)
        self.assertGreaterEqual(payload["sql_duration_ms"], 0)
        self.assertTrue(payload["sql_top_queries"])
        repeated_statement = payload["sql_top_queries"][0]["statement"]
        self.assertIn("select", repeated_statement)
        self.assertNotIn("secret@example.com", repeated_statement)
        normalized = normalize_sql_statement("SELECT * FROM orders WHERE id = 42 AND email = 'secret@example.com'")
        self.assertNotIn("secret@example.com", normalized)
        self.assertNotIn("42", normalized)
        self.assertIn("?", normalized)

    def test_template_render_and_response_size_are_recorded_and_context_resets(self):
        collector = PerformanceCollector(request_id="req-2", endpoint="/", method="GET")
        with performance_scope(collector):
            record_template_render("demo.html", {"items": [1, 2, 3], "request": object()}, 0.012)
            collector.record_response_size(2048)
            self.assertIs(current_performance(), collector)
        self.assertIsNone(current_performance())
        payload = collector.to_dict()
        self.assertGreater(payload["template_duration_ms"], 0)
        self.assertEqual(payload["response_size_bytes"], 2048)
        self.assertEqual(payload["loaded_record_count"], 3)
        self.assertEqual(payload["displayed_item_count"], 3)

    def test_collectors_do_not_mix_between_scopes(self):
        collector_a = PerformanceCollector(request_id="req-a", endpoint="/a", method="GET")
        collector_b = PerformanceCollector(request_id="req-b", endpoint="/b", method="GET")
        engine = create_engine("sqlite:///:memory:")
        with performance_scope(collector_a):
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        with performance_scope(collector_b):
            with engine.connect() as connection:
                connection.execute(text("SELECT 2"))
                connection.execute(text("SELECT 2"))
        self.assertEqual(collector_a.sql_query_count, 1)
        self.assertEqual(collector_b.sql_query_count, 2)
        self.assertEqual(collector_a.sql_duplicate_count, 0)
        self.assertEqual(collector_b.sql_duplicate_count, 1)

    def test_fixture_generation_is_temp_based_and_reproducible(self):
        fixture_a = build_performance_fixture("small")
        fixture_b = build_performance_fixture("small")
        try:
            self.assertTrue(fixture_a.master_path.parent.name.startswith("tmp") or fixture_a.master_path.parent == Path(tempfile.gettempdir()))
            self.assertTrue(fixture_b.tenant_path.parent.exists())
            self.assertEqual(fixture_a.counts, fixture_b.counts)
            self.assertGreater(fixture_a.counts["customers"], 0)
            self.assertGreater(fixture_a.counts["products"], 0)
            self.assertGreater(fixture_a.counts["orders"], 0)
        finally:
            fixture_a.cleanup()
            fixture_b.cleanup()

    def test_profiling_headers_are_emitted_on_real_request(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("X-Perf-SQL-Count", response.headers)
            self.assertIn("X-Perf-Template-Ms", response.headers)
            self.assertIn("X-Perf-Response-Size-Bytes", response.headers)
            self.assertGreaterEqual(int(response.headers["X-Perf-SQL-Count"]), 1)
        finally:
            fixture.cleanup()

    def test_channels_processed_page_handles_timezone_normalization(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/channels?tab=processed&date_range=30d")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("internal_error", response.text.lower())
        finally:
            fixture.cleanup()

    def test_history_page_loads_with_scoring_settings_present(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/history?date_range=90d")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("internal_error", response.text.lower())
            self.assertIn("X-Perf-SQL-Count", response.headers)
            self.assertLess(int(response.headers["X-Perf-SQL-Count"]), 25)
        finally:
            fixture.cleanup()

    def test_benchmark_script_writes_json_and_csv(self):
        with tempfile.TemporaryDirectory() as output_dir:
            payload = run_benchmark("small", runs=1, output_dir=Path(output_dir))
            files = list(Path(output_dir).glob("performance-baseline-small-*.json"))
            csv_files = list(Path(output_dir).glob("performance-baseline-small-*.csv"))
            self.assertTrue(files)
            self.assertTrue(csv_files)
            self.assertEqual(payload["scenario"], "small")
            self.assertEqual(len(payload["results"]), 14)
            self.assertIn("counts", payload)


if __name__ == "__main__":
    unittest.main()
