#!/usr/bin/env python3
"""Bounded local HTTP workload against an isolated disposable inventory service.

Includes genuine stock contention, expected HTTP 409 conflicts, and authenticated
product reads. It reports observations, not production capacity or an SLA.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import math
from pathlib import Path
import threading
import time

spec = importlib.util.spec_from_file_location("recovery_drill", Path(__file__).with_name("recovery-drill.py"))
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def percentile(values, percent):
    """Nearest-rank percentile; each value is one complete client-observed request."""
    return sorted(values)[max(0, math.ceil(len(values) * percent / 100) - 1)] if values else None


def summarize(samples):
    latencies = [sample["duration_ms"] for sample in samples]
    return {
        "requests": len(samples),
        "status_counts": dict(sorted(Counter(str(sample["status"]) for sample in samples).items())),
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95),
                       "p99": percentile(latencies, 99), "max": max(latencies) if latencies else None},
    }


def run_load(fixture, args, report):
    fixture.create_network()
    database = fixture.start_database("load-db")
    fixture.migrate_database("load-migrate", database)
    report["runtime_login"] = fixture.check_runtime_login(database, fixture.runtime_passwords[database])
    _, api = fixture.start_app("load-app", database)
    # Every test owns its own database; no records remain in the user's application.
    stock = 64
    product = api.expect("POST", "/api/v1/products", "admin", {
        "sku": "LOAD-" + fixture.run_id[:12].upper(), "name": "Disposable contention workload", "initialQuantity": stock,
    }, 201)
    product_path = "/api/v1/products/" + product["id"]
    for _ in range(8):
        api.expect("GET", product_path, "customer")

    # More attempts than stock ensures conflict handling is measured under load.
    reservations_target = max(stock + 1, args.requests * 5 // 9)
    jobs = ["reserve"] * reservations_target + ["read"] * (args.requests - reservations_target)
    cursor = 0
    lock = threading.Lock()
    samples = []
    started = time.monotonic()
    deadline = started + args.duration

    def worker():
        nonlocal cursor
        local_samples = []
        while True:
            with lock:
                if cursor >= len(jobs) or time.monotonic() >= deadline:
                    break
                index = cursor
                cursor += 1
            operation = jobs[index]
            request_started = time.monotonic()
            try:
                if operation == "reserve":
                    status, payload = api.request("POST", "/api/v1/reservations", "customer", {
                        "productId": product["id"], "quantity": 1,
                    }, timeout=3)
                else:
                    status, payload = api.request("GET", product_path, "customer", timeout=3)
                expected_conflict = (operation == "reserve" and status == 409 and
                                     payload.get("detail") == "Insufficient available stock")
                expected = (status == 201 or expected_conflict) if operation == "reserve" else status == 200
                error = None
            except Exception as exc:
                status, expected, expected_conflict, error = "transport_error", False, False, type(exc).__name__
            local_samples.append({
                "index": index, "operation": operation, "status": status, "expected": expected,
                "expected_stock_conflict": expected_conflict,
                "duration_ms": (time.monotonic() - request_started) * 1000, "error": error,
            })
        return local_samples

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(worker) for _ in range(args.concurrency)]
        for future in futures:
            samples.extend(future.result())
    elapsed = time.monotonic() - started
    samples.sort(key=lambda sample: sample["index"])
    summary = summarize(samples)
    summary.update({
        "elapsed_seconds": elapsed,
        "requests_per_second": len(samples) / elapsed,
        "successful_2xx": sum(isinstance(s["status"], int) and 200 <= s["status"] < 300 for s in samples),
        "expected_409_conflicts": sum(s["expected_stock_conflict"] for s in samples),
        "unexpected_errors": sum(not sample["expected"] for sample in samples),
        "by_operation": {operation: summarize([s for s in samples if s["operation"] == operation])
                         for operation in ("reserve", "read")},
    })
    report["workload"] = {"requested_requests": args.requests, "concurrency": args.concurrency,
        "request_issue_deadline_seconds": args.duration, "per_request_timeout_seconds": 3,
        "warmup_requests_excluded": 8, "initial_stock": stock,
        "phases": [{"operation": "reserve_one_unit", "requests": reservations_target},
                   {"operation": "read_product", "requests": args.requests - reservations_target}],
        "connection_policy": "new HTTP connection for each request", "percentile_method": "nearest rank",
        "clock": "time.monotonic", "setup_and_validation_excluded": True}
    report["results"] = summary
    report["samples"] = samples
    final_product = api.expect("GET", product_path, "customer")
    final_reservations = api.expect("GET", "/api/v1/reservations?size=100", "customer")
    successes = sum(s["operation"] == "reserve" and s["status"] == 201 for s in samples)
    report["invariants"] = {"successful_reservations": successes,
        "database_reservations_via_api": final_reservations["totalElements"],
        "remaining_stock": final_product["availableQuantity"],
        "no_overselling": successes <= stock and final_product["availableQuantity"] >= 0,
        "stock_accounting_matches": final_product["availableQuantity"] == stock - successes,
        "reservation_count_matches": final_reservations["totalElements"] == successes}
    drill.require(len(samples) == args.requests, "Workload deadline reached before all requested operations completed")
    drill.require(summary["unexpected_errors"] == 0, "Workload had unexpected HTTP or transport errors")
    drill.require(elapsed <= 60, "Measured workload exceeded the hard 60-second limit")
    drill.require(successes == stock and final_product["availableQuantity"] == 0,
                  "Expected exactly 64 reservations with zero available stock")
    drill.require(final_reservations["totalElements"] == successes, "API reservation count does not match successful writes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="inventory-cloud-service-app")
    parser.add_argument("--database-image", default="postgres:17-bookworm")
    parser.add_argument("--requests", type=int, default=288)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--duration", type=float, default=45,
                        help="Stop issuing work after this many seconds; setup is excluded (maximum 50)")
    parser.add_argument("--report", type=Path, default=drill.ROOT / "reports/load-latest.json")
    args = parser.parse_args()
    if not (100 <= args.requests <= 5000 and 1 <= args.concurrency <= 16 and 1 <= args.duration <= 50):
        parser.error("requests must be 100–5000, concurrency 1–16, duration 1–50 seconds")
    report = {"started_at": drill.utc_now(), "status": "failed", "drill": "bounded_local_contention_and_reads"}
    fixture = None
    try:
        fixture = drill.Fixture(args.image, args.database_image)
        report["environment"] = fixture.metadata()
        run_load(fixture, args, report)
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        report["cleanup_errors"] = fixture.cleanup() if fixture else []
        if report["cleanup_errors"]:
            report["status"] = "failed"
        report["finished_at"] = drill.utc_now()
        drill.write_report(args.report, report)
    print(f"{report['status'].upper()}: bounded load test; report: {args.report}")
    if "results" in report:
        result = report["results"]
        print(f"{result['requests']} requests, {result['requests_per_second']:.2f} req/s, "
              f"{result['expected_409_conflicts']} expected conflicts, {result['unexpected_errors']} unexpected errors")
    if "error" in report:
        print(report["error"])
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
