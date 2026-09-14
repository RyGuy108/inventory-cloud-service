package com.ryanshankar.inventory.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.FetchType;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;
import jakarta.persistence.Table;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.UUID;

@Entity
@Table(name = "reservations")
public class Reservation {

    @Id
    private UUID id;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "product_id", nullable = false, updatable = false)
    private Product product;

    @Column(nullable = false, updatable = false)
    private int quantity;

    @Column(name = "customer_id", nullable = false, length = 200, updatable = false)
    private String customerId;

    @Column(name = "idempotency_key", length = 128, updatable = false)
    private String idempotencyKey;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false, length = 16)
    private ReservationStatus status;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "cancelled_at")
    private Instant cancelledAt;

    protected Reservation() {
    }

    public Reservation(Product product, int quantity, String customerId) {
        this(product, quantity, customerId, null);
    }

    public Reservation(Product product, int quantity, String customerId, String idempotencyKey) {
        if (product == null) {
            throw new IllegalArgumentException("Product is required");
        }
        validateCustomerId(customerId);
        validateIdempotencyKey(idempotencyKey);
        product.reserveStock(quantity);
        this.id = UUID.randomUUID();
        this.product = product;
        this.quantity = quantity;
        this.customerId = customerId;
        this.idempotencyKey = idempotencyKey;
        this.status = ReservationStatus.ACTIVE;
        this.createdAt = Instant.now().truncatedTo(ChronoUnit.MICROS);
    }

    public static void validateCustomerId(String customerId) {
        if (customerId == null || customerId.isBlank() || customerId.length() > 200) {
            throw new IllegalArgumentException("Customer ID must contain 1 to 200 characters");
        }
    }

    public static void validateIdempotencyKey(String key) {
        if (key != null && !key.matches("[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")) {
            throw new IllegalArgumentException("Idempotency-Key must contain 1 to 128 letters, digits, dots, underscores, colons or hyphens, starting with a letter or digit");
        }
    }

    /** The service must hold locks on this reservation and its product before calling this. */
    public boolean cancel() {
        if (status == ReservationStatus.CANCELLED) {
            return false;
        }
        product.addStock(quantity);
        status = ReservationStatus.CANCELLED;
        cancelledAt = Instant.now().truncatedTo(ChronoUnit.MICROS);
        return true;
    }

    public UUID getId() {
        return id;
    }

    public Product getProduct() {
        return product;
    }

    public UUID getProductId() {
        return product.getId();
    }

    public int getQuantity() {
        return quantity;
    }

    public String getCustomerId() {
        return customerId;
    }

    public ReservationStatus getStatus() {
        return status;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public Instant getCancelledAt() {
        return cancelledAt;
    }
}
