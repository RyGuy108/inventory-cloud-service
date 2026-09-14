package com.ryanshankar.inventory.api;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import com.ryanshankar.inventory.repository.ReservationKeyLock;
import com.ryanshankar.inventory.service.InventoryService;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.boot.micrometer.metrics.test.autoconfigure.AutoConfigureMetrics;
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

/**
 * Exercises real HTTP requests, security filters, migrations, transactions, and PostgreSQL locks.
 * Docker is required: the suite deliberately fails instead of skipping database verification.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = {
        "app.auth.admin-password=Test-admin-7dQ9!",
        "app.auth.customer-password=Test-customer-4mP8!",
        "app.auth.other-password=Test-other-2vR6!"
})
@ActiveProfiles("test")
@AutoConfigureMetrics
@Testcontainers
@Timeout(90)
class InventoryApiIntegrationTest {

    private static final String PRODUCTS = "/api/v1/products";
    private static final String RESERVATIONS = "/api/v1/reservations";
    private static final Credentials ADMIN = new Credentials("admin", "Test-admin-7dQ9!");
    private static final Credentials CUSTOMER = new Credentials("customer", "Test-customer-4mP8!");
    private static final Credentials OTHER = new Credentials("other", "Test-other-2vR6!");
    private static final JsonMapper JSON = JsonMapper.builder().build();
    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();

    @Container
    static final PostgreSQLContainer POSTGRES = new PostgreSQLContainer("postgres:17-alpine");

    @DynamicPropertySource
    static void databaseProperties(DynamicPropertyRegistry properties) {
        properties.add("spring.datasource.url", POSTGRES::getJdbcUrl);
        properties.add("spring.datasource.username", POSTGRES::getUsername);
        properties.add("spring.datasource.password", POSTGRES::getPassword);
    }

    @LocalServerPort
    private int port;

    @Autowired
    private PlatformTransactionManager transactions;

    @Autowired
    private ReservationKeyLock reservationKeys;

    @Autowired
    private MeterRegistry meters;

    @Autowired
    private InventoryService inventory;

    @Test
    void createsRestocksReservesAndCancelsInventory() throws Exception {
        String sku = uniqueSku();
        var created = request("POST", PRODUCTS, ADMIN,
                Map.of("sku", sku, "name", "Mechanical keyboard", "initialQuantity", 10));
        JsonNode product = expectJson(created, 201);
        String productId = text(product, "id");
        assertEquals(sku, text(product, "sku"));
        assertEquals("Mechanical keyboard", text(product, "name"));
        assertEquals(10, product.path("availableQuantity").intValue());
        assertFalse(text(product, "createdAt").isBlank());

        var restocked = expectJson(request("POST", PRODUCTS + "/" + productId + "/stock", ADMIN,
                Map.of("quantity", 3)), 200);
        assertEquals(13, restocked.path("availableQuantity").intValue());

        var reservation = reserve(productId, 4, CUSTOMER);
        String reservationId = text(reservation, "id");
        assertEquals(productId, text(reservation, "productId"));
        assertEquals(4, reservation.path("quantity").intValue());
        assertEquals("customer", text(reservation, "customerId"));
        assertEquals("ACTIVE", text(reservation, "status"));
        assertFalse(text(reservation, "createdAt").isBlank());
        assertEquals(9, stock(productId));

        var fetched = expectJson(request("GET", RESERVATIONS + "/" + reservationId, CUSTOMER, null), 200);
        assertEquals(reservationId, text(fetched, "id"));

        var cancelled = expectJson(request("POST", RESERVATIONS + "/" + reservationId + "/cancel",
                CUSTOMER, null), 200);
        assertEquals("CANCELLED", text(cancelled, "status"));
        assertFalse(text(cancelled, "cancelledAt").isBlank());
        assertEquals(13, stock(productId));

        var repeated = expectJson(request("POST", RESERVATIONS + "/" + reservationId + "/cancel",
                CUSTOMER, null), 200);
        assertEquals(text(cancelled, "cancelledAt"), text(repeated, "cancelledAt"));
        assertEquals(13, stock(productId), "Repeated cancellation must never add stock twice");
    }

    @Test
    void rejectsDuplicateSkuRegardlessOfCase() throws Exception {
        String sku = uniqueSku();
        expectJson(request("POST", PRODUCTS, ADMIN,
                Map.of("sku", sku.toLowerCase(java.util.Locale.ROOT), "name", "First item",
                        "initialQuantity", 2)), 201);
        expectProblem(request("POST", PRODUCTS, ADMIN,
                Map.of("sku", sku, "name", "Duplicate item", "initialQuantity", 100)), 409, PRODUCTS);
    }

    @Test
    void invalidProductInputDoesNotCreateInventory() throws Exception {
        expectProblem(request("POST", PRODUCTS, ADMIN,
                Map.of("sku", uniqueSku(), "name", " ", "initialQuantity", 1)), 400, PRODUCTS);
        expectProblem(request("POST", PRODUCTS, ADMIN,
                Map.of("sku", uniqueSku(), "name", "Invalid stock", "initialQuantity", -1)), 400, PRODUCTS);
        expectProblem(request("POST", PRODUCTS, ADMIN,
                Map.of("sku", " ", "name", "Missing SKU", "initialQuantity", 1)), 400, PRODUCTS);
    }

    @Test
    void zeroInitialStockIsValidButCannotBeReserved() throws Exception {
        String id = createProduct(0);
        assertEquals(0, stock(id));
        expectProblem(request("POST", RESERVATIONS, CUSTOMER,
                Map.of("productId", id, "quantity", 1)), 409, RESERVATIONS);
    }

    @Test
    void rejectsNonPositiveAndMissingQuantitiesWithoutChangingStock() throws Exception {
        String id = createProduct(5);
        for (int quantity : new int[] {0, -1}) {
            expectProblem(request("POST", RESERVATIONS, CUSTOMER,
                    Map.of("productId", id, "quantity", quantity)), 400, RESERVATIONS);
            expectProblem(request("POST", PRODUCTS + "/" + id + "/stock", ADMIN,
                    Map.of("quantity", quantity)), 400, PRODUCTS + "/" + id + "/stock");
        }
        expectProblem(request("POST", RESERVATIONS, CUSTOMER, Map.of("productId", id)),
                400, RESERVATIONS);
        expectProblem(request("POST", PRODUCTS + "/" + id + "/stock", ADMIN, Map.of()),
                400, PRODUCTS + "/" + id + "/stock");
        assertEquals(5, stock(id));
    }

    @Test
    void failedReservationDoesNotConsumeStockOrPersistAReservation() throws Exception {
        String id = createProduct(2);
        long before = reservationCount(CUSTOMER);
        expectProblem(request("POST", RESERVATIONS, CUSTOMER,
                Map.of("productId", id, "quantity", 3)), 409, RESERVATIONS);
        assertEquals(2, stock(id));
        assertEquals(before, reservationCount(CUSTOMER));
        reserve(id, 2, CUSTOMER);
        assertEquals(0, stock(id));
        assertEquals(before + 1, reservationCount(CUSTOMER));
    }

    @Test
    void failedCancellationPreservesReservationUntilStockCanBeRestored() throws Exception {
        String id = createProduct(1_000_000);
        String reservationId = text(reserve(id, 2, CUSTOMER), "id");
        expectJson(request("POST", PRODUCTS + "/" + id + "/stock", ADMIN, Map.of("quantity", 2)), 200);
        String path = RESERVATIONS + "/" + reservationId;

        expectProblem(request("POST", path + "/cancel", CUSTOMER, null), 409, path + "/cancel");
        var unchanged = expectJson(request("GET", path, CUSTOMER, null), 200);
        assertEquals("ACTIVE", text(unchanged, "status"));
        assertEquals(1_000_000, stock(id));

        reserve(id, 2, CUSTOMER);
        var cancelled = expectJson(request("POST", path + "/cancel", CUSTOMER, null), 200);
        assertEquals("CANCELLED", text(cancelled, "status"));
        assertEquals(1_000_000, stock(id));
    }

    @Test
    void requiresAuthenticationAndRestrictsInventoryWritesToAdministrators() throws Exception {
        var anonymous = request("GET", PRODUCTS, null, null);
        assertEquals(401, anonymous.statusCode());
        assertTrue(anonymous.headers().firstValue("WWW-Authenticate").orElse("").startsWith("Basic "));
        assertEquals(401, request("GET", PRODUCTS, new Credentials("customer", "incorrect"), null)
                .statusCode());
        String id = createProduct(5);
        expectJson(request("GET", PRODUCTS + "/" + id, CUSTOMER, null), 200);
        expectProblem(request("POST", PRODUCTS, CUSTOMER,
                Map.of("sku", uniqueSku(), "name", "Unauthorized product", "initialQuantity", 4)),
                403, PRODUCTS);
        expectProblem(request("POST", PRODUCTS + "/" + id + "/stock", CUSTOMER,
                Map.of("quantity", 10)), 403, PRODUCTS + "/" + id + "/stock");
        assertEquals(5, stock(id));
    }

    @Test
    void hidesOtherCustomersReservationsAndPreventsTheirCancellation() throws Exception {
        String id = createProduct(7);
        var reservation = reserve(id, 2, CUSTOMER);
        String path = RESERVATIONS + "/" + text(reservation, "id");
        expectProblem(request("GET", path, OTHER, null), 404, path);
        expectProblem(request("POST", path + "/cancel", OTHER, null), 404, path + "/cancel");
        var original = expectJson(request("GET", path, CUSTOMER, null), 200);
        assertEquals("ACTIVE", text(original, "status"));
        assertEquals(5, stock(id));

        reserve(id, 1, OTHER);
        var page = expectJson(request("GET", RESERVATIONS + "?page=0&size=100", OTHER, null), 200);
        assertTrue(page.path("items").size() > 0);
        for (JsonNode item : page.path("items")) {
            assertEquals("other", text(item, "customerId"));
            assertFalse(text(reservation, "id").equals(text(item, "id")));
        }
    }

    @Test
    void returnsNotFoundForMissingResources() throws Exception {
        String missing = UUID.randomUUID().toString();
        expectProblem(request("GET", PRODUCTS + "/" + missing, CUSTOMER, null),
                404, PRODUCTS + "/" + missing);
        expectProblem(request("POST", PRODUCTS + "/" + missing + "/stock", ADMIN,
                Map.of("quantity", 1)), 404, PRODUCTS + "/" + missing + "/stock");
        expectProblem(request("POST", RESERVATIONS, CUSTOMER,
                Map.of("productId", missing, "quantity", 1)), 404, RESERVATIONS);
        expectProblem(request("GET", RESERVATIONS + "/" + missing, CUSTOMER, null),
                404, RESERVATIONS + "/" + missing);
        expectProblem(request("POST", RESERVATIONS + "/" + missing + "/cancel", CUSTOMER, null),
                404, RESERVATIONS + "/" + missing + "/cancel");
    }

    @Test
    void paginationHasStableMetadataAndRejectsInvalidInputs() throws Exception {
        createProduct(1);
        createProduct(1);
        var first = expectJson(request("GET", PRODUCTS + "?page=0&size=1", CUSTOMER, null), 200);
        var second = expectJson(request("GET", PRODUCTS + "?page=1&size=1", CUSTOMER, null), 200);
        assertEquals(0, first.path("page").intValue());
        assertEquals(1, first.path("size").intValue());
        assertEquals(1, first.path("items").size());
        assertTrue(first.path("totalElements").longValue() >= 2);
        assertEquals(first.path("totalElements").longValue(), first.path("totalPages").longValue());
        assertFalse(text(first.path("items").get(0), "id")
                .equals(text(second.path("items").get(0), "id")));

        for (String endpoint : List.of(PRODUCTS, RESERVATIONS)) {
            for (String query : List.of("?page=-1", "?size=0", "?size=-1", "?size=1001", "?page=nope")) {
                expectProblem(request("GET", endpoint + query, CUSTOMER, null), 400, endpoint);
            }
        }
    }

    @Test
    void rejectsMalformedJsonAndInvalidIdentifiers() throws Exception {
        var malformed = requestWithBody("POST", RESERVATIONS, CUSTOMER, "{\"quantity\":");
        expectProblem(malformed, 400, RESERVATIONS);
        expectProblem(request("GET", PRODUCTS + "/not-a-uuid", CUSTOMER, null),
                400, PRODUCTS + "/not-a-uuid");
        expectProblem(request("POST", RESERVATIONS, CUSTOMER,
                Map.of("productId", "invalid", "quantity", 1)), 400, RESERVATIONS);
    }

    @Test
    void formSubmissionCannotCancelReservation() throws Exception {
        String productId = createProduct(3);
        String reservationId = text(reserve(productId, 1, CUSTOMER), "id");
        String path = RESERVATIONS + "/" + reservationId;
        var formResponse = requestWithBody("POST", path + "/cancel", CUSTOMER, "confirm=true",
                Map.of("Content-Type", "application/x-www-form-urlencoded"));

        expectProblem(formResponse, 415, path + "/cancel");
        assertEquals("ACTIVE", text(expectJson(request("GET", path, CUSTOMER, null), 200), "status"));
        assertEquals(2, stock(productId));

        assertEquals("CANCELLED", text(expectJson(request("POST", path + "/cancel", CUSTOMER, null), 200),
                "status"));
        assertEquals(3, stock(productId));
    }

    @Test
    void unsupportedMethodPreservesAllowHeader() throws Exception {
        var response = request("PUT", RESERVATIONS, CUSTOMER, null);
        expectProblem(response, 405, RESERVATIONS);
        var allowed = List.of(response.headers().firstValue("Allow").orElse("").split(",\\s*"));
        assertTrue(allowed.contains("GET"), () -> "Missing GET in Allow: " + allowed);
        assertTrue(allowed.contains("POST"), () -> "Missing POST in Allow: " + allowed);
    }

    @Test
    void requestIdsEchoSafeValuesAndReplaceUnsafeOrOverlongValues() throws Exception {
        String safeId = "integration-request_123";
        var accepted = requestWithBody("GET", "/actuator/health", null, null, Map.of("X-Request-ID", safeId));
        assertEquals(200, accepted.statusCode());
        assertEquals(safeId, accepted.headers().firstValue("X-Request-ID").orElseThrow());

        for (String invalid : List.of("log-forgery[admin]=true", "x".repeat(65))) {
            var response = requestWithBody("GET", "/actuator/health", null, null, Map.of("X-Request-ID", invalid));
            assertEquals(200, response.statusCode());
            String generated = response.headers().firstValue("X-Request-ID").orElseThrow();
            assertFalse(invalid.equals(generated));
            assertEquals(generated, UUID.fromString(generated).toString());
        }

        var unauthorized = requestWithBody("GET", PRODUCTS, null, null, Map.of("X-Request-ID", safeId));
        assertEquals(safeId, text(expectJson(unauthorized, 401), "requestId"));
        assertEquals(safeId, unauthorized.headers().firstValue("X-Request-ID").orElseThrow());
    }

    @Test
    void concurrentReservationsCannotOversellInventory() throws Exception {
        int initialStock = 5;
        String productId = createProduct(initialStock);
        List<HttpResponse<String>> responses = concurrent(12, () -> request("POST", RESERVATIONS,
                CUSTOMER, Map.of("productId", productId, "quantity", 1)));

        long successful = responses.stream().filter(response -> response.statusCode() == 201).count();
        assertEquals(initialStock, successful, () -> "Unexpected responses: " + summarize(responses));
        for (var response : responses) {
            if (response.statusCode() == 201) {
                assertEquals("ACTIVE", text(expectJson(response, 201), "status"));
            } else {
                expectProblem(response, 409, RESERVATIONS);
            }
        }
        assertEquals(0, stock(productId), "All successful reservations must be reflected in stock");
    }

    @Test
    void concurrentCancellationsRestoreInventoryExactlyOnce() throws Exception {
        String productId = createProduct(10);
        String reservationId = text(reserve(productId, 6, CUSTOMER), "id");
        assertEquals(4, stock(productId));
        String path = RESERVATIONS + "/" + reservationId + "/cancel";
        List<HttpResponse<String>> responses = concurrent(10, () -> request("POST", path, CUSTOMER, null));
        String cancelledAt = null;
        for (var response : responses) {
            var body = expectJson(response, 200);
            assertEquals("CANCELLED", text(body, "status"));
            if (cancelledAt == null) {
                cancelledAt = text(body, "cancelledAt");
            } else {
                assertEquals(cancelledAt, text(body, "cancelledAt"));
            }
        }
        assertEquals(10, stock(productId), "Concurrent cancellation must release each unit only once");
    }

    @Test
    void keyedRetryReturnsOriginalEvenWhenStockIsExhaustedOrReservationCancelled() throws Exception {
        String productId = createProduct(2);
        String key = UUID.randomUUID().toString();
        var created = keyedReservation(productId, 2, CUSTOMER, key);
        var original = expectJson(created, 201);
        assertEquals("false", created.headers().firstValue("Idempotency-Replayed").orElseThrow());
        var replay = keyedReservation(productId, 2, CUSTOMER, key);
        assertEquals(original, expectJson(replay, 200));
        assertEquals(created.headers().firstValue("Location"), replay.headers().firstValue("Location"));
        assertEquals("true", replay.headers().firstValue("Idempotency-Replayed").orElseThrow());
        assertEquals(0, stock(productId));
        String path = RESERVATIONS + "/" + text(original, "id");
        expectJson(request("POST", path + "/cancel", CUSTOMER, null), 200);
        var cancelledReplay = expectJson(keyedReservation(productId, 2, CUSTOMER, key), 200);
        assertEquals("CANCELLED", text(cancelledReplay, "status"));
        assertEquals(text(original, "id"), text(cancelledReplay, "id"));
        assertEquals(2, stock(productId), "Retry after cancellation must not create another reservation");
    }

    @Test
    void keyedRetryRejectsChangedProductOrQuantity() throws Exception {
        String first = createProduct(5);
        String second = createProduct(5);
        String key = UUID.randomUUID().toString();
        expectJson(keyedReservation(first, 2, CUSTOMER, key), 201);
        expectProblem(keyedReservation(first, 3, CUSTOMER, key), 409, RESERVATIONS);
        expectProblem(keyedReservation(second, 2, CUSTOMER, key), 409, RESERVATIONS);
        assertEquals(3, stock(first));
        assertEquals(5, stock(second));
    }

    @Test
    void failedRequestDoesNotConsumeIdempotencyKey() throws Exception {
        String id = createProduct(0);
        String key = UUID.randomUUID().toString();
        expectProblem(keyedReservation(id, 2, CUSTOMER, key), 409, RESERVATIONS);
        expectJson(request("POST", PRODUCTS + "/" + id + "/stock", ADMIN, Map.of("quantity", 2)), 200);
        var created = expectJson(keyedReservation(id, 2, CUSTOMER, key), 201);
        assertEquals(created, expectJson(keyedReservation(id, 2, CUSTOMER, key), 200));
        assertEquals(0, stock(id));
    }

    @Test
    void idempotencyKeysAreScopedToTheAuthenticatedCustomer() throws Exception {
        String id = createProduct(5);
        String key = UUID.randomUUID().toString();
        var first = expectJson(keyedReservation(id, 1, CUSTOMER, key), 201);
        var second = expectJson(keyedReservation(id, 2, OTHER, key), 201);
        assertFalse(text(first, "id").equals(text(second, "id")));
        assertEquals(first, expectJson(keyedReservation(id, 1, CUSTOMER, key), 200));
        assertEquals(second, expectJson(keyedReservation(id, 2, OTHER, key), 200));
        assertEquals(2, stock(id));
    }

    @Test
    void simultaneousKeyedRetriesPersistOnlyOneReservation() throws Exception {
        String id = createProduct(2);
        String key = UUID.randomUUID().toString();
        long before = reservationCount(CUSTOMER);
        var responses = concurrent(8, () -> keyedReservation(id, 2, CUSTOMER, key));
        assertEquals(1, responses.stream().filter(r -> r.statusCode() == 201).count(), summarize(responses));
        var original = expectJson(responses.stream().filter(r -> r.statusCode() == 201).findFirst().orElseThrow(), 201);
        for (var response : responses) {
            assertEquals(original, expectJson(response, response.statusCode() == 201 ? 201 : 200));
        }
        assertEquals(before + 1, reservationCount(CUSTOMER));
        assertEquals(0, stock(id));
    }

    @Test
    void simultaneousDifferentProductsCannotClaimTheSameCustomerKey() throws Exception {
        List<String> ids = List.of(createProduct(4), createProduct(4));
        String key = UUID.randomUUID().toString();
        var next = new java.util.concurrent.atomic.AtomicInteger();
        var responses = concurrent(2, () -> keyedReservation(ids.get(next.getAndIncrement()), 2, CUSTOMER, key));
        assertEquals(1, responses.stream().filter(r -> r.statusCode() == 201).count(), summarize(responses));
        assertEquals(1, responses.stream().filter(r -> r.statusCode() == 409).count(), summarize(responses));
        assertEquals(6, stock(ids.get(0)) + stock(ids.get(1)));
    }

    @Test
    void invalidIdempotencyKeysAreRejectedWithoutStockChanges() throws Exception {
        String id = createProduct(5);
        for (String key : List.of("", "contains spaces", "bad,key", "x".repeat(129), "_bad-start")) {
            expectProblem(keyedReservation(id, 1, CUSTOMER, key), 400, RESERVATIONS);
        }
        assertEquals(5, stock(id));
        expectJson(keyedReservation(id, 1, CUSTOMER, "A" + "x".repeat(127)), 201);
        assertEquals(4, stock(id));
    }

    @Test
    void blockedKeyReturnsRetryableConflictAndCanBeUsedAfterLockRelease() throws Exception {
        String id = createProduct(2);
        String key = UUID.randomUUID().toString();
        var locked = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            var holder = executor.submit(() -> new TransactionTemplate(transactions).executeWithoutResult(status -> {
                reservationKeys.acquire("customer", key);
                locked.countDown();
                try {
                    if (!release.await(15, TimeUnit.SECONDS)) {
                        throw new IllegalStateException("Timed out awaiting test lock release");
                    }
                } catch (InterruptedException exception) {
                    Thread.currentThread().interrupt();
                    throw new IllegalStateException(exception);
                }
            }));
            try {
                assertTrue(locked.await(10, TimeUnit.SECONDS));
                var response = keyedReservation(id, 2, CUSTOMER, key);
                expectProblem(response, 409, RESERVATIONS);
                assertTrue(JSON.readTree(response.body()).path("detail").stringValue().contains("retry"));
                assertEquals(2, stock(id));
            } finally {
                release.countDown();
            }
            holder.get(10, TimeUnit.SECONDS);
        }
        expectJson(keyedReservation(id, 2, CUSTOMER, key), 201);
        assertEquals(0, stock(id));
    }

    private HttpResponse<String> keyedReservation(String productId, int quantity, Credentials customer, String key)
            throws Exception {
        return requestWithBody("POST", RESERVATIONS, customer,
                JSON.writeValueAsString(Map.of("productId", productId, "quantity", quantity)),
                Map.of("Idempotency-Key", key));
    }

    @Test
    void healthAndApiDocumentationArePublicButMetricsRequireAdmin() throws Exception {
        var health = expectJson(request("GET", "/actuator/health", null, null), 200);
        assertEquals("UP", text(health, "status"));
        assertFalse(health.has("components"), "Public health checks should not expose database details");
        assertEquals(200, request("GET", "/openapi.yaml", null, null).statusCode());
        assertEquals(401, request("GET", "/actuator/prometheus", null, null).statusCode());
        assertEquals(403, request("GET", "/actuator/prometheus", CUSTOMER, null).statusCode());
        var metrics = request("GET", "/actuator/prometheus", ADMIN, null);
        assertEquals(200, metrics.statusCode(), metrics.body());
        assertTrue(metrics.body().contains("jvm_memory_used_bytes"));
        assertTrue(metrics.body().contains("inventory_reservations_total"),
                () -> metrics.body().lines().filter(line -> line.contains("inventory_")).collect(java.util.stream.Collectors.joining("\n")));
        assertFalse(metrics.body().contains("idempotency_key="));
    }

    @Test
    void businessMetricsCountCommittedWorkAndDistinguishReplays() throws Exception {
        var before = businessCounts();
        String id = createProduct(7);
        expectJson(request("POST", PRODUCTS + "/" + id + "/stock", ADMIN, Map.of("quantity", 2)), 200);
        String key = UUID.randomUUID().toString();
        var reservation = expectJson(keyedReservation(id, 3, CUSTOMER, key), 201);
        expectJson(keyedReservation(id, 3, CUSTOMER, key), 200);
        String cancel = RESERVATIONS + "/" + text(reservation, "id") + "/cancel";
        expectJson(request("POST", cancel, CUSTOMER, null), 200);
        expectJson(request("POST", cancel, CUSTOMER, null), 200);
        assertDelta(before, "products.created", 1);
        assertDelta(before, "stock.added", 9);
        assertDelta(before, "reservations.created", 1);
        assertDelta(before, "reservations.replayed", 1);
        assertDelta(before, "reservations.cancelled", 1);
        assertDelta(before, "reservations.cancellation.replayed", 1);
        assertDelta(before, "stock.reserved", 3);
        assertDelta(before, "stock.released", 3);
    }

    @Test
    void rejectedBusinessOperationsDoNotAdvanceCommittedCounters() throws Exception {
        String id = createProduct(1_000_000);
        String reservationId = text(reserve(id, 1, CUSTOMER), "id");
        expectJson(request("POST", PRODUCTS + "/" + id + "/stock", ADMIN, Map.of("quantity", 1)), 200);
        var before = businessCounts();
        expectProblem(request("POST", PRODUCTS + "/" + id + "/stock", ADMIN, Map.of("quantity", 1)),
                409, PRODUCTS + "/" + id + "/stock");
        String cancel = RESERVATIONS + "/" + reservationId + "/cancel";
        expectProblem(request("POST", cancel, CUSTOMER, null), 409, cancel);
        expectProblem(keyedReservation(id, 0, CUSTOMER, UUID.randomUUID().toString()), 400, RESERVATIONS);
        assertEquals(before, businessCounts());
    }

    @Test
    void enclosingTransactionRollbackDoesNotEmitSuccessfulBusinessMetrics() throws Exception {
        String id = createProduct(5);
        String key = UUID.randomUUID().toString();
        var before = businessCounts();
        new TransactionTemplate(transactions).executeWithoutResult(status -> {
            inventory.reserve(UUID.fromString(id), 3, "customer", key);
            inventory.addStock(UUID.fromString(id), 2);
            status.setRollbackOnly();
        });
        assertEquals(before, businessCounts());
        assertEquals(5, stock(id));
        expectJson(keyedReservation(id, 3, CUSTOMER, key), 201);
        assertDelta(before, "reservations.created", 1);
        assertDelta(before, "stock.reserved", 3);
        assertDelta(before, "stock.added", 0);
    }

    @Test
    void concurrentRetriesDoNotInflateReservationOrStockMovementCounters() throws Exception {
        String id = createProduct(5);
        String key = UUID.randomUUID().toString();
        var before = businessCounts();
        var responses = concurrent(6, () -> keyedReservation(id, 2, CUSTOMER, key));
        assertEquals(1, responses.stream().filter(r -> r.statusCode() == 201).count());
        assertEquals(5, responses.stream().filter(r -> r.statusCode() == 200).count());
        String reservationId = text(JSON.readTree(responses.getFirst().body()), "id");
        for (var response : concurrent(6, () -> request("POST", RESERVATIONS + "/" + reservationId + "/cancel", CUSTOMER, null))) {
            expectJson(response, 200);
        }
        assertDelta(before, "reservations.created", 1);
        assertDelta(before, "reservations.replayed", 5);
        assertDelta(before, "reservations.cancelled", 1);
        assertDelta(before, "reservations.cancellation.replayed", 5);
        assertDelta(before, "stock.reserved", 2);
        assertDelta(before, "stock.released", 2);
    }

    private Map<String, Double> businessCounts() {
        var counts = new java.util.HashMap<String, Double>();
        for (String name : List.of("products.created", "stock.added", "reservations.created", "reservations.replayed",
                "reservations.cancelled", "reservations.cancellation.replayed", "stock.reserved", "stock.released")) {
            counts.put(name, meters.get("inventory." + name).counter().count());
        }
        return counts;
    }

    private void assertDelta(Map<String, Double> before, String name, double expected) {
        assertEquals(expected, meters.get("inventory." + name).counter().count() - before.get(name), name);
    }

    private String createProduct(int quantity) throws Exception {
        return text(expectJson(request("POST", PRODUCTS, ADMIN,
                Map.of("sku", uniqueSku(), "name", "Integration test product", "initialQuantity", quantity)),
                201), "id");
    }

    private JsonNode reserve(String productId, int quantity, Credentials customer) throws Exception {
        return expectJson(request("POST", RESERVATIONS, customer,
                Map.of("productId", productId, "quantity", quantity)), 201);
    }

    private int stock(String productId) throws Exception {
        return expectJson(request("GET", PRODUCTS + "/" + productId, CUSTOMER, null), 200)
                .path("availableQuantity").intValue();
    }

    private long reservationCount(Credentials customer) throws Exception {
        return expectJson(request("GET", RESERVATIONS + "?page=0&size=1", customer, null), 200)
                .path("totalElements").longValue();
    }

    private HttpResponse<String> request(String method, String path, Credentials credentials, Object body)
            throws Exception {
        return requestWithBody(method, path, credentials, body == null ? null : JSON.writeValueAsString(body));
    }

    private HttpResponse<String> requestWithBody(String method, String path, Credentials credentials, String body)
            throws Exception {
        return requestWithBody(method, path, credentials, body, Map.of());
    }

    private HttpResponse<String> requestWithBody(String method, String path, Credentials credentials, String body,
            Map<String, String> headers) throws Exception {
        var builder = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .timeout(Duration.ofSeconds(30));
        if (credentials != null) {
            String token = Base64.getEncoder().encodeToString(
                    (credentials.username() + ":" + credentials.password()).getBytes(StandardCharsets.UTF_8));
            builder.header("Authorization", "Basic " + token);
        }
        if (body != null || "POST".equals(method)) {
            builder.header("Content-Type", "application/json");
        }
        headers.forEach(builder::setHeader);
        return HTTP.send(builder.method(method, body == null ? HttpRequest.BodyPublishers.noBody()
                        : HttpRequest.BodyPublishers.ofString(body)).build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private static JsonNode expectJson(HttpResponse<String> response, int expectedStatus) {
        assertEquals(expectedStatus, response.statusCode(), response.body());
        assertTrue(response.headers().firstValue("Content-Type").orElse("").contains("json"),
                () -> "Expected JSON response: " + response.body());
        return JSON.readTree(response.body());
    }

    private static void expectProblem(HttpResponse<String> response, int expectedStatus, String path) {
        JsonNode problem = expectJson(response, expectedStatus);
        assertTrue(response.headers().firstValue("Content-Type").orElse("")
                .startsWith("application/problem+json"), response.body());
        assertEquals(expectedStatus, problem.path("status").intValue());
        assertFalse(text(problem, "title").isBlank());
        assertFalse(text(problem, "detail").isBlank());
        assertEquals(path, text(problem, "instance"));
    }

    private static String text(JsonNode node, String field) {
        assertNotNull(node.get(field), () -> "Missing " + field + " in " + node);
        assertTrue(node.get(field).isString(), () -> "Expected text for " + field + " in " + node);
        return node.get(field).stringValue();
    }

    private static String uniqueSku() {
        return "IT-" + UUID.randomUUID().toString().toUpperCase(java.util.Locale.ROOT);
    }

    private static <T> List<T> concurrent(int count, Callable<T> operation) throws Exception {
        var ready = new CountDownLatch(count);
        var start = new CountDownLatch(1);
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            List<Future<T>> futures = new ArrayList<>();
            for (int i = 0; i < count; i++) {
                futures.add(executor.submit(() -> {
                    ready.countDown();
                    if (!start.await(10, TimeUnit.SECONDS)) {
                        throw new IllegalStateException("Timed out waiting to start concurrent HTTP requests");
                    }
                    return operation.call();
                }));
            }
            assertTrue(ready.await(10, TimeUnit.SECONDS), "All request workers must be ready");
            start.countDown();
            List<T> results = new ArrayList<>();
            for (var future : futures) {
                results.add(future.get(45, TimeUnit.SECONDS));
            }
            return results;
        } finally {
            start.countDown();
        }
    }

    private static String summarize(List<HttpResponse<String>> responses) {
        return responses.stream().map(response -> response.statusCode() + ": " + response.body()).toList().toString();
    }

    private record Credentials(String username, String password) {
    }
}
