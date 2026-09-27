-- Runs once, when the entrypoint creates an empty cluster, as the superuser, connected to the
-- database named by POSTGRES_DB.
--
-- Roles
--   postgres       superuser; local socket with peer authentication only, never used by services
--   perimeter      owns the database and its schema; the init job runs migrations as this role
--   perimeter_app  api and engine: reads and writes rows, cannot change the schema
--
-- The login passwords arrive as SCRAM-SHA-256 verifiers written by perimeter.tools.secrets, so the
-- database never holds the passwords the services present.

\set ON_ERROR_STOP on
\set owner_verifier `cat /run/secrets/perimeter.scram`
\set app_verifier `cat /run/secrets/perimeter_app.scram`

CREATE ROLE perimeter LOGIN PASSWORD :'owner_verifier';
CREATE ROLE perimeter_app LOGIN PASSWORD :'app_verifier';

-- The database owner also owns the public schema (pg_database_owner), so migrations can create
-- objects there without superuser rights. Nobody else may even connect, except the services.
ALTER DATABASE :"DBNAME" OWNER TO perimeter;
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME" TO perimeter_app;

-- PostGIS and statement statistics need a superuser to install; citext is here for completeness.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Whatever the owner creates from now on (every migration) is readable and writable by the
-- services, and nothing more: no DDL, no TRUNCATE, no ownership.
ALTER DEFAULT PRIVILEGES FOR ROLE perimeter IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO perimeter_app;
ALTER DEFAULT PRIVILEGES FOR ROLE perimeter IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO perimeter_app;
