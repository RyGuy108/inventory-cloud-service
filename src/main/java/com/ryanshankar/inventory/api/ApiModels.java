package com.ryanshankar.inventory.api;

import com.ryanshankar.inventory.domain.Product;
import com.ryanshankar.inventory.domain.Reservation;
import jakarta.validation.constraints.*;
import org.springframework.data.domain.Page;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

public final class ApiModels {
    private ApiModels() {}

    public record CreateProduct(
            @NotBlank @Size(max = 64) @Pattern(regexp = "[A-Za-z0-9][A-Za-z0-9._-]{0,63}") String sku,
            @NotBlank @Size(max = 160) String name,
            @NotNull @Min(0) @Max(1_000_000) Integer initialQuantity) {}

    public record Quantity(@NotNull @Min(1) @Max(1_000_000) Integer quantity) {}

    public record CreateReservation(
            @NotNull UUID productId,
            @NotNull @Min(1) @Max(1_000_000) Integer quantity) {}

    public record ProductResponse(UUID id, String sku, String name, int availableQuantity, Instant createdAt) {
        public static ProductResponse from(Product p) {
            return new ProductResponse(p.getId(), p.getSku(), p.getName(), p.getAvailableQuantity(), p.getCreatedAt());
        }
    }

    public record ReservationResponse(UUID id, UUID productId, int quantity, String customerId,
                                      String status, Instant createdAt, Instant cancelledAt) {
        public static ReservationResponse from(Reservation r) {
            return new ReservationResponse(r.getId(), r.getProduct().getId(), r.getQuantity(),
                    r.getCustomerId(), r.getStatus().name(), r.getCreatedAt(), r.getCancelledAt());
        }
    }

    public record PageResponse<T>(List<T> items, int page, int size, long totalElements, int totalPages) {
        public static <T> PageResponse<T> from(Page<T> p) {
            return new PageResponse<>(p.getContent(), p.getNumber(), p.getSize(), p.getTotalElements(), p.getTotalPages());
        }
    }
}
