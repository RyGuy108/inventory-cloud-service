#!/usr/bin/env python3
"""Exercise backup/restore and a database outage using only disposable containers.

Python standard library plus a working Docker CLI/engine are required. No Compose
containers, persistent volumes, cloud resources, or saved credentials are used.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
OWNER_LABEL = "inventory.drill.run"
PURPOSE_LABEL = "inventory.drill.purpose"
PURPOSE = "disposable-local-verification"


def docker(*args, input_bytes=None, extra_env=None, timeout=90):
    result = subprocess.run(
        ["docker", *args], input=input_bytes, capture_output=True,
        env={**os.environ, **(extra_env or {})}, timeout=timeout,
    )
    if result.returncode:
        # Do not print subprocess arguments or container logs: they may hold secrets.
        raise RuntimeError(f"Docker {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Api:
    def __init__(self, port, passwords):
        self.port = port
        self.passwords = passwords

    def request(self, method, path, account=None, body=None, timeout=10, idempotency_key=None):
        headers = {"Content-Type": "application/json"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if account:
            token = base64.b64encode(f"{account}:{self.passwords[account]}".encode()).decode()
            headers["Authorization"] = "Basic " + token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
        finally:
            connection.close()

    def expect(self, method, path, account=None, body=None, status=200, idempotency_key=None):
        actual, payload = self.request(method, path, account, body, idempotency_key=idempotency_key)
        require(actual == status, f"{method} {path}: expected {status}, received {actual}")
        return payload

    def wait_health(self, expected=200, timeout=90):
        deadline = time.monotonic() + timeout
        last_status = None
        while time.monotonic() < deadline:
            try:
                last_status, payload = self.request("GET", "/actuator/health/readiness", timeout=12)
                if last_status == expected:
                    require(payload["status"] == ("UP" if expected == 200 else "DOWN"),
                            "Readiness HTTP status and payload disagree")
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.25)
        raise RuntimeError(f"Readiness did not reach {expected}; last response was {last_status}")


class Fixture:
    """Own a new private network and explicitly tracked, labelled containers."""
    def __init__(self, image="inventory-cloud-service-app", database_image="postgres:17-bookworm"):
        self.run_id = uuid.uuid4().hex
        self.prefix = "inventory-drill-" + self.run_id[:12]
        self.network = self.prefix + "-network"
        self.containers = []
        self.network_created = False
        self.passwords = {name: secrets.token_urlsafe(24) for name in ("admin", "customer")}
        self.db_password = secrets.token_urlsafe(24)
        self.initial_runtime_password = secrets.token_urlsafe(24)
        self.runtime_passwords = {}
        self.prepared_databases = set()
        self.migration_runs = []
        # Resolve mutable tags before starting either application instance.
        self.image = docker("image", "inspect", image, "--format", "{{.Id}}").decode().strip()
        self.database_image = docker("image", "inspect", database_image, "--format", "{{.Id}}").decode().strip()
        self.environment = json.loads(docker("info", "--format", "{{json .}}"))

    def labels(self):
        return ["--label", f"{OWNER_LABEL}={self.run_id}", "--label", f"{PURPOSE_LABEL}={PURPOSE}"]

    def create_network(self):
        # Remember the attempt before Docker responds; a timeout can follow creation.
        self.network_created = True
        docker("network", "create", *self.labels(), self.network)

    def owned(self, kind, name):
        require(name.startswith(self.prefix + "-"), "Refusing operation on a name outside this drill")
        require((kind == "container" and name in self.containers) or
                (kind == "network" and name == self.network and self.network_created),
                "Refusing operation on a resource not created by this fixture")
        item = json.loads(docker(kind, "inspect", name))[0]
        labels = item.get("Labels", {}) if kind == "network" else item.get("Config", {}).get("Labels", {})
        require(labels.get(OWNER_LABEL) == self.run_id and labels.get(PURPOSE_LABEL) == PURPOSE,
                f"Refusing operation: ownership labels do not match for {name}")
        return item

    def run_container(self, role, image, arguments, environment=None):
        name = self.prefix + "-" + role
        require(name not in self.containers, "Duplicate disposable container name")
        # If docker run creates a container but fails to start it, cleanup still sees it.
        self.containers.append(name)
        docker("run", "--detach", "--name", name, *self.labels(),
               "--network", self.network, *arguments, image, extra_env=environment)
        self.owned("container", name)
        return name

    def start_database(self, role):
        environment = {"POSTGRES_DB": "inventory", "POSTGRES_USER": "inventory",
                       "POSTGRES_PASSWORD": self.db_password,
                       "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256 --auth-local=trust"}
        name = self.run_container(role, self.database_image, [
            "--tmpfs", "/var/lib/postgresql/data:rw,size=256m,mode=0700", "--memory", "384m", "--cpus", "1",
            *[part for key in environment for part in ("--env", key)],
        ], environment)
        self.wait_database(name)
        return name

    def wait_database(self, name):
        self.owned("container", name)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            # The image starts a temporary socket-only server during initialization.
            # TCP readiness waits for the final server, avoiding a restore/startup race.
            result = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1",
                                     "-U", "inventory", "-d", "inventory"],
                                    capture_output=True, timeout=10)
            if result.returncode == 0:
                return
            time.sleep(0.3)
        raise RuntimeError("Disposable PostgreSQL did not become ready")

    def migrate_database(self, role, database, runtime_password=None):
        """Run the shipped migration entrypoint; a failed run blocks runtime startup."""
        self.owned("container", database)
        self.prepared_databases.discard(database)
        password = self.initial_runtime_password if runtime_password is None else runtime_password
        environment = {
            "SPRING_PROFILES_ACTIVE": "migrate",
            "DB_URL": f"jdbc:postgresql://{database}:5432/inventory?socketTimeout=5&connectTimeout=3",
            "DB_USERNAME": "inventory", "DB_PASSWORD": self.db_password, "DB_APP_PASSWORD": password,
        }
        started = time.monotonic()
        name = self.run_container(role, self.image, [
            "--no-healthcheck", "--read-only", "--tmpfs", "/tmp:rw,size=128m,mode=1777",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "512m", "--cpus", "1",
            *[part for key in environment for part in ("--env", key)],
        ], environment)
        self.owned("container", name)
        code_text = docker("container", "wait", name, timeout=90).decode().strip()
        require(code_text.isdigit(), "Migration job did not return a numeric exit status")
        code = int(code_text)
        state = self.owned("container", name).get("State", {})
        self.migration_runs.append({"role": role, "exit_code": code,
                                    "duration_seconds": time.monotonic() - started})
        require(code == 0 and state.get("Status") == "exited" and state.get("ExitCode") == 0,
                f"Disposable migration task failed (exit code {code}); runtime startup blocked")
        self.runtime_passwords[database] = password
        self.prepared_databases.add(database)
        return name

    def stop_container(self, name):
        """Graceful stop for this fixture's runtime; never used on tmpfs databases."""
        self.owned("container", name)
        docker("container", "stop", "--time", "35", name, timeout=45)
        require(self.owned("container", name).get("State", {}).get("Status") == "exited",
                "Disposable runtime did not stop")

    def check_runtime_login(self, database, password, accepted=True):
        """Probe a fresh SCRAM TCP connection, never a trusted Unix-domain socket."""
        self.owned("container", database)
        query = ("SELECT current_user, NOT (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls), "
                 "NOT has_schema_privilege('public', 'CREATE'), "
                 "NOT has_table_privilege('public.flyway_schema_history', 'SELECT'), "
                 "NOT has_table_privilege('public.products', 'DELETE') "
                 "FROM pg_roles WHERE rolname = current_user")
        result = subprocess.run([
            "docker", "exec", "--env", "PGPASSWORD", "--env", "PGCONNECT_TIMEOUT=3", database,
            "psql", "--host", database, "--username", "inventory_app", "--dbname", "inventory",
            "--no-password", "--no-psqlrc", "--tuples-only", "--no-align", "--command", query,
        ], capture_output=True, env={**os.environ, "PGPASSWORD": password}, timeout=15)
        if accepted:
            require(result.returncode == 0 and result.stdout.decode().strip() == "inventory_app|t|t|t|t",
                    "Fresh runtime login or restricted privilege checks failed")
        else:
            # Connection failures, missing tools, or SQL errors do not prove password rejection.
            require(result.returncode == 2 and b'password authentication failed for user "inventory_app"' in result.stderr,
                    "Old runtime password was not specifically rejected by PostgreSQL authentication")
        return {"fresh_tcp_connection": True, "accepted": accepted,
                "runtime_permissions_checked": accepted,
                "expected_authentication_rejection": not accepted}

    def start_app(self, role, database):
        self.owned("container", database)
        require(database in self.prepared_databases,
                "Runtime startup requires a confirmed successful one-shot migration")
        environment = {
            "SPRING_PROFILES_ACTIVE": "local",
            "DB_URL": f"jdbc:postgresql://{database}:5432/inventory?socketTimeout=5&connectTimeout=3",
            "DB_USERNAME": "inventory_app", "DB_PASSWORD": self.runtime_passwords[database],
            "APP_AUTH_ADMIN_PASSWORD": self.passwords["admin"],
            "APP_AUTH_CUSTOMER_PASSWORD": self.passwords["customer"],
            "SPRING_FLYWAY_ENABLED": "false",
        }
        name = self.run_container(role, self.image, [
            "--publish", "127.0.0.1::8080", "--read-only", "--tmpfs", "/tmp:rw,size=128m,mode=1777",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "768m", "--cpus", "1",
            *[part for key in environment for part in ("--env", key)],
        ], environment)
        item = self.owned("container", name)
        binding = item["NetworkSettings"]["Ports"]["8080/tcp"][0]
        require(binding["HostIp"] == "127.0.0.1", "Disposable API must bind only to loopback")
        api = Api(int(binding["HostPort"]), self.passwords)
        api.wait_health()
        return name, api

    def cleanup(self):
        errors = []
        for name in reversed(self.containers):
            try:
                # Listing IDs distinguishes an absent resource from daemon/inspect failure.
                found = docker("container", "ls", "--all", "--filter", f"name=^/{name}$", "--format", "{{.Names}}").decode().splitlines()
                if name not in found:
                    continue
                self.owned("container", name)
                docker("container", "rm", "--force", name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if self.network_created:
            try:
                found = docker("network", "ls", "--filter", f"name=^{self.network}$", "--format", "{{.Name}}").decode().splitlines()
                if self.network in found:
                    self.owned("network", self.network)
                    docker("network", "rm", self.network)
            except Exception as exc:
                errors.append(f"{self.network}: {exc}")
        return errors

    def metadata(self):
        return {
            "run_id": self.run_id, "application_image_id": self.image,
            "postgres_image_id": self.database_image,
            "docker_server_version": self.environment.get("ServerVersion"),
            "docker_os": self.environment.get("OperatingSystem"),
            "docker_architecture": self.environment.get("Architecture"),
            "docker_cpus": self.environment.get("NCPU"),
            "docker_memory_bytes": self.environment.get("MemTotal"),
            "application_limits": {"cpus": 1, "memory_bytes": 768 * 1024 * 1024},
            "database_limits": {"cpus": 1, "memory_bytes": 384 * 1024 * 1024, "storage": "disposable tmpfs"},
            "authentication": "local HTTP Basic with generated per-run credentials",
            "database_runtime_user": "inventory_app",
            "schema_preparation": "real one-shot migrate profile using separate owner credentials",
            "runtime_flyway_enabled": False,
            "database_host_authentication": "scram-sha-256",
            "migration_runs": self.migration_runs,
            "jdbc_timeout_override_seconds": {"socketTimeout": 5, "connectTimeout": 3},
            "scope": "disposable local Docker containers; existing Compose application untouched",
        }


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(path)


def run_drill(fixture, report):
    fixture.create_network()
    database = fixture.start_database("source-db")
    fixture.migrate_database("source-migrate", database)
    report["source_runtime_login"] = fixture.check_runtime_login(database, fixture.runtime_passwords[database])
    _, source = fixture.start_app("source-app", database)
    product = source.expect("POST", "/api/v1/products", "admin", {
        "sku": "RESTORE-" + fixture.run_id[:12].upper(), "name": "Disposable restore drill", "initialQuantity": 37,
    }, 201)
    reservation_body = {
        "productId": product["id"], "quantity": 7,
    }
    reservation_key = "restore-" + fixture.run_id
    reservation = source.expect("POST", "/api/v1/reservations", "customer", reservation_body,
                                status=201, idempotency_key=reservation_key)
    product_path = "/api/v1/products/" + product["id"]
    reservation_path = "/api/v1/reservations/" + reservation["id"]
    expected_product = source.expect("GET", product_path, "customer")
    require(expected_product["availableQuantity"] == 30, "Source stock after reservation must be 30")
    expected_reservation = source.expect("GET", reservation_path, "customer")
    require(expected_reservation["status"] == "ACTIVE", "Seeded reservation must be active")

    started = time.monotonic()
    fixture.owned("container", database)
    backup = docker("exec", database, "pg_dump", "--username", "inventory", "--dbname", "inventory",
                    "--format", "custom", "--no-owner", "--no-privileges")
    report["backup"] = {"format": "PostgreSQL custom", "bytes": len(backup),
                        "sha256": hashlib.sha256(backup).hexdigest(), "duration_seconds": time.monotonic() - started,
                        "storage": "process memory; never saved or uploaded"}
    restored_database = fixture.start_database("restored-db")
    started = time.monotonic()
    fixture.owned("container", restored_database)
    docker("exec", "--interactive", restored_database, "pg_restore", "--username", "inventory", "--dbname", "inventory",
           "--exit-on-error", "--no-owner", "--no-privileges", input_bytes=backup)
    del backup
    report["restore"] = {"database_restore_seconds": time.monotonic() - started, "destination": "separate fresh disposable database"}
    fixture.migrate_database("restored-migrate", restored_database)
    report["restored_runtime_login"] = fixture.check_runtime_login(restored_database, fixture.runtime_passwords[restored_database])
    started = time.monotonic()
    _, restored = fixture.start_app("restored-app", restored_database)
    report["restore"]["application_ready_seconds"] = time.monotonic() - started
    require(restored.expect("GET", product_path, "customer") == expected_product,
            "Restored product does not match source fields")
    require(restored.expect("GET", reservation_path, "customer") == expected_reservation,
            "Restored reservation does not match source fields")
    require(restored.expect("POST", "/api/v1/reservations", "customer", reservation_body,
                            idempotency_key=reservation_key) == expected_reservation,
            "Restored idempotency key did not replay the existing reservation")
    cancelled = restored.expect("POST", reservation_path + "/cancel", "customer", {})
    require(cancelled["status"] == "CANCELLED", "Restored reservation cancellation failed")
    require(restored.expect("POST", reservation_path + "/cancel", "customer", {}) == cancelled,
            "Repeated cancellation changed restored reservation")
    require(restored.expect("POST", "/api/v1/reservations", "customer", reservation_body,
                            idempotency_key=reservation_key) == cancelled,
            "Replaying the restored cancelled reservation created new work")
    require(restored.expect("GET", product_path, "customer")["availableQuantity"] == 37,
            "Restored stock was not replenished exactly once")
    require(source.expect("GET", product_path, "customer") == expected_product,
            "Restored application changed the source database")
    report["restore"]["checks"] = ["all product fields match", "all active reservation fields match",
        "idempotency key survives restore and cancellation",
        "cancellation succeeds", "repeat cancellation is idempotent", "stock restored exactly once", "source remains unchanged"]

    # Pause preserves the tmpfs contents while removing database responsiveness.
    # A stop/start would discard tmpfs storage and test data loss instead of a transient outage.
    fixture.owned("container", restored_database)
    started = time.monotonic()
    docker("container", "pause", restored_database)
    try:
        restored.wait_health(expected=503, timeout=30)
        unavailable_after = time.monotonic() - started
        liveness_status, liveness = restored.request("GET", "/actuator/health/liveness")
        require(liveness_status == 200 and liveness["status"] == "UP", "Database outage must not fail liveness")
    finally:
        fixture.owned("container", restored_database)
        docker("container", "unpause", restored_database)
    started = time.monotonic()
    restored.wait_health(timeout=60)
    report["database_outage"] = {"simulation": "pause/unpause only the restored disposable PostgreSQL container",
        "readiness_status_during_outage": 503, "liveness_status_during_outage": liveness_status,
        "unavailable_detection_seconds": unavailable_after, "readiness_recovery_seconds": time.monotonic() - started}
    require(restored.expect("GET", product_path, "customer")["availableQuantity"] == 37,
            "Business data inaccessible after database outage recovery")
    report["database_outage"]["business_read_after_recovery"] = "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="inventory-cloud-service-app")
    parser.add_argument("--database-image", default="postgres:17-bookworm")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/recovery-latest.json")
    args = parser.parse_args()
    report = {"started_at": utc_now(), "status": "failed", "drill": "local_backup_restore_and_database_outage"}
    fixture = None
    try:
        fixture = Fixture(args.image, args.database_image)
        report["environment"] = fixture.metadata()
        run_drill(fixture, report)
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        report["cleanup_errors"] = fixture.cleanup() if fixture else []
        if report["cleanup_errors"]:
            report["status"] = "failed"
        report["finished_at"] = utc_now()
        write_report(args.report, report)
    print(f"{report['status'].upper()}: recovery drill; report: {args.report}")
    if "error" in report:
        print(report["error"])
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
