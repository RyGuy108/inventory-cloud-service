package com.ryanshankar.inventory.api;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.Date;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import com.nimbusds.jose.JOSEException;
import com.nimbusds.jose.JWSAlgorithm;
import com.nimbusds.jose.JWSHeader;
import com.nimbusds.jose.crypto.RSASSASigner;
import com.nimbusds.jose.jwk.JWKSet;
import com.nimbusds.jose.jwk.RSAKey;
import com.nimbusds.jose.jwk.gen.RSAKeyGenerator;
import com.nimbusds.jwt.JWTClaimsSet;
import com.nimbusds.jwt.SignedJWT;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.postgresql.PostgreSQLContainer;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/** Uses the production security configuration, real RSA signatures, and a local public-key endpoint. */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = "spring.flyway.enabled=true")
@ActiveProfiles("prod")
@Testcontainers
@Timeout(60)
class ProductionJwtIntegrationTest {

    private static final String ISSUER = "https://issuer.inventory.test";
    private static final String AUDIENCE = "inventory-api";
    private static final String PRODUCTS = "/api/v1/products";
    private static final String RESERVATIONS = "/api/v1/reservations";
    private static final JsonMapper JSON = JsonMapper.builder().build();
    private static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
    private static final RSAKey SIGNING_KEY = generateKey("inventory-test-key");
    private static final RSAKey UNTRUSTED_KEY = generateKey("inventory-test-key");
    private static final HttpServer JWKS = startJwksServer();

    @Container
    static final PostgreSQLContainer POSTGRES = new PostgreSQLContainer("postgres:17-alpine");

    @DynamicPropertySource
    static void configuration(DynamicPropertyRegistry properties) {
        properties.add("spring.datasource.url", POSTGRES::getJdbcUrl);
        properties.add("spring.datasource.username", POSTGRES::getUsername);
        properties.add("spring.datasource.password", POSTGRES::getPassword);
        properties.add("spring.security.oauth2.resourceserver.jwt.issuer-uri", () -> ISSUER);
        properties.add("spring.security.oauth2.resourceserver.jwt.audiences", () -> AUDIENCE);
        properties.add("spring.security.oauth2.resourceserver.jwt.jwk-set-uri",
                () -> "http://127.0.0.1:" + JWKS.getAddress().getPort() + "/jwks");
    }

    @LocalServerPort
    private int port;

    @AfterAll
    static void stopJwksServer() {
        JWKS.stop(0);
    }

    @Test
    void productionRequiresBearerTokensAndRejectsLocalBasicCredentials() throws Exception {
        var anonymous = request("GET", PRODUCTS, null, null);
        assertEquals(401, anonymous.statusCode());
        assertEquals("Bearer", anonymous.headers().firstValue("WWW-Authenticate").orElse(""));
        String basic = "Basic " + Base64.getEncoder().encodeToString(
                "admin:Test-admin-7dQ9!".getBytes(StandardCharsets.UTF_8));
        assertEquals(401, request("GET", PRODUCTS, basic, null).statusCode());
    }

    @Test
    void acceptsCorrectlySignedCustomerToken() throws Exception {
        JsonNode page = expectJson(request("GET", PRODUCTS, bearer("jwt-customer", "CUSTOMER"), null), 200);
        assertTrue(page.path("items").isArray());
    }

    @Test
    void onlyAdminRoleCanCreateProducts() throws Exception {
        var product = productBody();
        assertEquals(403, request("POST", PRODUCTS, bearer("jwt-customer", "CUSTOMER"), product).statusCode());
        JsonNode created = expectJson(request("POST", PRODUCTS, bearer("jwt-admin", "ADMIN"), product), 201);
        assertEquals(product.get("sku"), created.path("sku").stringValue());
    }

    @Test
    void rejectsWrongIssuerWrongAudienceMissingAudienceExpiredAndInvalidSignature() throws Exception {
        Instant validExpiry = Instant.now().plusSeconds(600);
        Map<String, String> invalidTokens = Map.of(
                "wrong issuer", token(SIGNING_KEY, "https://untrusted.inventory.test", AUDIENCE,
                        "jwt-customer", "CUSTOMER", validExpiry),
                "wrong audience", token(SIGNING_KEY, ISSUER, "different-service",
                        "jwt-customer", "CUSTOMER", validExpiry),
                "missing audience", token(SIGNING_KEY, ISSUER, null,
                        "jwt-customer", "CUSTOMER", validExpiry),
                "expired", token(SIGNING_KEY, ISSUER, AUDIENCE,
                        "jwt-customer", "CUSTOMER", Instant.now().minusSeconds(300)),
                "invalid signature", token(UNTRUSTED_KEY, ISSUER, AUDIENCE,
                        "jwt-customer", "CUSTOMER", validExpiry));
        for (var entry : invalidTokens.entrySet()) {
            var response = request("GET", PRODUCTS, "Bearer " + entry.getValue(), null);
            assertEquals(401, response.statusCode(), () -> entry.getKey() + ": " + response.body());
        }
    }

    @Test
    void reservationOwnershipUsesVerifiedJwtSubject() throws Exception {
        JsonNode product = expectJson(request("POST", PRODUCTS, bearer("jwt-admin", "ADMIN"), productBody()), 201);
        String owner = bearer("owner-subject", "CUSTOMER");
        String other = bearer("different-subject", "CUSTOMER");
        JsonNode reservation = expectJson(request("POST", RESERVATIONS, owner,
                Map.of("productId", product.path("id").stringValue(), "quantity", 1)), 201);
        assertEquals("owner-subject", reservation.path("customerId").stringValue());
        String path = RESERVATIONS + "/" + reservation.path("id").stringValue();
        assertEquals(404, request("GET", path, other, null).statusCode());
        assertEquals(404, request("POST", path + "/cancel", other, null).statusCode());
        assertEquals("ACTIVE", expectJson(request("GET", path, owner, null), 200).path("status").stringValue());
        assertEquals("CANCELLED", expectJson(request("POST", path + "/cancel", owner, null), 200)
                .path("status").stringValue());
    }

    private static Map<String, Object> productBody() {
        return Map.of("sku", "JWT-" + UUID.randomUUID().toString().toUpperCase(java.util.Locale.ROOT),
                "name", "JWT integration product", "initialQuantity", 3);
    }

    private static String bearer(String subject, String role) throws JOSEException {
        return "Bearer " + token(SIGNING_KEY, ISSUER, AUDIENCE, subject, role, Instant.now().plusSeconds(600));
    }

    private static String token(RSAKey key, String issuer, String audience, String subject, String role,
            Instant expiresAt) throws JOSEException {
        var claims = new JWTClaimsSet.Builder()
                .issuer(issuer)
                .subject(subject)
                .claim("roles", List.of(role))
                .issueTime(Date.from(Instant.now().minusSeconds(600)))
                .expirationTime(Date.from(expiresAt));
        if (audience != null) {
            claims.audience(audience);
        }
        var jwt = new SignedJWT(new JWSHeader.Builder(JWSAlgorithm.RS256).keyID(key.getKeyID()).build(), claims.build());
        jwt.sign(new RSASSASigner(key));
        return jwt.serialize();
    }

    private HttpResponse<String> request(String method, String path, String authorization, Object body) throws Exception {
        var request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .timeout(Duration.ofSeconds(20));
        if (authorization != null) {
            request.header("Authorization", authorization);
        }
        if (body != null || "POST".equals(method)) {
            request.header("Content-Type", "application/json");
        }
        return HTTP.send(request.method(method, body == null ? HttpRequest.BodyPublishers.noBody()
                        : HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body))).build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private static JsonNode expectJson(HttpResponse<String> response, int status) {
        assertEquals(status, response.statusCode(), response.body());
        return JSON.readTree(response.body());
    }

    private static RSAKey generateKey(String keyId) {
        try {
            return new RSAKeyGenerator(2048).keyID(keyId).generate();
        } catch (JOSEException exception) {
            throw new ExceptionInInitializerError(exception);
        }
    }

    private static HttpServer startJwksServer() {
        try {
            var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            byte[] publicKeys = new JWKSet(SIGNING_KEY.toPublicJWK()).toString().getBytes(StandardCharsets.UTF_8);
            server.createContext("/jwks", exchange -> {
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.sendResponseHeaders(200, publicKeys.length);
                try (var output = exchange.getResponseBody()) {
                    output.write(publicKeys);
                }
            });
            server.start();
            return server;
        } catch (IOException exception) {
            throw new ExceptionInInitializerError(exception);
        }
    }
}
