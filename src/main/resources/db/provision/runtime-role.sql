-- Run as the database/schema owner, after Flyway, inside a transaction.
-- inventory.runtime_password is a transaction-local setting supplied by the caller.
DO $provision$
DECLARE
    runtime_password TEXT := current_setting('inventory.runtime_password', true);
    membership RECORD;
BEGIN
    IF runtime_password IS NULL OR length(runtime_password) < 16 THEN
        RAISE EXCEPTION 'Runtime database password must contain at least 16 characters';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'inventory_app') THEN
        CREATE ROLE inventory_app LOGIN;
    END IF;
    -- A login with object ownership cannot be restricted by REVOKE.
    IF EXISTS (SELECT 1 FROM pg_class WHERE relowner = 'inventory_app'::regrole)
       OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner = 'inventory_app'::regrole)
       OR EXISTS (SELECT 1 FROM pg_database WHERE datdba = 'inventory_app'::regrole) THEN
        RAISE EXCEPTION 'inventory_app must not own database objects';
    END IF;
    -- RDS database administrators are not PostgreSQL superusers. Even explicitly setting
    -- SUPERUSER/REPLICATION/BYPASSRLS/CREATEDB to false can require attributes they do
    -- not have. New roles default to safe flags; reject an elevated existing role.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'inventory_app'
               AND (rolsuper OR rolreplication OR rolbypassrls OR rolcreatedb OR rolcreaterole)) THEN
        RAISE EXCEPTION 'inventory_app must not have elevated PostgreSQL privileges';
    END IF;
    ALTER ROLE inventory_app WITH LOGIN NOINHERIT;
    FOR membership IN
        SELECT parent.rolname FROM pg_auth_members members
        JOIN pg_roles parent ON parent.oid = members.roleid
        WHERE members.member = 'inventory_app'::regrole
    LOOP
        EXECUTE format('REVOKE %I FROM inventory_app', membership.rolname);
    END LOOP;
    EXECUTE format('ALTER ROLE inventory_app PASSWORD %L', runtime_password);
    -- This is a dedicated application database; remove inherited CREATE/TEMP privileges.
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM inventory_app', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO inventory_app', current_database());
END
$provision$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM inventory_app;
GRANT USAGE ON SCHEMA public TO inventory_app;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM inventory_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM inventory_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.products, public.reservations TO inventory_app;
-- No default table grants: new tables and Flyway history remain private until explicitly reviewed.
