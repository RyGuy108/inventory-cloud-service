package com.ryanshankar.inventory.repository;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** Serializes duplicate requests across service instances using their shared database. */
@Repository
public class ReservationKeyLock {
    private final JdbcTemplate jdbc;

    public ReservationKeyLock(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public void acquire(String customerId, String key) {
        // JpaTransactionManager shares its JDBC connection with JdbcTemplate. The lock is
        // released by commit/rollback, including failed reservations; no key is consumed on failure.
        long lockId = lockId(customerId, key);
        jdbc.query(connection -> {
            var statement = connection.prepareStatement("SELECT pg_advisory_xact_lock(?)");
            statement.setLong(1, lockId);
            statement.setQueryTimeout(3);
            return statement;
        }, result -> { });
    }

    private static long lockId(String customerId, String key) {
        try {
            // Length prefix prevents ambiguous identity/key boundaries. Hash collisions only
            // serialize unrelated requests; the exact customer and key are still queried.
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(
                    ("reservation:" + customerId.length() + ":" + customerId + ":" + key)
                            .getBytes(StandardCharsets.UTF_8));
            return ByteBuffer.wrap(digest).getLong();
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is required by the Java runtime", exception);
        }
    }
}
