-- 1. Connect to your targeted database (Replace 'mydatabase' if needed)
\c tlmtry

-- 2. Create a read-only role
CREATE ROLE readonly;

-- 3. Grant connection and schema access
GRANT CONNECT ON DATABASE tlmtry TO readonly;
GRANT USAGE ON SCHEMA public TO readonly;

-- 4. Grant read permissions on existing tables and sequences
GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO readonly;

-- 5. Set default privileges so FUTURE tables are automatically readable
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO readonly;

-- 6. Create the final login user and assign the role
CREATE USER us WITH PASSWORD 'H2wf6w90dw4';
GRANT readonly TO us;
