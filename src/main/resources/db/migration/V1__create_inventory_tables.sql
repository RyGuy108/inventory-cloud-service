CREATE TABLE products (
    id UUID PRIMARY KEY,
    sku VARCHAR(64) NOT NULL,
    name VARCHAR(160) NOT NULL,
    available_quantity INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT products_sku_unique UNIQUE (sku),
    CONSTRAINT products_sku_format CHECK (sku ~ '^[A-Z0-9][A-Z0-9._-]{0,63}$'),
    CONSTRAINT products_name_nonblank CHECK (length(btrim(name)) > 0),
    CONSTRAINT products_stock_range CHECK (available_quantity BETWEEN 0 AND 1000000)
);

CREATE INDEX products_created_at_id_idx ON products (created_at DESC, id);

CREATE TABLE reservations (
    id UUID PRIMARY KEY,
    product_id UUID NOT NULL REFERENCES products (id),
    quantity INTEGER NOT NULL,
    customer_id VARCHAR(200) NOT NULL,
    status VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    cancelled_at TIMESTAMPTZ,
    CONSTRAINT reservations_quantity_range CHECK (quantity BETWEEN 1 AND 1000000),
    CONSTRAINT reservations_customer_nonblank CHECK (length(btrim(customer_id)) > 0),
    CONSTRAINT reservations_status_valid CHECK (status IN ('ACTIVE', 'CANCELLED')),
    CONSTRAINT reservations_cancellation_consistent CHECK (
        (status = 'ACTIVE' AND cancelled_at IS NULL)
        OR (status = 'CANCELLED' AND cancelled_at IS NOT NULL AND cancelled_at >= created_at)
    )
);

CREATE INDEX reservations_customer_created_at_id_idx ON reservations (customer_id, created_at DESC, id);
CREATE INDEX reservations_product_id_idx ON reservations (product_id);
