# Business metrics

Administrators can scrape `/actuator/prometheus`. Besides JVM, HTTP and database-pool
measurements, the application exposes these counters:

| Prometheus metric | Meaning |
| --- | --- |
| `inventory_products_total` | Committed product creations |
| `inventory_stock_added_total` | Units introduced by product creation and restocking |
| `inventory_reservations_total` | Committed new reservations |
| `inventory_reservations_replayed_total` | Successful requests reusing an existing reservation key |
| `inventory_reservations_cancelled_total` | First-time cancellations that committed |
| `inventory_reservations_cancellation_replayed_total` | Successful requests to cancel an already-cancelled reservation |
| `inventory_stock_reserved_total` | Units consumed by new reservations |
| `inventory_stock_released_total` | Units returned by first-time cancellations |

Metrics are recorded after transaction completion only when the transaction committed.
Failed constraints, insufficient stock, rejected cancellations, and enclosing-transaction
rollbacks do not advance successful business counters. Replays have separate request counters
and never add to stock movement. Real PostgreSQL integration tests verify these properties,
including simultaneous retries and cancellations.

Meter names are fixed. No product ID, customer ID, request ID, SKU or idempotency key is used
as a metric label. The common application label identifies the service; Prometheus adds its
own target labels when scraping. HTTP request status metrics remain the source for rejected
requests and server errors.

## Reading the measurements

Use rates or increases over a time interval, aggregating all application instances. For example:

```promql
sum(rate(inventory_reservations_total[5m]))
sum(rate(inventory_reservations_replayed_total[5m]))
sum(increase(inventory_stock_reserved_total[1h]))
```

Counters reset when a process restarts and can miss events if a process crashes after database
commit but before recording or scraping. They are operational observations, not a durable audit
ledger or authoritative inventory totals. Do not derive current stock by subtracting counters;
query the API/database for current inventory. Reset handling belongs in Prometheus `rate` and
`increase` expressions.

The [monitoring drill](monitoring.md) uses real protected scraping, a separate readiness probe,
and private firing/recovery notifications. CloudWatch alarms and the local Prometheus rules
serve different environments; successful local notification delivery does not prove SNS or
production alert routing.

Implementation references: [Spring transaction completion callbacks](https://docs.spring.io/spring-framework/docs/current/javadoc-api/org/springframework/transaction/support/TransactionSynchronization.html)
and [Micrometer counters](https://docs.micrometer.io/micrometer/reference/concepts/counters.html).
