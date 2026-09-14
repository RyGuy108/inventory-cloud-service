#!/usr/bin/env python3
"""Check shared-database correctness and one-replica restart in disposable local containers."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import threading
import time

spec = importlib.util.spec_from_file_location("inventory_replica_fixture", Path(__file__).with_name("recovery-drill.py"))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
docker, require = recovery.docker, recovery.require


class ReplicaFixture(recovery.Fixture):
    """Add a separate runtime allowlist before any stop/start operation."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.replicas = {}

    def start_replica(self, role, database):
        require(role in ("replica-a", "replica-b"), "Only the two fixed replica roles are allowed")
        name, api = self.start_app(role, database)
        self.replicas[name] = database
        self.inspect_replica(name, running=True)
        return name, api

    def inspect_replica(self, name, running):
        require(name in self.replicas, "Refusing operation on an untracked runtime replica")
        item = self.owned("container", name)
        environment = dict(entry.split("=", 1) for entry in item["Config"].get("Env", []) if "=" in entry)
        require(item.get("Image") == self.image, "Replica image identity changed")
        require(environment.get("SPRING_PROFILES_ACTIVE") == "local"
                and environment.get("DB_USERNAME") == "inventory_app"
                and environment.get("SPRING_FLYWAY_ENABLED") == "false"
                and environment.get("DB_URL") == (
                    f"jdbc:postgresql://{self.replicas[name]}:5432/inventory?socketTimeout=5&connectTimeout=3"),
                "Replica is not the expected restricted local runtime")
        require(set(item["NetworkSettings"].get("Networks", {})) == {self.network},
                "Replica is attached to an unexpected network")
        require(item["State"].get("Running") is running,
                "Replica running state differs from the expected lifecycle state")
        return item

    def api_for_replica(self, name):
        item = self.inspect_replica(name, running=True)
        bindings = item["NetworkSettings"]["Ports"].get("8080/tcp", [])
        require(len(bindings) == 1 and bindings[0].get("HostIp") == "127.0.0.1",
                "Replica API must have exactly one loopback-only binding")
        port = int(bindings[0]["HostPort"])
        require(1 <= port <= 65535, "Replica API port is outside the valid range")
        return recovery.Api(port, self.passwords)

    def stop_replica(self, name):
        self.inspect_replica(name, running=True)
        self.stop_container(name)
        self.inspect_replica(name, running=False)

    def restart_replica(self, name):
        before = self.inspect_replica(name, running=False)
        docker("container", "start", name)
        after = self.inspect_replica(name, running=True)
        require(after["Id"] == before["Id"], "Restart replaced the tracked container")
        api = self.api_for_replica(name)
        api.wait_health()
        return api


def concurrent_batch(apis, operation, count=8):
    require(len(apis) == 2, "Concurrency verification requires exactly two replicas")
    require(type(count) is int and 2 <= count <= 16 and count % 2 == 0,
            "Request batch must be an even integer from 2 to 16")
    barrier = threading.Barrier(count, timeout=10)

    def worker(index):
        barrier.wait()
        started = time.monotonic()
        try:
            status, payload = operation(apis[index % 2], index)
        except Exception as exc:
            raise RuntimeError(f"Replica request {index} failed: {type(exc).__name__}") from None
        return {"index": index, "replica": index % 2, "status": status, "payload": payload,
                "duration_seconds": time.monotonic() - started}

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(worker, range(count)))


def summarize_batch(samples):
    return {"requests": len(samples),
            "status_counts": dict(sorted(Counter(str(sample["status"]) for sample in samples).items())),
            "requests_by_replica": dict(sorted(Counter(str(sample["replica"]) for sample in samples).items())),
            "max_request_seconds": max(sample["duration_seconds"] for sample in samples)}


def require_same_reservation(samples, expected_status_counts, expected=None):
    require(Counter(sample["status"] for sample in samples) == Counter(expected_status_counts),
            "Concurrent requests returned unexpected HTTP status counts")
    reference = samples[0]["payload"] if expected is None else expected
    require(isinstance(reference, dict) and bool(reference.get("id")), "Reservation response has no identity")
    require(all(sample["payload"] == reference for sample in samples),
            "Concurrent requests did not return identical reservation fields")
    return reference


def require_stock_conflicts(samples, successes):
    require(Counter(sample["status"] for sample in samples) == Counter({201: successes, 409: len(samples) - successes}),
            "Contending replicas returned unexpected success/conflict counts")
    require(all(isinstance(sample["payload"], dict)
                and sample["payload"].get("detail") == "Insufficient available stock"
                for sample in samples if sample["status"] == 409),
            "A non-stock conflict cannot establish that overselling was prevented")
    created = [sample["payload"] for sample in samples if sample["status"] == 201]
    require(len({reservation["id"] for reservation in created}) == successes,
            "Distinct successful reservation requests did not produce distinct records")
    return created


def verify_stock(apis, product, quantity):
    path = "/api/v1/products/" + product["id"]
    snapshots = [api.expect("GET", path, "customer") for api in apis]
    require(snapshots[0] == snapshots[1] and snapshots[0]["availableQuantity"] == quantity,
            "Replicas disagree with expected shared stock")


def run_drill(fixture, report):
    fixture.create_network()
    database = fixture.start_database("replica-db")
    fixture.migrate_database("replica-migrate", database)
    report["runtime_login"] = fixture.check_runtime_login(database, fixture.runtime_passwords[database])
    first_name, first = fixture.start_replica("replica-a", database)
    second_name, second = fixture.start_replica("replica-b", database)
    apis = [first, second]
    replicas = [fixture.inspect_replica(name, running=True) for name in (first_name, second_name)]
    require(replicas[0]["Id"] != replicas[1]["Id"] and first.port != second.port,
            "Verification requires two separate application containers and API addresses")
    report["topology"] = {"replicas": 2, "distinct_container_ids": [item["Id"] for item in replicas],
                          "shared_database": True, "direct_loopback_api_ports": [api.port for api in apis],
                          "routing": "client alternates requests directly between containers; no load balancer"}
    product = first.expect("POST", "/api/v1/products", "admin", {
        "sku": "REPLICAS-" + fixture.run_id[:12].upper(), "name": "Disposable shared retry test", "initialQuantity": 20,
    }, 201)
    body = {"productId": product["id"], "quantity": 5}
    key = "replica-same-key-" + fixture.run_id
    samples = concurrent_batch(apis, lambda api, _: api.request(
        "POST", "/api/v1/reservations", "customer", body, timeout=12, idempotency_key=key))
    report["same_key_creation"] = summarize_batch(samples)
    reservation = require_same_reservation(samples, {201: 1, 200: 7})
    require(reservation["quantity"] == 5 and reservation["status"] == "ACTIVE", "Created reservation fields are wrong")
    verify_stock(apis, product, 15)
    report["same_key_creation"].update({"distinct_reservations": 1, "available_stock": 15})

    contended = second.expect("POST", "/api/v1/products", "admin", {
        "sku": "CONTEND-" + fixture.run_id[:12].upper(), "name": "Disposable cross-replica stock test", "initialQuantity": 7,
    }, 201)
    samples = concurrent_batch(apis, lambda api, index: api.request(
        "POST", "/api/v1/reservations", "customer", {"productId": contended["id"], "quantity": 2},
        timeout=12, idempotency_key=f"replica-stock-{fixture.run_id}-{index}"))
    report["stock_contention"] = summarize_batch(samples)
    contended_reservations = require_stock_conflicts(samples, successes=3)
    verify_stock(apis, contended, 1)
    report["stock_contention"].update({"initial_stock": 7, "units_per_attempt": 2,
                                       "reserved_units": 6, "available_stock": 1, "overselling": False})

    cancel_path = "/api/v1/reservations/" + reservation["id"] + "/cancel"
    samples = concurrent_batch(apis, lambda api, _: api.request("POST", cancel_path, "customer", {}, timeout=12))
    report["concurrent_cancellation"] = summarize_batch(samples)
    cancelled = require_same_reservation(samples, {200: 8})
    require(cancelled["status"] == "CANCELLED" and cancelled["id"] == reservation["id"],
            "Concurrent cancellation changed the reservation identity or did not cancel it")
    verify_stock(apis, product, 20)
    report["concurrent_cancellation"].update({"restored_units": 5, "available_stock": 20})

    active_body = {"productId": product["id"], "quantity": 4}
    active_key = "replica-active-" + fixture.run_id
    active = first.expect("POST", "/api/v1/reservations", "customer", active_body, 201, idempotency_key=active_key)
    verify_stock(apis, product, 16)
    started = time.monotonic()
    fixture.stop_replica(first_name)
    stopped_after = time.monotonic() - started
    second.wait_health(timeout=10)
    require(second.expect("POST", "/api/v1/reservations", "customer", body, idempotency_key=key) == cancelled,
            "Surviving replica forgot the cancelled reservation's retry key")
    require(second.expect("POST", "/api/v1/reservations", "customer", active_body, idempotency_key=active_key) == active,
            "Surviving replica forgot the active reservation's retry key")
    survivor_body = {"productId": product["id"], "quantity": 1}
    survivor_key = "replica-survivor-" + fixture.run_id
    survivor = second.expect("POST", "/api/v1/reservations", "customer", survivor_body, 201,
                             idempotency_key=survivor_key)
    survivor_stock = second.expect("GET", "/api/v1/products/" + product["id"], "customer")["availableQuantity"]
    require(survivor_stock == 15, "Surviving replica did not accept and retain a new reservation")
    require(fixture.inspect_replica(first_name, running=False)["State"]["Status"] == "exited",
            "Other replica was not stopped during the surviving-replica checks")
    restarted_at = time.monotonic()
    first = fixture.restart_replica(first_name)
    report["restart"] = {"stopped_replica": 0, "stop_seconds": stopped_after,
                         "restart_to_ready_seconds": time.monotonic() - restarted_at,
                         "other_replica_ready_while_stopped": True, "write_while_other_replica_stopped": True,
                         "original_image_and_container_preserved": True,
                         "loopback_port_changed": first.port != apis[0].port}
    apis = [first, second]
    verify_stock(apis, product, 15)
    for api in apis:
        require(api.expect("POST", "/api/v1/reservations", "customer", body, idempotency_key=key) == cancelled,
                "Cancelled retry key changed after restart")
        require(api.expect("POST", "/api/v1/reservations", "customer", survivor_body,
                           idempotency_key=survivor_key) == survivor, "Write made during restart did not replay consistently")
    samples = concurrent_batch(apis, lambda api, _: api.request(
        "POST", "/api/v1/reservations", "customer", active_body, timeout=12, idempotency_key=active_key))
    report["retries_after_restart"] = summarize_batch(samples)
    require_same_reservation(samples, {200: 8}, active)
    verify_stock(apis, product, 15)
    samples = concurrent_batch(apis, lambda api, _: api.request(
        "POST", "/api/v1/reservations/" + active["id"] + "/cancel", "customer", {}, timeout=12))
    report["cancellation_after_restart"] = summarize_batch(samples)
    cancelled_active = require_same_reservation(samples, {200: 8})
    require(cancelled_active["status"] == "CANCELLED" and cancelled_active["id"] == active["id"],
            "Cancellation after restart returned the wrong reservation")
    verify_stock(apis, product, 19)
    verify_stock(apis, contended, 1)

    expected = {item["id"]: item for item in [cancelled, cancelled_active, survivor, *contended_reservations]}
    require(len(expected) == 6, "Fixture should retain exactly six distinct reservations")
    for api in apis:
        require(api.expect("POST", "/api/v1/reservations", "customer", active_body,
                           idempotency_key=active_key) == cancelled_active,
                "Replayed key did not preserve cancellation after restart")
        page = api.expect("GET", "/api/v1/reservations?size=100", "customer")
        require(page["totalElements"] == 6 and {item["id"]: item for item in page["items"]} == expected,
                "Final API reservation records do not match the exact expected committed records")
    report["final_invariants"] = {"reservation_records": 6, "active_reservations": 4,
                                  "cancelled_reservations": 2, "product_stock": [19, 1],
                                  "all_record_fields_match_on_both_replicas": True,
                                  "no_extra_reservations_from_retries": True,
                                  "cancellations_restore_stock_once": True}
    report["checks"] = ["same-key creation across replicas creates exactly one record",
                        "competing replicas cannot oversell shared stock",
                        "concurrent cancellation restores stock once",
                        "surviving replica replays keys and accepts writes while peer is stopped",
                        "restarted replica retains active and cancelled retry behavior",
                        "both replicas return exact final records and stock"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="inventory-cloud-service-app")
    parser.add_argument("--database-image", default="postgres:17-bookworm")
    parser.add_argument("--report", type=Path, default=recovery.ROOT / "reports/replicas/latest.json")
    args = parser.parse_args()
    report = {"started_at": recovery.utc_now(), "status": "failed", "drill": "local_two_replica_correctness_and_restart"}
    fixture = None
    try:
        fixture = ReplicaFixture(args.image, args.database_image)
        report["environment"] = fixture.metadata()
        run_drill(fixture, report)
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        report["cleanup_errors"] = fixture.cleanup() if fixture else []
        if report["cleanup_errors"]:
            report["status"] = "failed"
        report["finished_at"] = recovery.utc_now()
        recovery.write_report(args.report, report)
    print(f"{report['status'].upper()}: two-replica drill; report: {args.report}")
    if "error" in report:
        print(report["error"])
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
