package com.ryanshankar.inventory.domain;

import org.junit.jupiter.api.Test;

import java.time.Instant;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class ReservationTest {

    @Test
    void reservesStockAndRestoresItExactlyOnce() {
        Product product = new Product("SKU", "Product", 10);
        Reservation reservation = new Reservation(product, 4, "customer-one");

        assertThat(product.getAvailableQuantity()).isEqualTo(6);
        assertThat(reservation.getStatus()).isEqualTo(ReservationStatus.ACTIVE);
        assertThat(reservation.getCancelledAt()).isNull();
        assertThat(reservation.cancel()).isTrue();
        Instant firstCancellation = reservation.getCancelledAt();
        assertThat(reservation.cancel()).isFalse();

        assertThat(product.getAvailableQuantity()).isEqualTo(10);
        assertThat(reservation.getStatus()).isEqualTo(ReservationStatus.CANCELLED);
        assertThat(reservation.getCancelledAt()).isEqualTo(firstCancellation);
        assertThat(firstCancellation).isAfterOrEqualTo(reservation.getCreatedAt());
    }

    @Test
    void invalidCustomerDoesNotConsumeStock() {
        Product product = new Product("SKU", "Product", 10);

        assertThatThrownBy(() -> new Reservation(product, 4, " "))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new Reservation(product, 4, "x".repeat(201)))
                .isInstanceOf(IllegalArgumentException.class);
        assertThat(product.getAvailableQuantity()).isEqualTo(10);
    }

    @Test
    void cancellationAtStockLimitLeavesReservationActive() {
        Product product = new Product("SKU", "Product", Product.MAX_QUANTITY);
        Reservation reservation = new Reservation(product, 1, "customer-one");
        product.addStock(1);

        assertThatThrownBy(reservation::cancel).isInstanceOf(DomainException.class);

        assertThat(reservation.getStatus()).isEqualTo(ReservationStatus.ACTIVE);
        assertThat(reservation.getCancelledAt()).isNull();
        assertThat(product.getAvailableQuantity()).isEqualTo(Product.MAX_QUANTITY);
    }
}
