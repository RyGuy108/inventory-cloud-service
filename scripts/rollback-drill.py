#!/usr/bin/env python3
"""Demonstrate an image rollback across an additive schema change using disposable local containers."""
import argparse
import http.client
import importlib.util
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("inventory_rollback_fixture", Path(__file__).with_name("recovery-drill.py"))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
docker, require = recovery.docker, recovery.require
CANDIDATE_LABEL = "inventory.drill.rollback-candidate"


def make_candidate_jar(source, destination):
    """Add one fixture SQL resource; retain every original Java class and other entry byte-for-byte."""
    with zipfile.ZipFile(source) as archive:
        entries = archive.namelist()
        versions = [int(match.group(1)) for name in entries
                    if (match := re.fullmatch(r"BOOT-INF/classes/db/migration/V(\d+)__[^/]+\.sql", name))]
        require(bool(versions), "The source image does not contain the expected versioned database migrations")
        require(len(entries) == len(set(entries)), "The source application archive has duplicate entries")
        require(not any("rollback_drill" in name for name in entries), "Do not use a prior drill candidate as the verified base")
    version = max(versions) + 1
    resource = f"BOOT-INF/classes/db/migration/V{version}__rollback_drill_add_optional_note.sql"
    migration = b"ALTER TABLE reservations ADD COLUMN rollback_drill_note VARCHAR(128);\n"
    shutil.copyfile(source, destination)
    with zipfile.ZipFile(destination, "a", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(resource, migration)
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(destination) as after:
        require(set(after.namelist()) - set(before.namelist()) == {resource}, "Candidate changed more than the fixture migration")
        require(all(before.read(name) == after.read(name) for name in before.namelist()),
                "Candidate changed existing application code or migration resources")
    return str(version), resource


def validate_history(before, after, new_version):
    require(all(row.get("success") is True for row in before + after), "Migration history contains an unsuccessful entry")
    require(len(after) == len(before) + 1, "The candidate must apply exactly one additive fixture migration")
    require(after[:-1] == before, "The candidate modified pre-existing migration history")
    require(after[-1]["version"] == new_version, "The expected additive fixture migration was not applied")


def schema_history(fixture, database):
    fixture.owned("container", database)
    query = """SELECT coalesce(json_agg(row_to_json(m) ORDER BY installed_rank), '[]'::json)
FROM (SELECT installed_rank, version, description, checksum, success
      FROM flyway_schema_history ORDER BY installed_rank) m;"""
    return json.loads(docker("exec", database, "psql", "--username", "inventory", "--dbname", "inventory",
                             "--no-psqlrc", "--tuples-only", "--no-align", "--command", query))


def check_added_column(fixture, database):
    fixture.owned("container", database)
    query = """SELECT count(*) FROM information_schema.columns WHERE table_schema='public'
AND table_name='reservations' AND column_name='rollback_drill_note'
AND is_nullable='YES' AND data_type='character varying';"""
    result = docker("exec", database, "psql", "--username", "inventory", "--dbname", "inventory",
                    "--no-psqlrc", "--tuples-only", "--no-align", "--command", query)
    require(result.decode().strip() == "1", "The candidate's additive nullable column was not preserved")


def contract_for(image):
    # Use the same embedded capability checks as the cloud release gate, without running image code.
    import release_contract
    details = json.loads(docker("image", "inspect", image))[0]
    platform = details["Os"] + "/" + details["Architecture"]
    return release_contract.inspect_local_contract(image, expected_platform=platform)


def start_runtime(fixture, role, image, database, port=None):
    fixture.owned("container", database)
    require(database in fixture.prepared_databases, "Runtime startup requires a successful migration job")
    environment = {
        "SPRING_PROFILES_ACTIVE": "local", "SPRING_FLYWAY_ENABLED": "false",
        "DB_URL": f"jdbc:postgresql://{database}:5432/inventory?socketTimeout=5&connectTimeout=3",
        "DB_USERNAME": "inventory_app", "DB_PASSWORD": fixture.runtime_passwords[database],
        "APP_AUTH_ADMIN_PASSWORD": fixture.passwords["admin"],
        "APP_AUTH_CUSTOMER_PASSWORD": fixture.passwords["customer"],
    }
    name = fixture.run_container(role, image, [
        "--publish", f"127.0.0.1:{port or ''}:8080", "--read-only", "--tmpfs", "/tmp:rw,size=128m,mode=1777",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "768m", "--cpus", "1",
        *[part for key in environment for part in ("--env", key)],
    ], environment)
    details = fixture.owned("container", name)
    require(details["Image"] == image, "Runtime started a different image from the inspected immutable image")
    binding = details["NetworkSettings"]["Ports"]["8080/tcp"][0]
    require(binding["HostIp"] == "127.0.0.1", "Rollback fixture must publish only to loopback")
    actual_port = int(binding["HostPort"])
    require(port is None or actual_port == port, "Rollback must preserve the same local API address")
    return name, recovery.Api(actual_port, fixture.passwords)


def build_candidate(fixture, source_container, directory, report):
    fixture.owned("container", source_container)
    docker("cp", source_container + ":/app/app.jar", str(directory / "source.jar"))
    version, resource = make_candidate_jar(directory / "source.jar", directory / "candidate.jar")
    # BuildKit treats a bare image ID in FROM as a registry tag. Give the already-inspected
    # image a unique local alias; do not reuse or change the user's existing image tags.
    base_tag = fixture.prefix + "-rollback-source:fixture"
    source_details = json.loads(docker("image", "inspect", fixture.image))[0]
    require(bool(source_details.get("RepoTags")), "Keep at least one tag on the verified source image before running this drill")
    report["temporary_base_tag"] = base_tag
    docker("image", "tag", fixture.image, base_tag)
    require(json.loads(docker("image", "inspect", base_tag))[0]["Id"] == fixture.image,
            "Temporary build alias does not resolve to the verified image")
    # The only application behavior change is the intentionally wrong health-route configuration.
    # The extra SQL resource is additive and contains no changes to Java business code.
    dockerfile = f"""FROM {base_tag}
COPY --chown=10001:10001 candidate.jar /app/app.jar
ENV MANAGEMENT_ENDPOINTS_WEB_BASE_PATH=/rollback-drill-actuator
LABEL {recovery.OWNER_LABEL}={fixture.run_id}
LABEL {recovery.PURPOSE_LABEL}={recovery.PURPOSE}
LABEL {CANDIDATE_LABEL}=true
HEALTHCHECK --interval=1s --timeout=2s --start-period=0s --retries=3 CMD curl --fail --silent http://localhost:8080/actuator/health/readiness || exit 1
"""
    (directory / "Dockerfile").write_text(dockerfile)
    (directory / ".dockerignore").write_text("*\n!Dockerfile\n!candidate.jar\n")
    # The derived image uses only the local source; its build context contains no repository files.
    docker("build", "--network", "none", "--pull=false", "--iidfile", str(directory / "candidate.iid"),
           str(directory), timeout=120)
    candidate = (directory / "candidate.iid").read_text().strip()
    report["candidate_image_id"] = candidate
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", candidate) is not None and candidate != fixture.image,
            "The unhealthy candidate must have its own immutable image identity")
    return candidate, version, resource


def migrate_candidate(fixture, candidate, database):
    fixture.owned("container", database)
    environment = {
        "SPRING_PROFILES_ACTIVE": "migrate", "DB_URL": f"jdbc:postgresql://{database}:5432/inventory",
        "DB_USERNAME": "inventory", "DB_PASSWORD": fixture.db_password,
        "DB_APP_PASSWORD": fixture.runtime_passwords[database],
    }
    name = fixture.run_container("candidate-migrate", candidate, [
        "--no-healthcheck", "--read-only", "--tmpfs", "/tmp:rw,size=128m,mode=1777",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "512m", "--cpus", "1",
        *[part for key in environment for part in ("--env", key)],
    ], environment)
    code = docker("container", "wait", name, timeout=90).decode().strip()
    state = fixture.owned("container", name)["State"]
    require(code == "0" and state["Status"] == "exited" and state["ExitCode"] == 0,
            "Candidate migration failed; candidate rollout is blocked")
    return {"exit_code": 0, "confirmed_stopped": True, "image_id": candidate}


def wait_candidate_failure(fixture, name, api):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        details = fixture.owned("container", name)
        require(details["State"]["Running"], "Candidate exited instead of exercising the intended unhealthy readiness route")
        try:
            status, _ = api.request("GET", "/api/v1/products", "customer", timeout=3)
            if status == 200:
                connection = http.client.HTTPConnection("127.0.0.1", api.port, timeout=3)
                try:
                    connection.request("GET", "/actuator/health/readiness")
                    response = connection.getresponse()
                    readiness_status = response.status
                    response.read()
                finally:
                    connection.close()
                require(readiness_status == 404, "Candidate did not fail for the deliberately relocated readiness route")
                health = details["State"].get("Health", {})
                if health.get("Status") == "unhealthy":
                    require(any(item.get("ExitCode", 0) != 0 for item in health.get("Log", [])),
                            "Docker did not record an actual failed health command")
                    return {"business_api_http_status": 200, "configured_readiness_http_status": 404,
                            "docker_health_status": "unhealthy", "image_id": details["Image"]}
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.3)
    raise RuntimeError("Candidate did not reach the expected observable unhealthy state")


def remove_candidate(fixture, candidate):
    require(candidate != fixture.image, "Refusing to remove the verified application image")
    details = json.loads(docker("image", "inspect", candidate))[0]
    labels = details.get("Config", {}).get("Labels", {})
    require(details["Id"] == candidate and labels.get(recovery.OWNER_LABEL) == fixture.run_id
            and labels.get(recovery.PURPOSE_LABEL) == recovery.PURPOSE and labels.get(CANDIDATE_LABEL) == "true",
            "Refusing to remove an image without this drill's exact ownership labels")
    docker("image", "rm", candidate)


def remove_base_tag(fixture, tag):
    require(tag == fixture.prefix + "-rollback-source:fixture", "Refusing to remove an untracked source-image tag")
    details = json.loads(docker("image", "inspect", tag))[0]
    require(details["Id"] == fixture.image, "Temporary source tag changed image identity")
    require(any(existing != tag for existing in details.get("RepoTags", [])),
            "Refusing to remove the last tag of the verified source image")
    docker("image", "rm", tag)
    require(json.loads(docker("image", "inspect", fixture.image))[0]["Id"] == fixture.image,
            "Verified source image was not preserved")


def run_drill(fixture, directory, report):
    report["base_release_contract"] = contract_for(fixture.image)
    fixture.create_network()
    database = fixture.start_database("rollback-db")
    fixture.migrate_database("baseline-migrate", database)
    report["runtime_login"] = fixture.check_runtime_login(database, fixture.runtime_passwords[database])
    before_history = schema_history(fixture, database)
    original, api = start_runtime(fixture, "original-api", fixture.image, database)
    api.wait_health()
    report["stable_loopback_port"] = api.port
    product = api.expect("POST", "/api/v1/products", "admin", {
        "sku": "ROLLBACK-" + fixture.run_id[:12].upper(), "name": "Rollback compatibility fixture", "initialQuantity": 17,
    }, 201)
    request_body = {"productId": product["id"], "quantity": 4}
    request_key = "rollback-" + fixture.run_id
    reservation = api.expect("POST", "/api/v1/reservations", "customer", request_body,
                             status=201, idempotency_key=request_key)
    product_path, reservation_path = "/api/v1/products/" + product["id"], "/api/v1/reservations/" + reservation["id"]
    original_product = api.expect("GET", product_path, "customer")
    require(original_product["availableQuantity"] == 13, "Baseline stock does not reflect exactly one reservation")
    candidate, new_version, resource = build_candidate(fixture, original, directory, report)
    report["candidate_image_id"] = candidate
    report["candidate_release_contract"] = contract_for(candidate)
    report["candidate_fixture_migration"] = {"version": new_version, "resource": resource,
                                               "change": "Add nullable reservations.rollback_drill_note; no business-code changes"}
    report["candidate_migration"] = migrate_candidate(fixture, candidate, database)
    after_history = schema_history(fixture, database)
    validate_history(before_history, after_history, new_version)
    check_added_column(fixture, database)
    report["schema_history_before"] = before_history
    report["schema_history_after_candidate"] = after_history
    report["checks"].append("candidate applies one additive migration without changing prior schema history or Java code")
    # Check the previous application against the upgraded schema before replacing it.
    require(api.expect("GET", product_path, "customer") == original_product, "Old application broke on the additive schema")
    report["checks"].append("the previous application continues reading existing data after the candidate migration")
    outage_started = time.monotonic()
    fixture.stop_container(original)
    candidate_container, candidate_api = start_runtime(fixture, "unhealthy-candidate", candidate, database, port=api.port)
    report["candidate_failure"] = wait_candidate_failure(fixture, candidate_container, candidate_api)
    require(report["candidate_failure"]["image_id"] == candidate, "Failure was observed on a different candidate image")
    failure_detected = time.monotonic()
    report["checks"].append("derived candidate serves its business API but returns readiness404 and becomes Docker-unhealthy")
    print("Candidate failure confirmed; restoring the prior immutable image on the same loopback address.", flush=True)
    fixture.stop_container(candidate_container)
    restored, restored_api = start_runtime(fixture, "restored-api", fixture.image, database, port=api.port)
    restored_api.wait_health()
    ready_at = time.monotonic()
    restored_image = fixture.owned("container", restored)["Image"]
    require(restored_image == fixture.image and restored_image != candidate, "Rollback did not restore the exact verified image")
    report["restored_image_id"] = restored_image
    report["timings_seconds"] = {"candidate_failure_to_restored_readiness": ready_at - failure_detected,
                                 "baseline_stop_to_restored_readiness": ready_at - outage_started}
    require(restored_api.expect("GET", product_path, "customer") == original_product, "Rollback changed original product fields")
    require(restored_api.expect("GET", reservation_path, "customer") == reservation, "Rollback changed original reservation fields")
    replay = restored_api.expect("POST", "/api/v1/reservations", "customer", request_body,
                                 status=200, idempotency_key=request_key)
    require(replay == reservation, "Rollback lost the idempotency key or returned a different reservation")
    require(restored_api.expect("GET", product_path, "customer")["availableQuantity"] == 13,
            "Idempotent replay after rollback consumed stock twice")
    report["checks"].append("exact prior image restores readiness, original records, and keyed reservation replay on the same API address")
    cancelled = restored_api.expect("POST", reservation_path + "/cancel", "customer", {})
    require(cancelled["status"] == "CANCELLED", "Restored image cannot cancel the existing reservation")
    require(restored_api.expect("POST", reservation_path + "/cancel", "customer", {}) == cancelled,
            "Repeated cancellation after rollback changed state")
    require(restored_api.expect("GET", product_path, "customer")["availableQuantity"] == 17,
            "Cancellation after rollback did not restore stock exactly once")
    replay_cancelled = restored_api.expect("POST", "/api/v1/reservations", "customer", request_body,
                                          status=200, idempotency_key=request_key)
    require(replay_cancelled == cancelled, "Cancelled idempotency key should retain the original reservation after rollback")
    report["checks"].append("restored image cancels once and replaying the cancelled request never creates another reservation")
    check_added_column(fixture, database)
    require(schema_history(fixture, database) == after_history, "Image rollback altered or removed migration history")
    report["checks"].append("candidate's additive column and successful migration remain intact after image rollback")
    report["preserved_records"] = {"product_id": product["id"], "reservation_id": reservation["id"],
                                   "final_stock": 17, "final_reservation_status": "CANCELLED"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="inventory-cloud-service-app")
    parser.add_argument("--database-image", default="postgres:17-bookworm")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/rollback/latest.json")
    args = parser.parse_args()
    fixture = None
    report = {"started_at": recovery.utc_now(), "passed": False, "checks": [],
              "scope": "local image replacement and schema/API compatibility; no ECS or cloud rollback executed"}
    try:
        fixture = recovery.Fixture(args.image, args.database_image)
        report.update(fixture.metadata())
        report["scope"] = "disposable local image replacement; existing Compose untouched; not an ECS rollback"
        # This directory is outside the repository, so normal application builds cannot include its archive.
        with tempfile.TemporaryDirectory(prefix="inventory-rollback-drill-") as temporary:
            print("Preparing a disposable database and an intentionally unhealthy image candidate.", flush=True)
            run_drill(fixture, Path(temporary), report)
            report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if fixture is not None:
            report["cleanup_errors"] = []
            if tag := report.get("temporary_base_tag"):
                try:
                    remove_base_tag(fixture, tag)
                    report["temporary_base_tag_removed"] = True
                except Exception as exc:
                    report["cleanup_errors"].append(str(exc))
            report["cleanup_errors"].extend(fixture.cleanup())
            if candidate := report.get("candidate_image_id"):
                try:
                    remove_candidate(fixture, candidate)
                    report["candidate_image_removed"] = True
                except Exception as exc:
                    report["cleanup_errors"].append(str(exc))
        if report.get("error") or report.get("cleanup_errors"):
            report["passed"] = False
        report["finished_at"] = recovery.utc_now()
        recovery.write_report(args.report, report)
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {len(report['checks'])} local rollback checks. Report: {args.report}")
    if report.get("error"):
        print(report["error"])
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
