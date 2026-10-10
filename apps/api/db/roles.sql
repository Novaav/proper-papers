-- Cluster-level roles. Run once, as a superuser, on a fresh PostgreSQL 18 cluster,
-- before database.sql:
--
--   psql -v ON_ERROR_STOP=1 -v owner_password=... -v runtime_password=... \
--        -v operator_password=... -f roles.sql
--
-- Passwords are supplied at run time and never stored in this file.

-- Owns the schema and runs migrations.
CREATE ROLE app_owner LOGIN PASSWORD :'owner_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Used by the API at runtime. Owns nothing; row-level security always applies.
CREATE ROLE app_runtime LOGIN PASSWORD :'runtime_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Used only by the operator CLI (tenant and membership administration).
-- Owns nothing; row-level security always applies; no access to tenant content.
CREATE ROLE app_operator LOGIN PASSWORD :'operator_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- The DI-1 processing worker (migration 0008 grants it only queue state and the
-- claimed run's clean content; it has no user identity).
CREATE ROLE app_worker LOGIN PASSWORD :'worker_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Fail if the role attributes are ever weakened.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname IN ('app_owner', 'app_runtime', 'app_operator', 'app_worker')
      AND (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication)
  ) THEN
    RAISE EXCEPTION 'application roles must not have elevated attributes';
  END IF;
END
$$;
