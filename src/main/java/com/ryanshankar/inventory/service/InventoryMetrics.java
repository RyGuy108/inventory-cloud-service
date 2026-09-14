package com.ryanshankar.inventory.service;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/** Process-local operational counters. PostgreSQL remains the durable inventory ledger. */
@Component
public class InventoryMetrics {
    private final Counter productsCreated;
    private final Counter stockAdded;
    private final Counter reservationsCreated;
    private final Counter reservationsReplayed;
    private final Counter reservationsCancelled;
    private final Counter cancellationReplayed;
    private final Counter stockReserved;
    private final Counter stockReleased;

    public InventoryMetrics(MeterRegistry registry) {
        productsCreated = counter(registry, "inventory.products.created", "Committed product creations");
        stockAdded = counter(registry, "inventory.stock.added", "Units added by committed product creation and restocking");
        reservationsCreated = counter(registry, "inventory.reservations.created", "Committed new reservations");
        reservationsReplayed = counter(registry, "inventory.reservations.replayed", "Successful keyed reservation replays");
        reservationsCancelled = counter(registry, "inventory.reservations.cancelled", "Committed first-time cancellations");
        cancellationReplayed = counter(registry, "inventory.reservations.cancellation.replayed", "Successful repeated cancellations");
        stockReserved = counter(registry, "inventory.stock.reserved", "Units consumed by committed reservations");
        stockReleased = counter(registry, "inventory.stock.released", "Units restored by committed cancellations");
    }

    public void productCreated(int quantity) {
        afterCommit(() -> {
            productsCreated.increment();
            if (quantity > 0) stockAdded.increment(quantity);
        });
    }

    public void stockAdded(int quantity) {
        afterCommit(() -> stockAdded.increment(quantity));
    }

    public void reservationCreated(int quantity) {
        afterCommit(() -> {
            reservationsCreated.increment();
            stockReserved.increment(quantity);
        });
    }

    public void reservationReplayed() {
        afterCommit(reservationsReplayed::increment);
    }

    public void reservationCancelled(int quantity) {
        afterCommit(() -> {
            reservationsCancelled.increment();
            stockReleased.increment(quantity);
        });
    }

    public void cancellationReplayed() {
        afterCommit(cancellationReplayed::increment);
    }

    private static Counter counter(MeterRegistry registry, String name, String description) {
        // Fixed meter names and no per-customer/product/request labels keep cardinality bounded.
        return Counter.builder(name).description(description).register(registry);
    }

    private static void afterCommit(Runnable increment) {
        if (!TransactionSynchronizationManager.isActualTransactionActive()
                || !TransactionSynchronizationManager.isSynchronizationActive()) {
            throw new IllegalStateException("Inventory metrics require an active transaction");
        }
        TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
            @Override
            public void afterCompletion(int status) {
                // afterCompletion exceptions are logged, not propagated into a response for an
                // already-committed operation. Rollbacks never advance the business counters.
                if (status == STATUS_COMMITTED) increment.run();
            }
        });
    }
}
