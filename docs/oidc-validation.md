# Production authentication validation

`scripts/oidc-drill.py` starts the actual application image with the `prod` profile and a real
Keycloak identity provider. It creates disposable PostgreSQL, applies the application's migration
mode, and runs the API with the restricted `inventory_app` account. The running local Compose
service and its data are not used or changed.

The fixture is pinned to Keycloak **26.7.3**, the version in the official Docker getting-started
guide when checked on September 13, 2026. Its container manifest is pinned in the script, and
the report records the resolved image identities. [Official Keycloak Docker guide](https://www.keycloak.org/getting-started/getting-started-docker)

## Run the drill

Build the application image and make the two supporting images available:

```sh
docker build -t inventory-cloud-service-app .
docker pull postgres:17-bookworm
docker pull quay.io/keycloak/keycloak:26.7.3@sha256:ff4257d0d64efbe99ed1ddfaf07765cc3c36dc7518bf8324d41961327f441c54
python3 scripts/oidc-drill.py
```

The script uses Python's standard library and Docker; certificate generation and Java truststore
creation run inside the application image. Allow approximately three minutes, including a real
token-expiry wait beyond the application's allowed clock skew. A successful run exits zero and
writes `reports/oidc/latest.json`. `--image`, `--database-image`, `--keycloak-image`, and `--report`
allow explicit overrides. The service image is resolved before the drill so replacing its tag
during the run cannot mix application versions.

## What is checked

- The issuer presents a certificate signed by a newly generated private CA. Discovery and token
  requests use ordinary Python certificate and hostname validation. An untrusted-client check
  confirms that the certificate is rejected without that CA.
- The application trusts that CA through a dedicated Java PKCS12 truststore and obtains discovery
  and signing keys from `https://keycloak:8443/realms/inventory-drill`. No TLS verification bypass
  or plain-HTTP signing-key override is configured.
- Keycloak issues genuine signed access tokens with audience `inventory-api`, an application
  `roles` claim, and different subjects for administrator, customer, and another customer.
- The production API rejects anonymous and Basic authentication, restricts inventory creation
  to the administrator, and uses the authenticated JWT subject for reservation ownership.
- The customer can reserve and cancel inventory. Another customer cannot read, list, or cancel
  that reservation. Repeated cancellation restores the reserved stock exactly once.
- Retrying a reservation with the same `Idempotency-Key` and customer token returns the original
  reservation with HTTP 200 after the first HTTP 201, consuming stock only once.
- A genuine Keycloak token with another audience and a token with an altered signature both
  receive HTTP 401. A genuine short-lived token works while valid, receives HTTP 401 after expiry
  plus clock skew, and a newly issued token succeeds afterward.

Realm import, explicit hostname configuration, and PEM certificate inputs use the supported
Keycloak interfaces. The generated realm maps realm roles into the API's `roles` claim and uses
an audience mapper for `inventory-api`. [Realm import and containers](https://www.keycloak.org/server/containers),
[TLS configuration](https://www.keycloak.org/server/enabletls),
[Token roles and audiences](https://www.keycloak.org/docs/latest/server_admin/index.html#_oidc_clients)

## Isolation and limits

All resources have unique names and ownership labels. PostgreSQL uses temporary memory-backed
storage. The issuer and API expose only random loopback ports. Credentials, certificates, and
private keys are generated per run in a private temporary directory under ignored `.local/`;
passwords and tokens are omitted from reports and command arguments. Container cleanup checks
ownership before removal, and the generated files are removed when the drill ends. Abruptly
killing the process or losing the Docker daemon can require manual cleanup of the exact labelled
resources; failed cleanup is reported rather than treated as success.

Keycloak uses its disposable `dev-file` database and fixture clients with direct access grants
to acquire tokens without a browser. Those are test choices. A deployed identity provider needs
persistent storage and a browser authorization-code flow with PKCE as appropriate. This drill
does not validate interactive login, MFA, cloud networking, an external identity-provider account,
or the public load balancer's certificate. The API's loopback connection and fixture PostgreSQL
connection are HTTP/plain JDBC; issuer discovery and token/key retrieval use verified HTTPS.

The existing `ProductionJwtIntegrationTest` also checks wrong and missing audiences, issuer
mismatch, expired tokens, bad signatures, permissions, and ownership using a deterministic local
signing-key fixture. The real Keycloak drill checks the integration with an independently running
identity provider and its realm configuration.
