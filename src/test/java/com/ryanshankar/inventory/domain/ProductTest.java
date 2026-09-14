package com.ryanshankar.inventory.domain;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class ProductTest {

    @Test
    void normalizesSkuAndNameAndAllowsZeroInitialStock() {
        Product product = new Product("  cable-usb_c.2  ", " USB cable ", 0);

        assertThat(product.getSku()).isEqualTo("CABLE-USB_C.2");
        assertThat(product.getName()).isEqualTo("USB cable");
        assertThat(product.getAvailableQuantity()).isZero();
        assertThat(product.getId()).isNotNull();
        assertThat(product.getCreatedAt()).isNotNull();
    }

    @ParameterizedTest
    @ValueSource(strings = {"", " ", "two words", "../product", "SKU/2", "-SKU"})
    void rejectsInvalidSkus(String sku) {
        assertThatThrownBy(() -> new Product(sku, "Product", 1))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @ParameterizedTest
    @ValueSource(ints = {-1, 1_000_001, Integer.MAX_VALUE})
    void rejectsInvalidInitialStock(int quantity) {
        assertThatThrownBy(() -> new Product("SKU", "Product", quantity))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void insufficientStockDoesNotChangeAvailableQuantity() {
        Product product = new Product("SKU", "Product", 2);

        assertThatThrownBy(() -> product.reserveStock(3))
                .isInstanceOfSatisfying(DomainException.class,
                        exception -> assertThat(exception.getKind()).isEqualTo(DomainException.Kind.CONFLICT));
        assertThat(product.getAvailableQuantity()).isEqualTo(2);
    }

    @Test
    void restockCannotExceedMaximumOrOverflow() {
        Product product = new Product("SKU", "Product", 999_999);

        product.addStock(1);
        assertThatThrownBy(() -> product.addStock(1)).isInstanceOf(DomainException.class);
        assertThatThrownBy(() -> product.addStock(Integer.MAX_VALUE)).isInstanceOf(IllegalArgumentException.class);
        assertThat(product.getAvailableQuantity()).isEqualTo(Product.MAX_QUANTITY);
    }

    @ParameterizedTest
    @ValueSource(ints = {-1, 0, 1_000_001, Integer.MAX_VALUE})
    void rejectsInvalidStockChangesWithoutMutation(int quantity) {
        Product product = new Product("SKU", "Product", 10);

        assertThatThrownBy(() -> product.addStock(quantity)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> product.reserveStock(quantity)).isInstanceOf(IllegalArgumentException.class);
        assertThat(product.getAvailableQuantity()).isEqualTo(10);
    }
}
