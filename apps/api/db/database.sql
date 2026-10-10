-- One application database, owned by app_owner. Run as a superuser after roles.sql:
--
--   psql -v ON_ERROR_STOP=1 -v db_name=app -v db_purpose=development -f database.sql
--
-- db_purpose is stored as the database comment. Tests run only against a
-- database whose comment is exactly 'disposable-test-database'.
-- Tables, grants on tables and row-level security belong to migrations.

CREATE DATABASE :"db_name" OWNER app_owner ENCODING 'UTF8' TEMPLATE template0;
COMMENT ON DATABASE :"db_name" IS :'db_purpose';
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO app_runtime, app_operator, app_worker;

\connect :"db_name"

REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA app AUTHORIZATION app_owner;
GRANT USAGE ON SCHEMA app TO app_runtime, app_operator, app_worker;
ALTER ROLE app_owner IN DATABASE :"db_name" SET search_path = app;
ALTER ROLE app_runtime IN DATABASE :"db_name" SET search_path = app;
ALTER ROLE app_operator IN DATABASE :"db_name" SET search_path = app;
ALTER ROLE app_worker IN DATABASE :"db_name" SET search_path = app;

-- Fail if the runtime or operator role could own or create anything in this database.
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['app_runtime', 'app_operator', 'app_worker'] LOOP
    IF EXISTS (SELECT 1 FROM pg_database WHERE datdba = r::regrole)
       OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner = r::regrole)
       OR EXISTS (SELECT 1 FROM pg_class WHERE relowner = r::regrole) THEN
      RAISE EXCEPTION '% must not own any database, schema or relation', r;
    END IF;

    IF has_database_privilege(r, current_database(), 'CREATE')
       OR has_database_privilege(r, current_database(), 'TEMPORARY')
       OR has_schema_privilege(r, 'app', 'CREATE')
       OR has_schema_privilege(r, 'public', 'USAGE') THEN
      RAISE EXCEPTION '% has more than CONNECT and USAGE on schema app', r;
    END IF;
  END LOOP;
END
$$;
