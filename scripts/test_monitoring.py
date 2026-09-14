#!/usr/bin/env python3
"""Offline monitoring safeguards; no Docker, cloud, or external notifications."""
import importlib.util
import json
from pathlib import Path
from string import Template
import unittest
from unittest.mock import patch


def module(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(loaded)
    return loaded


monitoring = module("monitoring_drill", Path(__file__).with_name("monitoring-drill.py"))
receiver = module("webhook_receiver", monitoring.MONITORING / "webhook-receiver.py")


class MonitoringFailureTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "0123456789abcdef0123456789abcdef"
        self.names = {name: "inventory-drill-0123456789ab-" + name
                      for name in ("app", "alertmanager", "blackbox", "receiver")}
        substitutions = {"RUN_ID": self.run_id, "APP_HOST": self.names["app"],
            "ALERTMANAGER_HOST": self.names["alertmanager"], "BLACKBOX_HOST": self.names["blackbox"],
            "RECEIVER_URL": "http://" + self.names["receiver"] + ":8081/webhook"}
        self.prometheus = json.loads(Template((monitoring.MONITORING / "prometheus.json.template").read_text()).substitute(substitutions))
        self.alertmanager = json.loads(Template((monitoring.MONITORING / "alertmanager.json.template").read_text()).substitute(substitutions))
        self.payload = {"receiver": "drill-webhook", "status": "firing", "alerts": [{
            "status": "firing", "fingerprint": "0123456789abcdef", "startsAt": "2026-09-14T01:00:00Z",
            "endsAt": "2026-09-14T01:05:00Z", "labels": {"alertname": monitoring.ALERT_NAME, "drill_run": self.run_id},
        }]}

    def validate(self):
        monitoring.validate_configs(self.prometheus, self.alertmanager, self.names, self.run_id)

    def test_private_configuration_is_accepted(self):
        self.validate()

    def test_scrape_proxy_field_is_rejected(self):
        self.prometheus["scrape_configs"][0]["proxy_url"] = "http://example.com:8080"
        with self.assertRaisesRegex(RuntimeError, "Unexpected scrape job fields"):
            self.validate()

    def test_relabel_sequence_cannot_redirect_probes(self):
        self.prometheus["scrape_configs"][1]["relabel_configs"].insert(1, {
            "target_label": "__param_target", "replacement": "http://example.com"})
        with self.assertRaisesRegex(RuntimeError, "relabel sequence"):
            self.validate()

    def test_protected_metrics_redirects_are_rejected(self):
        self.prometheus["scrape_configs"][0]["follow_redirects"] = True
        with self.assertRaisesRegex(RuntimeError, "disabled redirects"):
            self.validate()

    def test_external_notification_destination_rejected(self):
        self.alertmanager["receivers"][0]["webhook_configs"][0]["url"] = "https://example.com/webhook"
        with self.assertRaisesRegex(RuntimeError, "private receiver"):
            self.validate()

    def test_additional_email_receiver_rejected(self):
        self.alertmanager["receivers"][0]["email_configs"] = [{"to": "somewhere@example.com"}]
        with self.assertRaisesRegex(RuntimeError, "Only the private webhook"):
            self.validate()

    def test_disabled_resolved_notifications_rejected(self):
        self.alertmanager["receivers"][0]["webhook_configs"][0]["send_resolved"] = False
        with self.assertRaisesRegex(RuntimeError, "Resolved delivery"):
            self.validate()

    def test_notification_redirects_rejected(self):
        self.alertmanager["receivers"][0]["webhook_configs"][0]["http_config"]["follow_redirects"] = True
        with self.assertRaisesRegex(RuntimeError, "disabled redirects"):
            self.validate()

    def test_prometheus_external_remote_write_rejected(self):
        self.prometheus["remote_write"] = [{"url": "https://example.com/metrics"}]
        with self.assertRaisesRegex(RuntimeError, "external writes"):
            self.validate()

    def test_external_scrape_target_rejected(self):
        self.prometheus["scrape_configs"][0]["static_configs"][0]["targets"] = ["example.com:80"]
        with self.assertRaisesRegex(RuntimeError, "Scrape targets"):
            self.validate()

    def test_receiver_drops_unapproved_fields(self):
        self.payload["externalURL"] = "http://internal.example/secret"
        self.payload["alerts"][0]["annotations"] = {"secret": "never-retain-this"}
        self.payload["alerts"][0]["labels"]["customer_id"] = "never-retain-this"
        clean = receiver.sanitize_notification(self.payload, self.run_id)
        self.assertNotIn("never-retain-this", json.dumps(clean))
        self.assertNotIn("externalURL", clean)

    def test_foreign_run_notification_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not belong"):
            receiver.sanitize_notification(self.payload, "another-run")

    def test_malformed_timestamp_rejected_instead_of_retained(self):
        self.payload["alerts"][0]["startsAt"] = "untrusted-secret-content"
        with self.assertRaises(ValueError):
            receiver.sanitize_notification(self.payload, self.run_id)

    def test_other_alert_resolution_cannot_pass_recovery(self):
        clean = receiver.sanitize_notification(self.payload, self.run_id)
        clean["status"] = clean["alerts"][0]["status"] = "resolved"
        self.assertIsNone(monitoring.notification([clean], "resolved", self.run_id, fingerprint="ffffffffffffffff"))

    def test_firing_notification_cannot_pass_resolution(self):
        clean = receiver.sanitize_notification(self.payload, self.run_id)
        self.assertIsNone(monitoring.notification([clean], "resolved", self.run_id))

    def test_missing_probe_is_not_interpreted_as_zero(self):
        class Observer:
            def wait(self, action, description, timeout):
                if action() is None:
                    raise RuntimeError("missing observation")
                raise AssertionError("Missing probe was incorrectly accepted")
        with patch.object(monitoring, "query_sample", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "missing observation"):
                monitoring.wait_metric(Observer(), 9090, "probe_success", 0)

    def test_nonfinite_metric_rejected(self):
        data = {"status": "success", "data": {"resultType": "vector", "result": [{"value": [1, "NaN"]}]}}
        with patch.object(monitoring, "json_get", return_value=data):
            with self.assertRaisesRegex(RuntimeError, "non-finite"):
                monitoring.query_sample(9090, "probe_success")


if __name__ == "__main__":
    unittest.main()
