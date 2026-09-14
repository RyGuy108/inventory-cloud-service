#!/usr/bin/env python3
"""Verify real local Prometheus scraping and Alertmanager firing/resolved delivery."""
import argparse
import base64
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
from string import Template
import tempfile
import time
from urllib.parse import urlencode, urlsplit

spec = importlib.util.spec_from_file_location("recovery_drill", Path(__file__).with_name("recovery-drill.py"))
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)
MONITORING = drill.ROOT / "monitoring"
ALERT_NAME = "InventoryReadinessFailed"


def http_get(port, path, headers=None, timeout=5):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def json_get(port, path):
    status, body = http_get(port, path)
    drill.require(status == 200, f"Local monitoring endpoint {path.split('?')[0]} returned {status}")
    return json.loads(body)


def validate_receiver_url(url, expected_host):
    parsed = urlsplit(url)
    drill.require(parsed.scheme == "http" and parsed.hostname == expected_host and parsed.port == 8081
                  and parsed.path == "/webhook" and not parsed.query and not parsed.fragment
                  and not parsed.username and not parsed.password,
                  "Notifications must target only this fixture's private receiver")


def validate_configs(prometheus, alertmanager, names, run_id):
    # Fail closed if edited templates introduce external targets or other senders.
    drill.require(set(prometheus) == {"global", "rule_files", "alerting", "scrape_configs"},
                  "Unexpected Prometheus configuration; external writes are not permitted")
    drill.require(prometheus["rule_files"] == ["/config/alerts.yml"], "Unexpected rule source")
    drill.require(prometheus["alerting"] == {"alertmanagers": [{"static_configs": [{"targets": [names["alertmanager"] + ":9093"]}]}]},
                  "Alertmanager must belong to this fixture")
    jobs = prometheus["scrape_configs"]
    drill.require(len(jobs) == 2 and [job["job_name"] for job in jobs] == ["inventory-app", "inventory-readiness"],
                  "Unexpected scrape jobs")
    drill.require(set(jobs[0]) == {"job_name", "metrics_path", "follow_redirects", "basic_auth", "static_configs"}
                  and set(jobs[1]) == {"job_name", "scrape_interval", "scrape_timeout", "metrics_path", "params",
                                       "static_configs", "relabel_configs"}, "Unexpected scrape job fields")
    drill.require(jobs[0]["metrics_path"] == "/actuator/prometheus" and jobs[0]["follow_redirects"] is False,
                  "Protected metrics path and disabled redirects are required")
    drill.require(jobs[1]["metrics_path"] == "/probe" and jobs[1]["params"] == {"module": ["inventory_readiness"]}
                  and jobs[1]["scrape_interval"] == "10s" and jobs[1]["scrape_timeout"] == "9s",
                  "Unexpected readiness probe configuration")
    for job, target in zip(jobs, (names["app"] + ":8080", "http://" + names["app"] + ":8080/actuator/health/readiness")):
        drill.require(job["static_configs"] == [{"targets": [target], "labels": {"drill_run": run_id}}],
                      "Scrape targets must belong to this fixture")
    drill.require(jobs[0]["basic_auth"] == {"username": "admin", "password_file": "/config/metrics-password"},
                  "Protected metrics must use the generated credential file")
    drill.require(jobs[1]["relabel_configs"] == [
        {"source_labels": ["__address__"], "target_label": "__param_target"},
        {"source_labels": ["__param_target"], "target_label": "instance"},
        {"target_label": "__address__", "replacement": names["blackbox"] + ":9115"}],
        "Readiness relabel sequence must target only this fixture")
    drill.require(set(alertmanager) == {"global", "route", "receivers"}, "Unexpected Alertmanager configuration")
    drill.require(alertmanager["global"] == {"resolve_timeout": "1m"}, "Unexpected Alertmanager global settings")
    receivers = alertmanager["receivers"]
    drill.require(len(receivers) == 1 and set(receivers[0]) == {"name", "webhook_configs"}
                  and receivers[0]["name"] == "drill-webhook", "Only the private webhook receiver is allowed")
    webhooks = receivers[0]["webhook_configs"]
    drill.require(len(webhooks) == 1 and set(webhooks[0]) == {"url", "send_resolved", "timeout", "http_config"},
                  "Unexpected notification configuration")
    validate_receiver_url(webhooks[0]["url"], names["receiver"])
    drill.require(webhooks[0]["send_resolved"] is True and webhooks[0]["http_config"] == {"follow_redirects": False},
                  "Resolved delivery and disabled redirects are required")


def prepare_configs(directory, fixture, names):
    replacements = {"APP_HOST": names["app"], "ALERTMANAGER_HOST": names["alertmanager"],
                    "BLACKBOX_HOST": names["blackbox"], "RUN_ID": fixture.run_id,
                    "RECEIVER_URL": "http://" + names["receiver"] + ":8081/webhook"}
    prometheus = json.loads(Template((MONITORING / "prometheus.json.template").read_text()).substitute(replacements))
    alertmanager = json.loads(Template((MONITORING / "alertmanager.json.template").read_text()).substitute(replacements))
    validate_configs(prometheus, alertmanager, names, fixture.run_id)
    configurations = {"prometheus.yml": prometheus, "alertmanager.yml": alertmanager,
                      "blackbox.yml": json.loads((MONITORING / "blackbox.json").read_text()),
                      "alerts.yml": json.loads((MONITORING / "alerts.json").read_text())}
    for name, config in configurations.items():
        file = directory / name
        file.write_text(json.dumps(config, indent=2) + "\n")
        file.chmod(0o600)
    password_file = directory / "metrics-password"
    password_file.write_text(fixture.passwords["admin"])
    password_file.chmod(0o600)
    shutil.copyfile(MONITORING / "webhook-receiver.py", directory / "webhook-receiver.py")
    (directory / "webhook-receiver.py").chmod(0o600)
    return configurations


def start_service(fixture, role, image, arguments, command, environment=None):
    """Use the shared fixture's ownership/cleanup with explicit image command arguments."""
    name = fixture.prefix + "-" + role
    drill.require(name not in fixture.containers, "Duplicate disposable monitoring container")
    fixture.containers.append(name)
    drill.docker("run", "--detach", "--name", name, *fixture.labels(), "--network", fixture.network,
                 *arguments, image, *command, extra_env=environment)
    fixture.owned("container", name)
    return name


def loopback_port(fixture, name, port):
    bindings = fixture.owned("container", name)["NetworkSettings"]["Ports"][f"{port}/tcp"]
    drill.require(len(bindings) == 1 and bindings[0]["HostIp"] == "127.0.0.1",
                  "Monitoring inspection port must bind only to loopback")
    return int(bindings[0]["HostPort"])


class Observation:
    def __init__(self, overall_seconds=240):
        self.deadline = time.monotonic() + overall_seconds

    def wait(self, operation, description, timeout=65):
        deadline = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < deadline:
            try:
                value = operation()
                if value is not None and value is not False:
                    return value
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.5)
        raise RuntimeError("Timed out waiting for " + description)


def query_sample(port, expression):
    payload = json_get(port, "/api/v1/query?" + urlencode({"query": expression}))
    drill.require(payload.get("status") == "success", "Prometheus query did not succeed")
    data = payload["data"]
    drill.require(data.get("resultType") == "vector", "Unexpected Prometheus query result type")
    if not data["result"]:
        return None
    drill.require(len(data["result"]) == 1, "Prometheus query unexpectedly matched multiple series")
    value = float(data["result"][0]["value"][1])
    drill.require(math.isfinite(value), "Prometheus query returned a non-finite value")
    return value


def wait_metric(observer, port, expression, expected, timeout=65):
    def check():
        value = query_sample(port, expression)
        return {"expression": expression, "value": value} if value == expected else None
    return observer.wait(check, f"metric {expression.split('{')[0]} = {expected}", timeout)


def notification(events, status, run_id, fingerprint=None):
    for event in events:
        if event.get("status") != status:
            continue
        for alert in event.get("alerts", []):
            if (alert.get("status") == status and alert.get("drill_run") == run_id
                    and alert.get("alertname") == ALERT_NAME
                    and (fingerprint is None or alert.get("fingerprint") == fingerprint)):
                return alert
    return None


def resolve_images():
    images = json.loads((MONITORING / "images.json").read_text())
    for specification in images.values():
        item = json.loads(drill.docker("image", "inspect", specification["image"]))[0]
        specification["image_id"] = item["Id"]
        specification["repo_digests"] = item.get("RepoDigests", [])
    return images


def run_monitoring(fixture, directory, report, images):
    observer = Observation()
    fixture.create_network()
    database = fixture.start_database("monitoring-db")
    fixture.migrate_database("monitoring-migrate", database)
    _, api = fixture.start_app("monitoring-app", database)
    names = {name: fixture.prefix + "-" + role for name, role in {
        "app": "monitoring-app", "prometheus": "prometheus", "alertmanager": "alertmanager",
        "blackbox": "blackbox", "receiver": "receiver"}.items()}
    configs = prepare_configs(directory, fixture, names)
    report["configuration"] = {"metrics_scrape_seconds": 2, "readiness_scrape_seconds": 10,
        "readiness_timeout_seconds": 8, "alert_for_seconds": 4, "evaluation_seconds": 1,
        "notification_group_wait_seconds": 1, "notification_group_interval_seconds": 2,
        "receiver_scope": "private disposable webhook; no external message destination",
        "metrics_password_storage": "temporary mode-0600 file, deleted after container cleanup"}
    user = f"{os.getuid()}:{os.getgid()}"
    common = ["--user", user, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
              "--cpus", "0.5"]
    def mount_file(name):
        return ["--mount", f"type=bind,source={directory / name},target=/config/{name},readonly"]
    receiver_name = start_service(fixture, "receiver", images["receiver"]["image_id"], [
        *common, *mount_file("webhook-receiver.py"), "--memory", "64m", "--publish", "127.0.0.1::8081", "--env", "DRILL_RUN_ID"],
        ["python3", "-B", "/config/webhook-receiver.py"], {"DRILL_RUN_ID": fixture.run_id})
    receiver_port = loopback_port(fixture, receiver_name, 8081)
    observer.wait(lambda: json_get(receiver_port, "/health").get("status") == "UP", "private webhook receiver", 25)
    tmpfs_options = f"rw,size=128m,uid={os.getuid()},gid={os.getgid()},mode=0700"
    start_service(fixture, "alertmanager", images["alertmanager"]["image_id"], [
        *common, *mount_file("alertmanager.yml"), "--memory", "128m", "--tmpfs", "/alertmanager:" + tmpfs_options],
        ["--config.file=/config/alertmanager.yml", "--storage.path=/alertmanager", "--cluster.listen-address="])
    start_service(fixture, "blackbox", images["blackbox"]["image_id"], [*common, *mount_file("blackbox.yml"), "--memory", "64m"],
                  ["--config.file=/config/blackbox.yml"])
    prometheus_name = start_service(fixture, "prometheus", images["prometheus"]["image_id"], [
        *common, *mount_file("prometheus.yml"), *mount_file("alerts.yml"), *mount_file("metrics-password"),
        "--memory", "256m", "--tmpfs", "/prometheus:" + tmpfs_options, "--publish", "127.0.0.1::9090"],
        ["--config.file=/config/prometheus.yml", "--storage.tsdb.path=/prometheus", "--storage.tsdb.retention.time=1h"])
    prometheus_port = loopback_port(fixture, prometheus_name, 9090)
    observer.wait(lambda: http_get(prometheus_port, "/-/ready")[0] == 200, "Prometheus readiness", 30)
    selector = '{job="inventory-app",drill_run="' + fixture.run_id + '"}'
    probe_selector = '{job="inventory-readiness",drill_run="' + fixture.run_id + '"}'
    up = "up" + selector
    probe = "probe_success" + probe_selector
    status_code = "probe_http_status_code" + probe_selector
    wait_metric(observer, prometheus_port, up, 1)
    wait_metric(observer, prometheus_port, probe, 1)
    wait_metric(observer, prometheus_port, status_code, 200)
    anonymous_status, _ = http_get(api.port, "/actuator/prometheus")
    customer_token = base64.b64encode(("customer:" + fixture.passwords["customer"]).encode()).decode()
    customer_status, _ = http_get(api.port, "/actuator/prometheus", {"Authorization": "Basic " + customer_token})
    drill.require(anonymous_status == 401 and customer_status == 403, "Metrics authorization boundary failed")
    report["metrics_access"] = {"unauthenticated_status": anonymous_status, "customer_status": customer_status,
                                "authenticated_prometheus_scrape_up": 1}
    memory_series = query_sample(prometheus_port, "count(jvm_memory_used_bytes" + selector + ")")
    drill.require(memory_series is not None and memory_series > 0, "JVM metrics were not scraped")
    report["jvm_memory_series"] = int(memory_series)
    product = api.expect("POST", "/api/v1/products", "admin", {
        "sku": "MONITOR-" + fixture.run_id[:12].upper(), "name": "Disposable monitoring workflow", "initialQuantity": 9,
    }, 201)
    reservation_body = {"productId": product["id"], "quantity": 3}
    key = "monitor-" + fixture.run_id
    reservation = api.expect("POST", "/api/v1/reservations", "customer", reservation_body,
                             status=201, idempotency_key=key)
    drill.require(api.expect("POST", "/api/v1/reservations", "customer", reservation_body,
                             idempotency_key=key) == reservation, "Monitoring seed replay changed reservation")
    api.expect("POST", "/api/v1/reservations/" + reservation["id"] + "/cancel", "customer", {})
    report["business_metrics"] = {
        name: wait_metric(observer, prometheus_port, name + selector, 1)["value"] for name in (
            "inventory_reservations_total", "inventory_reservations_replayed_total", "inventory_reservations_cancelled_total")}
    report["baseline"] = {"metrics_up": 1, "readiness_probe_success": 1, "readiness_http_status": 200}
    drill.require(not json_get(receiver_port, "/events")["events"], "Readiness alert arrived before the simulated outage")

    started = time.monotonic()
    firing = None
    try:
        fixture.owned("container", database)
        drill.docker("container", "pause", database)
        wait_metric(observer, prometheus_port, probe, 0)
        wait_metric(observer, prometheus_port, status_code, 503)
        firing = observer.wait(lambda: notification(json_get(receiver_port, "/events")["events"], "firing", fixture.run_id),
                               "actual firing webhook notification", 75)
        firing_seconds = time.monotonic() - started
        alerts = json_get(prometheus_port, "/api/v1/alerts")["data"]["alerts"]
        drill.require(any(a.get("state") == "firing" and a.get("labels", {}).get("alertname") == ALERT_NAME
                          and a.get("labels", {}).get("drill_run") == fixture.run_id for a in alerts),
                      "Prometheus did not report this readiness alert as firing")
        wait_metric(observer, prometheus_port, up, 1, 10)
        last_scrape_age = query_sample(prometheus_port, "time() - timestamp(up" + selector + ")")
        drill.require(last_scrape_age is not None and last_scrape_age < 5, "Metrics scrape is stale during readiness failure")
        drill.require(api.expect("GET", "/actuator/health/liveness")["status"] == "UP", "Liveness failed during database outage")
        report["outage"] = {"readiness_probe_success": 0, "readiness_http_status": 503, "metrics_up": 1,
            "metrics_last_scrape_age_seconds": last_scrape_age, "liveness_http_status": 200,
            "prometheus_alert_state": "firing",
            "pause_to_firing_notification_seconds": firing_seconds, "fingerprint": firing["fingerprint"]}
    finally:
        state = fixture.owned("container", database).get("State", {})
        if state.get("Paused"):
            drill.docker("container", "unpause", database)
        report["notifications"] = json_get(receiver_port, "/events")["events"]
    started = time.monotonic()
    api.wait_health(timeout=30)
    wait_metric(observer, prometheus_port, probe, 1, 35)
    wait_metric(observer, prometheus_port, status_code, 200, 20)
    resolved = observer.wait(lambda: notification(json_get(receiver_port, "/events")["events"], "resolved", fixture.run_id,
                                                fingerprint=firing["fingerprint"]), "matching resolved webhook notification", 45)
    report["notifications"] = json_get(receiver_port, "/events")["events"]
    report["recovery"] = {"readiness_probe_success": 1, "readiness_http_status": 200,
        "unpause_to_resolved_notification_seconds": time.monotonic() - started,
        "same_alert_fingerprint": resolved["fingerprint"] == firing["fingerprint"]}
    active = json_get(prometheus_port, "/api/v1/alerts")["data"]["alerts"]
    drill.require(not any(a.get("labels", {}).get("alertname") == ALERT_NAME for a in active),
                  "Readiness alert is still active after recovery")
    drill.require(api.expect("GET", "/api/v1/products/" + product["id"], "customer")["availableQuantity"] == 9,
                  "Business state changed during the monitoring drill")
    report["business_read_after_recovery"] = "passed"
    report["sanitized_configuration"] = configs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="inventory-cloud-service-app")
    parser.add_argument("--database-image", default="postgres:17-bookworm")
    parser.add_argument("--report", type=Path, default=drill.ROOT / "reports/monitoring/latest.json")
    args = parser.parse_args()
    report = {"started_at": drill.utc_now(), "status": "failed", "drill": "local_metrics_readiness_and_notification_delivery"}
    fixture = None
    temporary = None
    try:
        images = resolve_images()
        report["monitoring_images"] = images
        fixture = drill.Fixture(args.image, args.database_image)
        report["environment"] = fixture.metadata()
        local_root = drill.ROOT / ".local"
        local_root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="monitoring-" + fixture.run_id[:12] + "-", dir=local_root)
        run_monitoring(fixture, Path(temporary.name), report, images)
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        report["cleanup_errors"] = fixture.cleanup() if fixture else []
        if temporary:
            temporary.cleanup()
        if report["cleanup_errors"]:
            report["status"] = "failed"
        report["finished_at"] = drill.utc_now()
        drill.write_report(args.report, report)
    print(f"{report['status'].upper()}: monitoring and private notification drill; report: {args.report}")
    if "error" in report:
        print(report["error"])
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
