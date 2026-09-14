#!/usr/bin/env python3
"""Rotate a runtime database password using only disposable local resources."""
import argparse
import importlib.util
from pathlib import Path
import secrets
import time

spec = importlib.util.spec_from_file_location("recovery_drill", Path(__file__).with_name("recovery-drill.py"))
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def run_rotation(fixture, report):
    fixture.create_network()
    database = fixture.start_database("rotation-db")
    fixture.migrate_database("initial-migrate", database)
    old_password = fixture.runtime_passwords[database]
    report["initial_runtime_login"] = fixture.check_runtime_login(database, old_password)
    old_container, original = fixture.start_app("old-runtime", database)
    product = original.expect("POST", "/api/v1/products", "admin", {
        "sku": "ROTATE-" + fixture.run_id[:12].upper(), "name": "Disposable credential rotation", "initialQuantity": 23,
    }, 201)
    reservation_body = {
        "productId": product["id"], "quantity": 6,
    }
    reservation_key = "rotate-" + fixture.run_id
    reservation = original.expect("POST", "/api/v1/reservations", "customer", reservation_body,
                                  status=201, idempotency_key=reservation_key)
    product_path = "/api/v1/products/" + product["id"]
    reservation_path = "/api/v1/reservations/" + reservation["id"]
    expected_product = original.expect("GET", product_path, "customer")
    expected_reservation = original.expect("GET", reservation_path, "customer")
    drill.require(expected_product["availableQuantity"] == 17 and expected_reservation["status"] == "ACTIVE",
                  "Rotation seed data is incorrect")

    # This is explicitly a maintenance procedure. Old pooled connections must not
    # disguise whether replacement processes can authenticate with the new secret.
    started = time.monotonic()
    fixture.stop_container(old_container)
    report["maintenance"] = {"old_runtime_stopped": True,
                             "strategy": "stop old runtime, rotate with migration task, replace runtime"}
    new_password = secrets.token_urlsafe(24)
    drill.require(new_password != old_password, "New runtime credential must differ from old credential")
    fixture.migrate_database("rotation-migrate", database, runtime_password=new_password)
    report["old_password_probe"] = fixture.check_runtime_login(database, old_password, accepted=False)
    report["new_password_probe"] = fixture.check_runtime_login(database, new_password)
    _, replacement = fixture.start_app("replacement-runtime", database)
    report["maintenance"]["stop_through_replacement_ready_seconds"] = time.monotonic() - started
    drill.require(replacement.expect("GET", product_path, "customer") == expected_product,
                  "Product changed during password rotation")
    drill.require(replacement.expect("GET", reservation_path, "customer") == expected_reservation,
                  "Reservation changed during password rotation")
    drill.require(replacement.expect("POST", "/api/v1/reservations", "customer", reservation_body,
                                     idempotency_key=reservation_key) == expected_reservation,
                  "Idempotency key did not survive runtime replacement")
    cancelled = replacement.expect("POST", reservation_path + "/cancel", "customer", {})
    drill.require(cancelled["status"] == "CANCELLED", "Cancellation failed after password rotation")
    drill.require(replacement.expect("POST", reservation_path + "/cancel", "customer", {}) == cancelled,
                  "Repeated cancellation changed the reservation after rotation")
    drill.require(replacement.expect("POST", "/api/v1/reservations", "customer", reservation_body,
                                     idempotency_key=reservation_key) == cancelled,
                  "Replaying the cancelled reservation created new work after rotation")
    drill.require(replacement.expect("GET", product_path, "customer")["availableQuantity"] == 23,
                  "Cancellation did not restore stock exactly once after rotation")
    report["business_checks"] = ["product fields preserved", "active reservation fields preserved",
        "idempotency key survives runtime replacement and cancellation",
        "replacement runtime accepts authenticated requests", "cancellation succeeds",
        "repeat cancellation is idempotent", "stock restored exactly once"]
    report["limits"] = ["local Docker and generated per-run environment credentials",
        "maintenance interval required; not zero downtime", "no AWS Secrets Manager or RDS rotation demonstrated",
        "old existing database sessions are not used to test password invalidation"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="inventory-cloud-service-app")
    parser.add_argument("--database-image", default="postgres:17-bookworm")
    parser.add_argument("--report", type=Path, default=drill.ROOT / "reports/credential-rotation-latest.json")
    args = parser.parse_args()
    report = {"started_at": drill.utc_now(), "status": "failed", "drill": "local_runtime_password_rotation"}
    fixture = None
    try:
        fixture = drill.Fixture(args.image, args.database_image)
        report["environment"] = fixture.metadata()
        run_rotation(fixture, report)
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        report["cleanup_errors"] = fixture.cleanup() if fixture else []
        if report["cleanup_errors"]:
            report["status"] = "failed"
        report["finished_at"] = drill.utc_now()
        drill.write_report(args.report, report)
    print(f"{report['status'].upper()}: runtime password rotation; report: {args.report}")
    if "error" in report:
        print(report["error"])
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
