#!/usr/bin/env python3
"""Validate the production API against real Keycloak over verified TLS in disposable Docker containers."""
import argparse
import base64
import http.client
import importlib.util
import json
import os
from pathlib import Path
import secrets
import ssl
import tempfile
import time
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:26.7.3@sha256:ff4257d0d64efbe99ed1ddfaf07765cc3c36dc7518bf8324d41961327f441c54"
REALM = "inventory-drill"
ISSUER = f"https://keycloak:8443/realms/{REALM}"
spec = importlib.util.spec_from_file_location("inventory_recovery_fixture", Path(__file__).with_name("recovery-drill.py"))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
docker, require = recovery.docker, recovery.require


def token_claims(token):
    """Inspect fixture-issued claims for test expectations; the API verifies the signature."""
    encoded = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


def realm_definition(user_passwords, client_secrets):
    clients = []
    for name, audience, lifetime in (("inventory-cli", "inventory-api", 300),
                                     ("wrong-audience", "different-api", 300),
                                     ("short-lived", "inventory-api", 2)):
        clients.append({
            "clientId": name, "enabled": True, "protocol": "openid-connect",
            "publicClient": False, "secret": client_secrets[name],
            "standardFlowEnabled": False, "directAccessGrantsEnabled": True,
            "serviceAccountsEnabled": False, "fullScopeAllowed": True,
            "attributes": {"access.token.lifespan": str(lifetime)},
            "protocolMappers": [
                {"name": "inventory-audience", "protocol": "openid-connect",
                 "protocolMapper": "oidc-audience-mapper",
                 "config": {"included.custom.audience": audience, "access.token.claim": "true",
                            "id.token.claim": "false", "introspection.token.claim": "true"}},
                {"name": "inventory-roles", "protocol": "openid-connect",
                 "protocolMapper": "oidc-usermodel-realm-role-mapper",
                 "config": {"claim.name": "roles", "jsonType.label": "String", "multivalued": "true",
                            "access.token.claim": "true", "id.token.claim": "false"}},
            ],
        })
    return {
        "realm": REALM, "enabled": True, "sslRequired": "all", "registrationAllowed": False,
        "resetPasswordAllowed": False, "accessTokenLifespan": 300,
        "roles": {"realm": [{"name": "ADMIN"}, {"name": "CUSTOMER"}]},
        "clients": clients,
        "users": [{"username": name, "enabled": True, "emailVerified": True,
                   "email": name + "@inventory.example.invalid", "firstName": "Drill", "lastName": name,
                   "credentials": [{"type": "password", "value": password, "temporary": False}],
                   "realmRoles": ["ADMIN" if name == "admin" else "CUSTOMER"]}
                  for name, password in user_passwords.items()],
    }


def private_file(directory, name, content, executable=False):
    path = directory / name
    path.write_text(content)
    # The parent is 0700. Files selectively bind-mounted into non-root containers must be readable there.
    path.chmod(0o755 if executable else 0o644)
    return path


def mount(path, destination):
    return ["--mount", f"type=bind,src={path},dst={destination},readonly"]


def wait_exit(fixture, name, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = fixture.owned("container", name)["State"]
        if not state["Running"]:
            require(state["ExitCode"] == 0, "Disposable preparation container failed")
            return
        time.sleep(0.3)
    raise RuntimeError("Disposable preparation container timed out")


def prepare_certificates(fixture, directory, password):
    private_file(directory, "leaf.ext", "\n".join([
        "basicConstraints=critical,CA:FALSE", "keyUsage=critical,digitalSignature,keyEncipherment",
        "extendedKeyUsage=serverAuth", "subjectAltName=DNS:keycloak,IP:127.0.0.1", "",
    ]))
    script = private_file(directory, "make-certificates.sh", """#!/bin/sh
set -eu
cd /fixture
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=Inventory-Drill-CA \\
  -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign \\
  -keyout ca.key -out ca.crt
openssl req -new -newkey rsa:2048 -nodes -subj /CN=keycloak -keyout keycloak.key -out keycloak.csr
openssl x509 -req -in keycloak.csr -CA ca.crt -CAkey ca.key -CAcreateserial \\
  -days 2 -sha256 -extfile leaf.ext -out keycloak.crt
keytool -importcert -noprompt -alias inventory-drill-ca -file ca.crt \\
  -keystore truststore.p12 -storetype PKCS12 -storepass:env TRUSTSTORE_PASSWORD
chmod 644 ca.crt keycloak.crt keycloak.key truststore.p12
""", executable=True)
    name = fixture.run_container("certificate-builder", fixture.image, [
        "--user", "0", "--entrypoint", "/fixture/make-certificates.sh",
        "--mount", f"type=bind,src={directory},dst=/fixture", "--env", "TRUSTSTORE_PASSWORD",
        "--memory", "256m", "--cpus", "1",
    ], {"TRUSTSTORE_PASSWORD": password})
    wait_exit(fixture, name)
    require((directory / "truststore.p12").is_file(), "Java truststore was not created")


class IdentityProvider:
    def __init__(self, port, certificate, users, clients):
        self.port, self.users, self.clients = port, users, clients
        self.context = ssl.create_default_context(cafile=str(certificate))

    def request(self, method, path, form=None):
        connection = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.context, timeout=10)
        try:
            body = urlencode(form) if form is not None else None
            connection.request(method, path, body, {"Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            payload = response.read()
            require(response.status == 200, f"Identity provider {path}: received HTTP {response.status}")
            return json.loads(payload)
        finally:
            connection.close()

    def token(self, username, client="inventory-cli"):
        result = self.request("POST", f"/realms/{REALM}/protocol/openid-connect/token", {
            "grant_type": "password", "client_id": client, "client_secret": self.clients[client],
            "username": username, "password": self.users[username],
        })
        require(result.get("token_type", "").lower() == "bearer", "Keycloak did not issue a bearer token")
        return result["access_token"]


class BearerApi:
    def __init__(self, port):
        self.port = port

    def expect(self, method, path, token=None, body=None, status=200, authorization=None, extra_headers=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if authorization is not None:
            headers["Authorization"] = authorization
        headers.update(extra_headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            payload = response.read()
            require(response.status == status, f"API {method} {path}: expected {status}, received {response.status}")
            return json.loads(payload) if payload else None
        finally:
            connection.close()


def start_identity_provider(fixture, directory, keycloak_image, users, clients):
    realm = private_file(directory, "inventory-drill-realm.json", json.dumps(realm_definition(users, clients)))
    script = private_file(directory, "start-keycloak.sh", """#!/bin/sh
exec /opt/keycloak/bin/kc.sh start --import-realm --db=dev-file --cache=local \\
  --hostname=https://keycloak:8443 --http-enabled=false --https-port=8443 \\
  --https-certificate-file=/fixture/keycloak.crt --https-certificate-key-file=/fixture/keycloak.key
""", executable=True)
    name = fixture.run_container("keycloak", keycloak_image, [
        "--network-alias", "keycloak", "--publish", "127.0.0.1::8443",
        "--memory", "1536m", "--cpus", "2", "--entrypoint", "/fixture/start-keycloak.sh",
        *mount(script, "/fixture/start-keycloak.sh"), *mount(directory / "keycloak.crt", "/fixture/keycloak.crt"),
        *mount(directory / "keycloak.key", "/fixture/keycloak.key"),
        *mount(realm, f"/opt/keycloak/data/import/{REALM}-realm.json"),
    ])
    binding = fixture.owned("container", name)["NetworkSettings"]["Ports"]["8443/tcp"][0]
    require(binding["HostIp"] == "127.0.0.1", "Identity provider must bind only to loopback")
    provider = IdentityProvider(int(binding["HostPort"]), directory / "ca.crt", users, clients)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        require(fixture.owned("container", name)["State"]["Running"], "Disposable Keycloak exited during startup")
        try:
            discovery = provider.request("GET", f"/realms/{REALM}/.well-known/openid-configuration")
            require(discovery["issuer"] == ISSUER, "Discovery issuer does not match the production configuration")
            require(discovery["jwks_uri"].startswith(ISSUER + "/"), "Discovery keys must come from the HTTPS issuer")
            return provider
        except (OSError, http.client.HTTPException, RuntimeError):
            time.sleep(1)
    raise RuntimeError("Disposable Keycloak discovery did not become ready")


def start_production_api(fixture, directory, database, truststore_password):
    private_file(directory, "java-args", "\n".join([
        "-Djavax.net.ssl.trustStore=/fixture/truststore.p12", "-Djavax.net.ssl.trustStoreType=PKCS12",
        "-Djavax.net.ssl.trustStorePassword=" + truststore_password, "",
    ]))
    script = private_file(directory, "start-production-api.sh",
                          "#!/bin/sh\nexec java @/fixture/java-args -jar /app/app.jar\n", executable=True)
    environment = {
        "SPRING_PROFILES_ACTIVE": "prod", "SPRING_FLYWAY_ENABLED": "false",
        "DB_URL": f"jdbc:postgresql://{database}:5432/inventory",
        "DB_USERNAME": "inventory_app", "DB_PASSWORD": fixture.runtime_passwords[database],
        "JWT_ISSUER_URI": ISSUER, "JWT_AUDIENCE": "inventory-api",
    }
    name = fixture.run_container("production-api", fixture.image, [
        "--publish", "127.0.0.1::8080", "--read-only", "--tmpfs", "/tmp:rw,size=128m,mode=1777",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "768m", "--cpus", "1",
        "--entrypoint", "/fixture/start-production-api.sh", *mount(script, "/fixture/start-production-api.sh"),
        *mount(directory / "java-args", "/fixture/java-args"), *mount(directory / "truststore.p12", "/fixture/truststore.p12"),
        *[part for key in environment for part in ("--env", key)],
    ], environment)
    binding = fixture.owned("container", name)["NetworkSettings"]["Ports"]["8080/tcp"][0]
    require(binding["HostIp"] == "127.0.0.1", "Production-profile API must bind only to loopback")
    api = BearerApi(int(binding["HostPort"]))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        require(fixture.owned("container", name)["State"]["Running"], "Production-profile API exited during startup")
        try:
            require(api.expect("GET", "/actuator/health/readiness")["status"] == "UP", "API is not ready")
            return api
        except (OSError, http.client.HTTPException, RuntimeError):
            time.sleep(0.5)
    raise RuntimeError("Production-profile API did not become ready")


def run_checks(api, provider, report):
    checks = report["checks"]
    tokens = {name: provider.token(name) for name in ("admin", "customer", "other")}
    short_token = provider.token("customer", "short-lived")
    short_claims = token_claims(short_token)
    require(short_claims["exp"] - short_claims["iat"] <= 5, "Fixture short-lived token did not receive the configured expiry")
    api.expect("GET", "/api/v1/products", short_token)
    for name, token in tokens.items():
        claims = token_claims(token)
        audience = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        require(claims["iss"] == ISSUER and "inventory-api" in audience, "Token issuer/audience mapper is incorrect")
        require(("ADMIN" if name == "admin" else "CUSTOMER") in claims["roles"], "Role mapper is incorrect")
    checks.append("real Keycloak tokens contain the configured issuer, audience, and application roles")
    api.expect("GET", "/api/v1/products", status=401)
    api.expect("GET", "/api/v1/products", authorization="Basic " + base64.b64encode(b"admin:invalid").decode(), status=401)
    api.expect("GET", "/api/v1/products", tokens["customer"])
    checks.append("production rejects anonymous and Basic authentication and accepts a signed customer token")
    product_body = {"sku": "OIDC-" + secrets.token_hex(6).upper(), "name": "OIDC validation product", "initialQuantity": 9}
    api.expect("POST", "/api/v1/products", tokens["customer"], product_body, 403)
    product = api.expect("POST", "/api/v1/products", tokens["admin"], product_body, 201)
    checks.append("ADMIN can create inventory and CUSTOMER cannot")
    reservation_body = {"productId": product["id"], "quantity": 3}
    retry_headers = {"Idempotency-Key": "oidc-" + secrets.token_hex(16)}
    reservation = api.expect("POST", "/api/v1/reservations", tokens["customer"], reservation_body, 201,
                             extra_headers=retry_headers)
    replay = api.expect("POST", "/api/v1/reservations", tokens["customer"], reservation_body, 200,
                        extra_headers=retry_headers)
    require(replay == reservation, "A retried reservation with the same key must return the original reservation")
    checks.append("a genuine customer token can create and replay an idempotent reservation without duplicate stock consumption")
    require(reservation["customerId"] == token_claims(tokens["customer"])["sub"], "Reservation ownership must use the verified subject")
    reservation_path, product_path = "/api/v1/reservations/" + reservation["id"], "/api/v1/products/" + product["id"]
    require(api.expect("GET", product_path, tokens["customer"])["availableQuantity"] == 6, "Reservation did not subtract stock")
    api.expect("GET", reservation_path, tokens["other"], status=404)
    api.expect("POST", reservation_path + "/cancel", tokens["other"], {}, 404)
    other_page = api.expect("GET", "/api/v1/reservations", tokens["other"])
    require(other_page["totalElements"] == 0, "Another customer can see a private reservation in the listing")
    checks.append("reservation ownership uses JWT subject and another customer cannot read, list, or cancel it")
    cancelled = api.expect("POST", reservation_path + "/cancel", tokens["customer"], {})
    require(cancelled["status"] == "CANCELLED", "Customer cancellation failed")
    require(api.expect("POST", reservation_path + "/cancel", tokens["customer"], {}) == cancelled, "Repeated cancellation changed state")
    require(api.expect("GET", product_path, tokens["customer"])["availableQuantity"] == 9, "Cancellation did not restore stock exactly once")
    checks.append("customer can reserve and cancel; repeated cancellation restores stock exactly once")
    wrong_audience = provider.token("customer", "wrong-audience")
    api.expect("GET", "/api/v1/products", wrong_audience, status=401)
    checks.append("a genuine Keycloak token for a different audience is rejected")
    parts = tokens["customer"].split(".")
    signature = bytearray(base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4)))
    signature[0] ^= 1
    parts[2] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    api.expect("GET", "/api/v1/products", ".".join(parts), status=401)
    checks.append("a token with a modified signature is rejected")
    # Spring Security permits 60 seconds of clock skew. Wait beyond that without changing clocks or keys.
    expires_after_skew = short_claims["exp"] + 65
    while time.time() < expires_after_skew:
        remaining = expires_after_skew - time.time()
        print(f"Waiting for the genuine short-lived token to expire beyond validation clock skew ({int(remaining)}s).", flush=True)
        time.sleep(min(15, remaining))
    api.expect("GET", "/api/v1/products", short_token, status=401)
    api.expect("GET", "/api/v1/products", provider.token("customer"))
    checks.append("a genuine short-lived token works before expiry, is rejected after clock skew, and a fresh token succeeds")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="inventory-cloud-service-app")
    parser.add_argument("--database-image", default="postgres:17-bookworm")
    parser.add_argument("--keycloak-image", default=KEYCLOAK_IMAGE)
    parser.add_argument("--report", type=Path, default=ROOT / "reports/oidc/latest.json")
    args = parser.parse_args()
    fixture, report = None, {"started_at": recovery.utc_now(), "checks": [], "passed": False}
    ROOT.joinpath(".local").mkdir(exist_ok=True)
    try:
        fixture = recovery.Fixture(args.image, args.database_image)
        keycloak_id = docker("image", "inspect", args.keycloak_image, "--format", "{{.Id}}").decode().strip()
        report.update(fixture.metadata())
        report.update({"authentication": "production Bearer tokens from real Keycloak over verified TLS",
                       "keycloak_image": args.keycloak_image, "keycloak_image_id": keycloak_id,
                       "issuer": ISSUER, "audience": "inventory-api", "tls_certificate_validation": True,
                       "tls_hostname_validation": True, "database_runtime_role": "inventory_app",
                       "token_acquisition": "direct access grants enabled only on disposable fixture clients",
                       "identity_provider_storage": "disposable container dev-file database"})
        fixture.create_network()
        with tempfile.TemporaryDirectory(prefix="oidc-drill-", dir=ROOT / ".local") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            users = {name: secrets.token_urlsafe(24) for name in ("admin", "customer", "other")}
            clients = {name: secrets.token_urlsafe(32) for name in ("inventory-cli", "wrong-audience", "short-lived")}
            truststore_password = secrets.token_urlsafe(24)
            print("Preparing isolated certificates, PostgreSQL, and the Keycloak issuer.", flush=True)
            prepare_certificates(fixture, directory, truststore_password)
            database = fixture.start_database("oidc-db")
            fixture.migrate_database("oidc-migrate", database)
            provider = start_identity_provider(fixture, directory, keycloak_id, users, clients)
            # Positive TLS does not silently fall back to accepting an untrusted self-signed certificate.
            connection = http.client.HTTPSConnection("127.0.0.1", provider.port, context=ssl.create_default_context(), timeout=5)
            try:
                connection.request("GET", f"/realms/{REALM}/.well-known/openid-configuration")
            except ssl.SSLCertVerificationError:
                report["checks"].append("the issuer certificate is rejected without the generated CA trust")
            else:
                raise RuntimeError("Untrusted issuer certificate unexpectedly passed default trust validation")
            finally:
                connection.close()
            print("Starting the production-profile API with restricted database credentials and explicit CA trust.", flush=True)
            api = start_production_api(fixture, directory, database, truststore_password)
            run_checks(api, provider, report)
            report["passed"] = True
            # Clean up containers before deleting their private bind-mounted files.
            report["cleanup_errors"] = fixture.cleanup()
            fixture = None
            require(not report["cleanup_errors"], "Disposable OIDC resource cleanup failed")
    except Exception as exc:
        report["passed"] = False
        report["error"] = str(exc)
    finally:
        if fixture is not None:
            report["cleanup_errors"] = fixture.cleanup()
        if report.get("cleanup_errors"):
            report["passed"] = False
        report["finished_at"] = recovery.utc_now()
        recovery.write_report(args.report, report)
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {len(report['checks'])} real OIDC checks. Report: {args.report}")
    if report.get("error"):
        print(report["error"])
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
