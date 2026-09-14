package com.ryanshankar.inventory.service;

import com.ryanshankar.inventory.domain.DomainException;
import com.ryanshankar.inventory.domain.Product;
import com.ryanshankar.inventory.domain.Reservation;
import com.ryanshankar.inventory.domain.ReservationStatus;
import com.ryanshankar.inventory.repository.ProductRepository;
import com.ryanshankar.inventory.repository.ReservationRepository;
import com.ryanshankar.inventory.repository.ReservationKeyLock;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.UUID;

@Service
@Transactional(readOnly = true)
public class InventoryService {

    private final ProductRepository products;
    private final ReservationRepository reservations;
    private final ReservationKeyLock reservationKeys;
    private final InventoryMetrics metrics;

    public InventoryService(ProductRepository products, ReservationRepository reservations,
                            ReservationKeyLock reservationKeys, InventoryMetrics metrics) {
        this.products = products;
        this.reservations = reservations;
        this.reservationKeys = reservationKeys;
        this.metrics = metrics;
    }

    @Transactional
    public Product createProduct(String sku, String name, int initialQuantity) {
        Product product = new Product(sku, name, initialQuantity);
        if (products.existsBySku(product.getSku())) {
            throw new DomainException(DomainException.Kind.CONFLICT, "A product with this SKU already exists");
        }
        try {
            // Flushing here catches a competing request that inserted the same normalized SKU.
            products.saveAndFlush(product);
            metrics.productCreated(initialQuantity);
            return product;
        } catch (DataIntegrityViolationException exception) {
            throw new DomainException(DomainException.Kind.CONFLICT, "A product with this SKU already exists");
        }
    }

    public Page<Product> listProducts(Pageable pageable) {
        return products.findAll(pageable);
    }

    public Product getProduct(UUID id) {
        return products.findById(id).orElseThrow(InventoryService::productNotFound);
    }

    @Transactional
    public Product addStock(UUID id, int quantity) {
        Product.validatePositiveQuantity(quantity);
        Product product = products.findByIdForUpdate(id).orElseThrow(InventoryService::productNotFound);
        product.addStock(quantity);
        metrics.stockAdded(quantity);
        return product;
    }

    @Transactional
    public ReservationResult reserve(UUID productId, int quantity, String customerId, String idempotencyKey) {
        Product.validatePositiveQuantity(quantity);
        Reservation.validateCustomerId(customerId);
        Reservation.validateIdempotencyKey(idempotencyKey);
        if (idempotencyKey != null) {
            reservationKeys.acquire(customerId, idempotencyKey);
            var existing = reservations.findByCustomerIdAndIdempotencyKey(customerId, idempotencyKey);
            if (existing.isPresent()) {
                Reservation reservation = existing.get();
                if (!reservation.getProductId().equals(productId) || reservation.getQuantity() != quantity) {
                    throw new DomainException(DomainException.Kind.CONFLICT,
                            "Idempotency-Key was already used with a different product or quantity");
                }
                metrics.reservationReplayed();
                return new ReservationResult(reservation, true);
            }
        }
        Product product = products.findByIdForUpdate(productId).orElseThrow(InventoryService::productNotFound);
        Reservation reservation = reservations.save(new Reservation(product, quantity, customerId, idempotencyKey));
        metrics.reservationCreated(quantity);
        return new ReservationResult(reservation, false);
    }

    public record ReservationResult(Reservation reservation, boolean replayed) { }

    public Page<Reservation> listReservations(String customerId, Pageable pageable) {
        Reservation.validateCustomerId(customerId);
        return reservations.findAllByCustomerId(customerId, pageable);
    }

    public Reservation getReservation(UUID id, String customerId) {
        Reservation.validateCustomerId(customerId);
        return reservations.findByIdAndCustomerId(id, customerId)
                .orElseThrow(InventoryService::reservationNotFound);
    }

    @Transactional
    public Reservation cancelReservation(UUID id, String customerId) {
        Reservation.validateCustomerId(customerId);
        // Lock the reservation before inspecting its status so concurrent retries cannot restore stock twice.
        Reservation reservation = reservations.findOwnedForUpdate(id, customerId)
                .orElseThrow(InventoryService::reservationNotFound);
        if (reservation.getStatus() == ReservationStatus.ACTIVE) {
            products.findByIdForUpdate(reservation.getProductId()).orElseThrow(InventoryService::productNotFound);
            reservation.cancel();
            metrics.reservationCancelled(reservation.getQuantity());
        } else {
            metrics.cancellationReplayed();
        }
        // Initialize for response mapping after this transaction ends, including idempotent retries.
        reservation.getProduct().getSku();
        return reservation;
    }

    private static DomainException productNotFound() {
        return new DomainException(DomainException.Kind.NOT_FOUND, "Product not found");
    }

    private static DomainException reservationNotFound() {
        return new DomainException(DomainException.Kind.NOT_FOUND, "Reservation not found");
    }
}
