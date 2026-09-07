from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import statistics
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


ENDPOINT_TARGETS = [
    {"label": "home", "method": "GET", "path": "/"},
    {"label": "workbench", "method": "GET", "path": "/workbench"},
    {"label": "channels", "method": "GET", "path": "/channels?tab=processed&date_range=30d"},
    {"label": "orders", "method": "GET", "path": "/orders?date_range=90d"},
    {"label": "order_detail", "method": "GET", "path_template": "/orders/{order_id}"},
    {"label": "history", "method": "GET", "path": "/history?date_range=90d"},
    {"label": "customers", "method": "GET", "path": "/customers"},
    {"label": "products", "method": "GET", "path": "/products"},
    {"label": "imports", "method": "GET", "path": "/imports/quick"},
    {"label": "settings", "method": "GET", "path": "/settings"},
    {"label": "jobs", "method": "GET", "path": "/jobs/monitor"},
    {"label": "alerts", "method": "GET", "path": "/alerts"},
    {"label": "logs", "method": "GET", "path": "/logs"},
    {"label": "admin_diagnostics", "method": "GET", "path": "/admin/diagnostics"},
]


@dataclass(slots=True)
class EndpointSummary:
    endpoint: str
    method: str
    runs: int
    median_duration_ms: float
    min_duration_ms: float
    max_duration_ms: float
    sql_duration_ms: float
    sql_query_count: int
    sql_duplicate_count: int
    template_duration_ms: float
    response_size_bytes: int
    loaded_record_count: int
    displayed_item_count: int
    status_code: int
    notes: str
    raw_runs: list[dict[str, Any]]


def _prepare_import_environment() -> None:
    bootstrap_root = Path(tempfile.mkdtemp(prefix="kibak-performance-bootstrap-"))
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
    os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
    os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")
    os.environ.setdefault("MASTER_DATABASE_URL", f"sqlite:///{(bootstrap_root / 'master.sqlite').as_posix()}")
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{(bootstrap_root / 'tenant.sqlite').as_posix()}")


def _response_metric(response, header_name: str, fallback: float = 0.0) -> float:
    raw_value = response.headers.get(header_name)
    if raw_value is None:
        return fallback
    try:
        return float(raw_value)
    except ValueError:
        return fallback


def _response_metric_int(response, header_name: str, fallback: int = 0) -> int:
    raw_value = response.headers.get(header_name)
    if raw_value is None:
        return fallback
    try:
        return int(float(raw_value))
    except ValueError:
        return fallback


def _warmup_request(client, method: str, path: str) -> None:
    if method == "GET":
        client.get(path)
        return
    raise ValueError(f"Metodo no soportado para benchmark: {method}")


def _execute_request(client, method: str, path: str) -> tuple[float, dict[str, Any], int]:
    started_at = perf_counter()
    if method == "GET":
        response = client.get(path)
    else:
        raise ValueError(f"Metodo no soportado para benchmark: {method}")
    duration_ms = (perf_counter() - started_at) * 1000
    payload = {
        "duration_ms": round(_response_metric(response, "X-Perf-Total-Ms", duration_ms), 2),
        "sql_duration_ms": round(_response_metric(response, "X-Perf-SQL-Duration-Ms"), 2),
        "sql_query_count": _response_metric_int(response, "X-Perf-SQL-Count"),
        "sql_duplicate_count": _response_metric_int(response, "X-Perf-SQL-Duplicate-Count"),
        "template_duration_ms": round(_response_metric(response, "X-Perf-Template-Ms"), 2),
        "response_size_bytes": _response_metric_int(response, "X-Perf-Response-Size-Bytes", len(response.content or b"")),
        "loaded_record_count": _response_metric_int(response, "X-Perf-Loaded-Records"),
        "displayed_item_count": _response_metric_int(response, "X-Perf-Displayed-Items"),
        "status_code": response.status_code,
    }
    return payload["duration_ms"], payload, response.status_code


def _median(values: list[float]) -> float:
    return round(float(statistics.median(values)), 2) if values else 0.0


def _summarize_endpoint(client, fixture, target: dict[str, str], runs: int) -> EndpointSummary:
    path = target.get("path_template")
    if path:
        path = path.format(order_id=fixture.order_ids[0])
    else:
        path = target["path"]

    _warmup_request(client, target["method"], path)
    raw_runs: list[dict[str, Any]] = []
    duration_values: list[float] = []
    for _ in range(runs):
        duration_ms, payload, _status_code = _execute_request(client, target["method"], path)
        raw_runs.append(payload)
        duration_values.append(duration_ms)

    sql_duration = _median([run["sql_duration_ms"] for run in raw_runs])
    sql_query_count = int(statistics.median([run["sql_query_count"] for run in raw_runs])) if raw_runs else 0
    sql_duplicate_count = int(statistics.median([run["sql_duplicate_count"] for run in raw_runs])) if raw_runs else 0
    template_duration = _median([run["template_duration_ms"] for run in raw_runs])
    response_size = int(statistics.median([run["response_size_bytes"] for run in raw_runs])) if raw_runs else 0
    loaded_records = int(statistics.median([run["loaded_record_count"] for run in raw_runs])) if raw_runs else 0
    displayed_items = int(statistics.median([run["displayed_item_count"] for run in raw_runs])) if raw_runs else 0
    status_code = int(statistics.median([run["status_code"] for run in raw_runs])) if raw_runs else 200
    notes: list[str] = ["warm-up excluded"]
    if target["label"] in {"workbench", "channels", "jobs", "alerts", "logs"}:
        notes.append("operational view")
    if target["label"] in {"orders", "history", "customers", "products", "settings"}:
        notes.append("list view")
    if target["label"] in {"order_detail"}:
        notes.append("detail view")
    return EndpointSummary(
        endpoint=path,
        method=target["method"],
        runs=runs,
        median_duration_ms=_median(duration_values),
        min_duration_ms=round(min(duration_values), 2) if duration_values else 0.0,
        max_duration_ms=round(max(duration_values), 2) if duration_values else 0.0,
        sql_duration_ms=sql_duration,
        sql_query_count=sql_query_count,
        sql_duplicate_count=sql_duplicate_count,
        template_duration_ms=template_duration,
        response_size_bytes=response_size,
        loaded_record_count=loaded_records,
        displayed_item_count=displayed_items,
        status_code=status_code,
        notes=", ".join(notes),
        raw_runs=raw_runs,
    )


def run_benchmark(scenario: str, *, runs: int = 5, output_dir: Path | None = None) -> dict[str, Any]:
    _prepare_import_environment()
    from scripts.performance_data import build_performance_fixture, performance_test_client

    fixture = build_performance_fixture(scenario)
    results_dir = output_dir or Path(__file__).resolve().parents[1] / "performance-results"
    results_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    summaries: list[EndpointSummary] = []
    started_at = perf_counter()
    with performance_test_client(fixture) as client:
        for target in ENDPOINT_TARGETS:
            summaries.append(_summarize_endpoint(client, fixture, target, runs))
    elapsed_ms = round((perf_counter() - started_at) * 1000, 2)

    payload = {
        "scenario": scenario,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": elapsed_ms,
        "counts": fixture.counts,
        "results": [asdict(summary) for summary in summaries],
    }
    json_path = results_dir / f"performance-baseline-{scenario}-{timestamp}.json"
    csv_path = results_dir / f"performance-baseline-{scenario}-{timestamp}.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "endpoint",
                "method",
                "runs",
                "median_duration_ms",
                "min_duration_ms",
                "max_duration_ms",
                "sql_duration_ms",
                "sql_query_count",
                "sql_duplicate_count",
                "template_duration_ms",
                "response_size_bytes",
                "loaded_record_count",
                "displayed_item_count",
                "status_code",
                "notes",
            ],
        )
        writer.writeheader()
        for summary in summaries:
            writer.writerow({key: getattr(summary, key) for key in writer.fieldnames})

    slowest = sorted(summaries, key=lambda item: item.median_duration_ms, reverse=True)[:5]
    print(f"Scenario: {scenario}")
    print(f"Results JSON: {json_path}")
    print(f"Results CSV:  {csv_path}")
    print("Top 5 endpoints:")
    for index, item in enumerate(slowest, start=1):
        print(
            f"  {index}. {item.endpoint} -> {item.median_duration_ms:.2f} ms, "
            f"{item.sql_query_count} queries, {item.response_size_bytes} bytes",
        )

    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure Anchi performance baseline")
    parser.add_argument("--scenario", choices=sorted({"small", "medium", "large"}), required=True)
    parser.add_argument("--runs", type=int, default=5, help="Number of measured executions per endpoint")
    parser.add_argument("--output-dir", type=Path, default=None, help="Optional output directory for generated results")
    args = parser.parse_args(argv)
    run_benchmark(args.scenario, runs=max(1, args.runs), output_dir=args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
