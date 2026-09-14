# Retrying reservation requests safely

Send `Idempotency-Key` with `POST /api/v1/reservations`. Generate a new random UUID for
each intended reservation and keep the same key and body when retrying after a timeout.

```sh
curl --user customer -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 4d98c6ab-3a6c-4dea-9257-8ad61caa6c40' \
  -d '{"productId":"REPLACE_WITH_PRODUCT_ID","quantity":2}' \
  http://localhost:8080/api/v1/reservations
```

The example key is illustrative; generate your own for real requests. Keys are case-sensitive,
start with a letter or digit, and contain 1–128 letters, digits, dots, underscores, colons, or
hyphens. They are scoped to the authenticated customer, so two customers can use the same
key without seeing each other's data. Keys are request identifiers, not credentials; do not
put personal data or secrets in them.

| Request | Result |
| --- | --- |
| First successful request with a key | 201, `Idempotency-Replayed: false`, new reservation |
| Same customer, key, product, and quantity | 200, `Idempotency-Replayed: true`, original reservation in its current state |
| Same customer/key, changed product or quantity | 409; inventory unchanged |
| Retry after the original reservation was cancelled | 200 with that cancelled reservation; stock is not reserved again |
| Invalid key | 400; inventory unchanged |
| Failed reservation, followed by a corrected or later retry | The failure does not consume the key |
| No key supplied | Each successful call creates a new reservation |

Successful keys do not expire and are retained with the reservation. The response is the
reservation's current state, not a stored copy of its initial response. Use a new key if you
intend another reservation after cancellation. On transient lock contention, wait briefly
and retry with the same key and body; do not substitute a fresh key after an uncertain response.

## Transactions and schema compatibility

The V2 migration adds a nullable column and a unique index on customer/key. Existing records
remain unchanged. A transaction-scoped PostgreSQL advisory lock serializes simultaneous
requests for the same customer/key across application instances. The lock is taken before the
product row lock, and both stock deduction and keyed reservation insertion commit together.
Lock waiting is bounded; timeouts return a retryable 409. Hash collisions only serialize
unrelated requests: lookup and uniqueness still use the exact customer/key values.

The runtime continues to use SELECT/INSERT/UPDATE grants on the existing business tables.
No new table, owner credential, or process-local key cache is involved. Tests cover concurrent
identical requests, competing different products, key isolation, failed-request retries,
cancellation replay, lock timeouts, and upgrading existing V1 data.

Deploy V2 before the new application. Existing binaries tolerate the additive schema but do
not implement keyed retries. Enable client reliance on this feature only after every serving
task supports it. Once clients rely on keys, rollback targets must also support idempotency;
schema compatibility alone is insufficient. Never remove the unique index or clear successful
keys as part of a routine rollback.

Implementation references: [PostgreSQL transaction-level advisory locks](https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS)
and [Spring JPA/JDBC transaction participation](https://docs.spring.io/spring-framework/reference/data-access/orm/jpa.html).
