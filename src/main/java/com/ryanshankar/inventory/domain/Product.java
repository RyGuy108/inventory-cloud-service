package com.ryanshankar.inventory.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Locale;
import java.util.UUID;
import java.util.regex.Pattern;

@Entity
@Table(name = "products")
public class Product {

    public static final int MAX_QUANTITY = 1_000_000;
    private static final Pattern SKU_PATTERN = Pattern.compile("[A-Z0-9][A-Z0-9._-]{0,63}");

    @Id
    private UUID id;

    @Column(nullable = false, unique = true, length = 64, updatable = false)
    private String sku;

    @Column(nullable = false, length = 160)
    private String name;

    @Column(name = "available_quantity", nullable = false)
    private int availableQuantity;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    protected Product() {
    }

    public Product(String sku, String name, int initialQuantity) {
        this.sku = normalizeSku(sku);
        if (name == null || name.isBlank() || name.trim().length() > 160) {
            throw new IllegalArgumentException("Product name must contain 1 to 160 characters");
        }
        if (initialQuantity < 0 || initialQuantity > MAX_QUANTITY) {
            throw new IllegalArgumentException("Initial quantity must be between 0 and " + MAX_QUANTITY);
        }
        this.id = UUID.randomUUID();
        this.name = name.trim();
        this.availableQuantity = initialQuantity;
        this.createdAt = Instant.now().truncatedTo(ChronoUnit.MICROS);
    }

    public static String normalizeSku(String sku) {
        if (sku == null) {
            throw new IllegalArgumentException("SKU is required");
        }
        String normalized = sku.trim().toUpperCase(Locale.ROOT);
        if (!SKU_PATTERN.matcher(normalized).matches()) {
            throw new IllegalArgumentException(
                    "SKU must contain 1 to 64 letters, numbers, dots, underscores, or hyphens, starting with a letter or number");
        }
        return normalized;
    }

    public static void validatePositiveQuantity(int quantity) {
        if (quantity < 1 || quantity > MAX_QUANTITY) {
            throw new IllegalArgumentException("Quantity must be between 1 and " + MAX_QUANTITY);
        }
    }

    public void addStock(int quantity) {
        validatePositiveQuantity(quantity);
        if (quantity > MAX_QUANTITY - availableQuantity) {
            throw new DomainException(DomainException.Kind.CONFLICT,
                    "Available stock cannot exceed " + MAX_QUANTITY);
        }
        availableQuantity += quantity;
    }

    public void reserveStock(int quantity) {
        validatePositiveQuantity(quantity);
        if (quantity > availableQuantity) {
            throw new DomainException(DomainException.Kind.CONFLICT, "Insufficient available stock");
        }
        availableQuantity -= quantity;
    }

    public UUID getId() {
        return id;
    }

    public String getSku() {
        return sku;
    }

    public String getName() {
        return name;
    }

    public int getAvailableQuantity() {
        return availableQuantity;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }
}
