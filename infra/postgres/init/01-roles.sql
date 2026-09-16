-- ADR 0001 / architecture §3.1: three roles, least privilege.
-- migrator: owns schema, runs Alembic.   app: runtime, RLS enforced.   platform: platform jobs only.
CREATE ROLE migrator LOGIN PASSWORD 'migrator';
CREATE ROLE app LOGIN PASSWORD 'app' NOBYPASSRLS;
CREATE ROLE platform LOGIN PASSWORD 'platform' BYPASSRLS;
CREATE DATABASE ecomm OWNER migrator;
\connect ecomm
GRANT CONNECT ON DATABASE ecomm TO app, platform;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app, platform;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app, platform;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO app, platform;
