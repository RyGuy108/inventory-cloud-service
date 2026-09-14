package com.ryanshankar.inventory.api;

import com.ryanshankar.inventory.service.InventoryService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Sort;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import java.net.URI;
import java.util.UUID;
import static com.ryanshankar.inventory.api.ApiModels.*;

@RestController
@RequestMapping("/api/v1")
public class InventoryController {
    private final InventoryService inventory;

    public InventoryController(InventoryService inventory) { this.inventory = inventory; }

    @PostMapping("/products")
    public ResponseEntity<ProductResponse> createProduct(@Valid @RequestBody CreateProduct request) {
        var product = inventory.createProduct(request.sku(), request.name(), request.initialQuantity());
        return ResponseEntity.created(URI.create("/api/v1/products/" + product.getId())).body(ProductResponse.from(product));
    }

    @GetMapping("/products")
    public PageResponse<ProductResponse> listProducts(
            @RequestParam(defaultValue = "0") @Min(0) int page,
            @RequestParam(defaultValue = "20") @Min(1) @Max(100) int size) {
        return PageResponse.from(inventory.listProducts(pagination(page, size)).map(ProductResponse::from));
    }

    @GetMapping("/products/{id}")
    public ProductResponse getProduct(@PathVariable UUID id) { return ProductResponse.from(inventory.getProduct(id)); }

    @PostMapping("/products/{id}/stock")
    public ProductResponse addStock(@PathVariable UUID id, @Valid @RequestBody Quantity request) {
        return ProductResponse.from(inventory.addStock(id, request.quantity()));
    }

    @PostMapping("/reservations")
    public ResponseEntity<ReservationResponse> reserve(@Valid @RequestBody CreateReservation request,
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey, Authentication auth) {
        var result = inventory.reserve(request.productId(), request.quantity(), auth.getName(), idempotencyKey);
        var reservation = result.reservation();
        return ResponseEntity.status(result.replayed() ? 200 : 201)
                .location(URI.create("/api/v1/reservations/" + reservation.getId()))
                .header("Idempotency-Replayed", Boolean.toString(result.replayed()))
                .body(ReservationResponse.from(reservation));
    }

    @GetMapping("/reservations")
    public PageResponse<ReservationResponse> listReservations(
            @RequestParam(defaultValue = "0") @Min(0) int page,
            @RequestParam(defaultValue = "20") @Min(1) @Max(100) int size, Authentication auth) {
        return PageResponse.from(inventory.listReservations(auth.getName(), pagination(page, size)).map(ReservationResponse::from));
    }

    @GetMapping("/reservations/{id}")
    public ReservationResponse getReservation(@PathVariable UUID id, Authentication auth) {
        return ReservationResponse.from(inventory.getReservation(id, auth.getName()));
    }

    @PostMapping(value = "/reservations/{id}/cancel", consumes = "application/json")
    public ReservationResponse cancel(@PathVariable UUID id, Authentication auth) {
        return ReservationResponse.from(inventory.cancelReservation(id, auth.getName()));
    }

    private PageRequest pagination(int page, int size) {
        // Explicit guard also protects callers if MVC method validation is changed.
        if (page < 0 || size < 1 || size > 100 || (long) page * size > Integer.MAX_VALUE) {
            throw new IllegalArgumentException("page must be nonnegative and size must be between 1 and 100; offset must fit a 32-bit integer");
        }
        return PageRequest.of(page, size, Sort.by(Sort.Direction.DESC, "createdAt").and(Sort.by("id")));
    }
}
