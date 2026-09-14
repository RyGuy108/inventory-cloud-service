-- Nullable for existing rows and older application versions during a rolling release.
ALTER TABLE reservations ADD COLUMN idempotency_key VARCHAR(128);
ALTER TABLE reservations ADD CONSTRAINT reservations_idempotency_key_format
    CHECK (idempotency_key IS NULL OR idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$');
CREATE UNIQUE INDEX reservations_customer_idempotency_key_idx
    ON reservations (customer_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
