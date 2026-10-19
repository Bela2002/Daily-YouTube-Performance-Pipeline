-- Easiest: `python -m yt_pipeline create-reader` does the same without psql.
-- This file is the psql alternative.
--
-- A read-only login for Looker Studio, so the
-- dashboard can never modify data and cannot see the raw/core tables.
-- The password is passed on the command line, so it is never written into
-- a file that could be committed to git:
--
--     psql "$DATABASE_URL" -v reader_password='a-long-random-password' \
--          -f sql/003_looker_readonly_user.sql
--
-- Re-running it just resets the password.

SELECT format('CREATE ROLE looker_reader LOGIN PASSWORD %L', :'reader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'looker_reader') \gexec

SELECT format('ALTER ROLE looker_reader LOGIN PASSWORD %L', :'reader_password') \gexec

GRANT USAGE ON SCHEMA reporting TO looker_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO looker_reader;
-- views read core tables with the view owner's rights, so no grants on core/raw are needed.

-- keep access after `init-db` recreates the views
ALTER DEFAULT PRIVILEGES IN SCHEMA reporting GRANT SELECT ON TABLES TO looker_reader;
