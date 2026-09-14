#!/usr/bin/env python3
"""Private disposable Alertmanager receiver; no outbound requests or raw logging."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading

MAX_BODY_BYTES = 65536
MAX_EVENTS = 64


def sanitize_notification(payload, run_id):
    """Accept only this drill's real alert shape and retain a small fixed field set."""
    if not isinstance(payload, dict) or payload.get("receiver") != "drill-webhook":
        raise ValueError("Unexpected notification receiver")
    if payload.get("status") not in ("firing", "resolved"):
        raise ValueError("Unexpected notification status")
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not 1 <= len(alerts) <= 10:
        raise ValueError("Unexpected alert count")
    clean = []
    for alert in alerts:
        labels = alert.get("labels", {})
        if labels.get("alertname") != "InventoryReadinessFailed" or labels.get("drill_run") != run_id:
            raise ValueError("Alert does not belong to this drill")
        if alert.get("status") not in ("firing", "resolved"):
            raise ValueError("Unexpected alert status")
        fingerprint = alert.get("fingerprint", "")
        if not isinstance(fingerprint, str) or len(fingerprint) != 16 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("Invalid alert fingerprint")
        for field in ("startsAt", "endsAt"):
            timestamp = alert.get(field)
            if not isinstance(timestamp, str) or len(timestamp) > 40:
                raise ValueError("Invalid alert timestamp")
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        clean.append({"status": alert["status"], "fingerprint": fingerprint,
                      "starts_at": alert.get("startsAt"), "ends_at": alert.get("endsAt"),
                      "alertname": labels["alertname"], "drill_run": run_id})
    return {"received_at": datetime.now(timezone.utc).isoformat(),
            "status": payload["status"], "alerts": clean}


class Receiver(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self.respond(200, {"status": "UP"})
        elif self.path == "/events":
            with self.server.event_lock:
                self.respond(200, {"events": list(self.server.events)})
        else:
            self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/webhook":
            self.respond(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                self.respond(413, {"error": "Invalid request size"})
                return
            payload = json.loads(self.rfile.read(length))
            notification = sanitize_notification(payload, self.server.run_id)
            with self.server.event_lock:
                if len(self.server.events) >= MAX_EVENTS:
                    self.respond(429, {"error": "Notification limit reached"})
                    return
                self.server.events.append(notification)
            self.respond(200, {"accepted": True})
        except (ValueError, TypeError, AttributeError):
            self.respond(400, {"error": "Invalid notification"})


def main():
    run_id = os.environ["DRILL_RUN_ID"]
    server = ThreadingHTTPServer(("0.0.0.0", 8081), Receiver)
    server.run_id = run_id
    server.events = []
    server.event_lock = threading.Lock()
    server.serve_forever()


if __name__ == "__main__":
    main()
