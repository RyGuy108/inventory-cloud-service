#!/usr/bin/env python3
"""Verify the running local workflow using credentials from the private .env file."""
import base64
import http.client
import json
from pathlib import Path
import uuid


def main():
    config = {}
    for line in (Path(__file__).resolve().parents[1] / ".env").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            config[key] = value

    def request(method, path, account=None, body=None, expected=200, key=None):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        if account:
            password = config[f"APP_AUTH_{account.upper()}_PASSWORD"]
            headers["Authorization"] = "Basic " + base64.b64encode(f"{account}:{password}".encode()).decode()
        # Credentials from this file are only ever sent to the loopback service.
        connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=20)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            payload = response.read()
            if response.status != expected:
                raise RuntimeError(f"{method} {path}: expected {expected}, received {response.status}")
            return json.loads(payload)
        finally:
            connection.close()

    assert request("GET", "/actuator/health/readiness")["status"] == "UP"
    request("GET", "/api/v1/products", expected=401)
    product = request("POST", "/api/v1/products", "admin", {
        "sku": "DEMO-" + uuid.uuid4().hex[:12].upper(),
        "name": "Demo inventory item", "initialQuantity": 7,
    }, expected=201)
    key = str(uuid.uuid4())
    reservation_body = {"productId": product["id"], "quantity": 3}
    reservation = request("POST", "/api/v1/reservations", "customer", reservation_body, expected=201, key=key)
    assert request("POST", "/api/v1/reservations", "customer", reservation_body, key=key) == reservation
    request("POST", "/api/v1/reservations", "customer", {**reservation_body, "quantity": 4}, expected=409, key=key)
    product_path = "/api/v1/products/" + product["id"]
    assert request("GET", product_path, "customer")["availableQuantity"] == 4
    cancel_path = "/api/v1/reservations/" + reservation["id"] + "/cancel"
    cancelled = request("POST", cancel_path, "customer", {})
    assert cancelled["status"] == "CANCELLED"
    assert request("POST", cancel_path, "customer", {}) == cancelled
    assert request("POST", "/api/v1/reservations", "customer", reservation_body, key=key) == cancelled
    assert request("GET", product_path, "customer")["availableQuantity"] == 7
    print("PASS: readiness, authentication, create, reserve, keyed replay, changed-payload conflict, cancel, and repeated cancellation")
    print("Demo product:", product["id"])
    print("Demo reservation:", reservation["id"], "(cancelled)")


if __name__ == "__main__":
    main()
