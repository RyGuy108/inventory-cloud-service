package com.ryanshankar.inventory.database;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.UUID;

import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.springframework.boot.SpringApplication;
import org.flywaydb.core.Flyway;
import org.springframework.context.ConfigurableApplicationContext;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.postgresql.PostgreSQLContainer;
import org.testcontainers.utility.MountableFile;

import com.ryanshankar.inventory.InventoryCloudServiceApplication;
import com.ryanshankar.inventory.config.DatabaseMigrationConfiguration;

/** Exercises the actual migration entry point and PostgreSQL grants, without the Compose database. */
@Testcontainers
@Timeout(90)
class DatabasePermissionsIntegrationTest {
    private static final String APP_PASSWORD = "Runtime-db-test-'quoted-$password-29";

    @Container
    static final PostgreSQLContainer POSTGRES = new PostgreSQLContainer("postgres:17-alpine")
            .withDatabaseName("inventory").withUsername("inventory");

    @BeforeAll
    static void prepareDatabase() {
        try (var context = migrate()) {
            assertFalse(context.containsBean("entityManagerFactory"));
            assertFalse(context.containsBean("securityFilterChain"));
            assertFalse(context.containsBean("requestMappingHandlerMapping"));
            assertFalse(context.containsBean("dataSource"));
        }
    }

    @Test
    void runtimeCanCreateReadAndUpdateBothBusinessTables() throws Exception {
        String product = UUID.randomUUID().toString();
        String reservation = UUID.randomUUID().toString();
        try (var connection = runtime(); var statement = connection.createStatement()) {
            assertEquals(1, statement.executeUpdate(productInsert(product)));
            assertEquals(1, statement.executeUpdate("INSERT INTO reservations "
                    + "(id, product_id, quantity, customer_id, status, created_at) VALUES ('"
                    + reservation + "', '" + product + "', 2, 'test-customer', 'ACTIVE', now())"));
            assertEquals(1, statement.executeUpdate("UPDATE products SET available_quantity = 3 WHERE id = '"
                    + product + "'"));
            assertEquals(1, statement.executeUpdate("UPDATE reservations SET status = 'CANCELLED', "
                    + "cancelled_at = now() WHERE id = '" + reservation + "'"));
            try (var result = statement.executeQuery("SELECT p.available_quantity, r.status FROM products p "
                    + "JOIN reservations r ON r.product_id = p.id WHERE r.id = '" + reservation + "'")) {
                assertTrue(result.next());
                assertEquals(3, result.getInt(1));
                assertEquals("CANCELLED", result.getString(2));
            }
        }
    }

    @Test
    void runtimeCannotCreateAlterDropTruncateOrDelete() throws Exception {
        for (String sql : new String[] {
                "CREATE TABLE public.forbidden_table (id int)",
                "CREATE SCHEMA forbidden_schema",
                "CREATE TEMP TABLE forbidden_temp (id int)",
                "ALTER TABLE products ADD COLUMN forbidden int",
                "DROP TABLE reservations",
                "TRUNCATE TABLE reservations",
                "DELETE FROM reservations WHERE false",
                "DELETE FROM products WHERE false"
        }) {
            assertDenied(sql);
        }
    }

    @Test
    void runtimeCannotReadOrModifyFlywayHistoryOrAssumeMigrationRole() throws Exception {
        assertDenied("SELECT * FROM flyway_schema_history");
        assertDenied("UPDATE flyway_schema_history SET success = false WHERE false");
        assertDenied("SET ROLE inventory");
        assertDenied("CREATE ROLE forbidden_role");
    }

    @Test
    void newTablesDoNotAutomaticallyBecomeVisibleToRuntime() throws Exception {
        try (var connection = owner(); var statement = connection.createStatement()) {
            statement.execute("CREATE TABLE migration_private (id int)");
            try {
                assertDenied("SELECT * FROM migration_private");
                assertDenied("INSERT INTO migration_private VALUES (1)");
            } finally {
                statement.execute("DROP TABLE migration_private");
            }
        }
    }

    @Test
    void repeatingDatabasePreparationPreservesExistingRowsAndMigrationHistory() throws Exception {
        String product = UUID.randomUUID().toString();
        int migrationCount = successfulMigrations();
        try (var connection = runtime(); var statement = connection.createStatement()) {
            statement.executeUpdate(productInsert(product));
        }
        try (var ignored = migrate()) {
            // The completed migration job must be safe to rerun on a populated database.
        }
        try (var connection = runtime(); var statement = connection.createStatement();
                var result = statement.executeQuery("SELECT available_quantity FROM products WHERE id = '"
                        + product + "'")) {
            assertTrue(result.next());
            assertEquals(5, result.getInt(1));
        }
        assertEquals(migrationCount, successfulMigrations());
        assertDenied("SELECT * FROM flyway_schema_history");
    }

    @Test
    void manualProvisioningScriptIsRepeatableAndDoesNotPrintThePassword() throws Exception {
        POSTGRES.copyFileToContainer(MountableFile.forHostPath("db/provision-runtime.sh"),
                "/work/db/provision-runtime.sh");
        POSTGRES.copyFileToContainer(MountableFile.forHostPath("src/main/resources/db/provision/runtime-role.sql"),
                "/work/src/main/resources/db/provision/runtime-role.sql");
        var result = POSTGRES.execInContainer("env", "DATABASE_APP_PASSWORD=" + APP_PASSWORD,
                "PGPASSWORD=" + POSTGRES.getPassword(), "PGUSER=" + POSTGRES.getUsername(),
                "PGDATABASE=" + POSTGRES.getDatabaseName(), "sh", "/work/db/provision-runtime.sh");
        assertEquals(0, result.getExitCode(), result.getStdout() + result.getStderr());
        assertFalse(result.getStdout().contains(APP_PASSWORD));
        assertFalse(result.getStderr().contains(APP_PASSWORD));
        assertDenied("SELECT * FROM flyway_schema_history");
    }

    @Test
    void runtimeCredentialsCannotRunTheMigrationJob() {
        assertThrows(RuntimeException.class, () -> {
            try (var ignored = application().run(migrationArguments("inventory_app", APP_PASSWORD))) {
                // A runtime credential must not migrate even when the schema is already current.
            }
        });
    }

    @Test
    void nonSuperuserDatabaseOwnerCanMigrateAndProvisionRuntimeRole() throws Exception {
        try (var database = new PostgreSQLContainer("postgres:17-alpine")
                .withDatabaseName("inventory").withUsername("bootstrap")) {
            database.start();
            String ownerPassword = "Non-superuser-owner-test-password-42";
            try (var connection = DriverManager.getConnection(database.getJdbcUrl(),
                    database.getUsername(), database.getPassword()); var statement = connection.createStatement()) {
                statement.execute("CREATE ROLE migration_owner LOGIN CREATEROLE PASSWORD '" + ownerPassword + "'");
                statement.execute("ALTER DATABASE inventory OWNER TO migration_owner");
            }
            String[] arguments = {"--spring.profiles.active=migrate", "--DB_URL=" + database.getJdbcUrl(),
                    "--DB_USERNAME=migration_owner", "--DB_PASSWORD=" + ownerPassword,
                    "--DB_APP_PASSWORD=" + APP_PASSWORD};
            // Repeat to exercise ALTER ROLE on a previously provisioned login, too.
            for (int attempt = 0; attempt < 2; attempt++) {
                try (var ignored = application().run(arguments)) {
                }
            }
            try (var connection = DriverManager.getConnection(database.getJdbcUrl(), "inventory_app", APP_PASSWORD);
                    var statement = connection.createStatement()) {
                assertEquals(1, statement.executeUpdate(productInsert(UUID.randomUUID().toString())));
                SQLException denied = assertThrows(SQLException.class,
                        () -> statement.executeQuery("SELECT * FROM flyway_schema_history"));
                assertEquals("42501", denied.getSQLState());
            }
            try (var connection = DriverManager.getConnection(database.getJdbcUrl(), "migration_owner", ownerPassword);
                    var statement = connection.createStatement(); var result = statement.executeQuery(
                            "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user")) {
                assertTrue(result.next());
                assertFalse(result.getBoolean("rolsuper"));
                assertFalse(result.getBoolean("rolcreatedb"));
                assertTrue(result.getBoolean("rolcreaterole"));
            }
        }
    }

    @Test
    void upgradesVersionOneDataAndEnforcesKeyUniquenessWithRuntimeCredentials() throws Exception {
        try (var database = new PostgreSQLContainer("postgres:17-alpine")) {
            database.start();
            Flyway.configure().dataSource(database.getJdbcUrl(), database.getUsername(), database.getPassword())
                    .target("1").load().migrate();
            String product = UUID.randomUUID().toString();
            String legacyReservation = UUID.randomUUID().toString();
            try (var connection = DriverManager.getConnection(database.getJdbcUrl(),
                    database.getUsername(), database.getPassword()); var statement = connection.createStatement()) {
                statement.executeUpdate(productInsert(product));
                statement.executeUpdate("INSERT INTO reservations (id, product_id, quantity, customer_id, status, created_at) "
                        + "VALUES ('" + legacyReservation + "', '" + product + "', 1, 'legacy', 'ACTIVE', now())");
            }
            try (var ignored = application().run("--spring.profiles.active=migrate", "--DB_URL=" + database.getJdbcUrl(),
                    "--DB_USERNAME=" + database.getUsername(), "--DB_PASSWORD=" + database.getPassword(),
                    "--DB_APP_PASSWORD=" + APP_PASSWORD)) {
            }
            try (var connection = DriverManager.getConnection(database.getJdbcUrl(), "inventory_app", APP_PASSWORD);
                    var statement = connection.createStatement()) {
                try (var result = statement.executeQuery("SELECT idempotency_key FROM reservations WHERE id = '"
                        + legacyReservation + "'")) {
                    assertTrue(result.next());
                    assertEquals(null, result.getString(1));
                }
                String insert = "INSERT INTO reservations (id, product_id, quantity, customer_id, status, created_at, "
                        + "idempotency_key) VALUES (?, '" + product + "', 1, 'customer', 'ACTIVE', now(), 'upgrade-test')";
                try (var prepared = connection.prepareStatement(insert)) {
                    prepared.setObject(1, UUID.randomUUID());
                    assertEquals(1, prepared.executeUpdate());
                    prepared.setObject(1, UUID.randomUUID());
                    assertEquals("23505", assertThrows(SQLException.class, prepared::executeUpdate).getSQLState());
                }
            }
        }
    }

    private static ConfigurableApplicationContext migrate() {
        return application().run(migrationArguments(POSTGRES.getUsername(), POSTGRES.getPassword()));
    }

    private static SpringApplication application() {
        return new SpringApplication(InventoryCloudServiceApplication.class, DatabaseMigrationConfiguration.class);
    }

    private static String[] migrationArguments(String username, String password) {
        return new String[] {"--spring.profiles.active=migrate", "--DB_URL=" + POSTGRES.getJdbcUrl(),
                "--DB_USERNAME=" + username, "--DB_PASSWORD=" + password, "--DB_APP_PASSWORD=" + APP_PASSWORD};
    }

    private static Connection owner() throws SQLException {
        return DriverManager.getConnection(POSTGRES.getJdbcUrl(), POSTGRES.getUsername(), POSTGRES.getPassword());
    }

    private static Connection runtime() throws SQLException {
        return DriverManager.getConnection(POSTGRES.getJdbcUrl(), "inventory_app", APP_PASSWORD);
    }

    private static int successfulMigrations() throws SQLException {
        try (var connection = owner(); var statement = connection.createStatement();
                var result = statement.executeQuery("SELECT count(*) FROM flyway_schema_history WHERE success")) {
            assertTrue(result.next());
            return result.getInt(1);
        }
    }

    private static void assertDenied(String sql) throws Exception {
        try (var connection = runtime(); var statement = connection.createStatement()) {
            SQLException failure = assertThrows(SQLException.class, () -> statement.execute(sql), sql);
            assertEquals("42501", failure.getSQLState(), sql + ": " + failure.getMessage());
        }
    }

    private static String productInsert(String id) {
        return "INSERT INTO products (id, sku, name, available_quantity, created_at) VALUES ('" + id
                + "', 'DB-" + id.toUpperCase(java.util.Locale.ROOT) + "', 'Permission test', 5, now())";
    }
}
