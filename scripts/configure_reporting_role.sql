\set ON_ERROR_STOP on

SELECT format('CREATE ROLE dbeaver_analyst LOGIN PASSWORD %L', :'analyst_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='dbeaver_analyst') \gexec

ALTER ROLE dbeaver_analyst SET default_transaction_read_only = on;
ALTER ROLE dbeaver_analyst SET statement_timeout = '30s';
ALTER ROLE dbeaver_analyst SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE dbeaver_analyst CONNECTION LIMIT 3;

SELECT format('GRANT CONNECT ON DATABASE %I TO dbeaver_analyst', current_database()) \gexec
GRANT USAGE ON SCHEMA reporting TO dbeaver_analyst;
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO dbeaver_analyst;
ALTER DEFAULT PRIVILEGES IN SCHEMA reporting
  GRANT SELECT ON TABLES TO dbeaver_analyst;

REVOKE ALL ON TABLE google_oauth_credentials FROM dbeaver_analyst;
REVOKE ALL ON SCHEMA public FROM dbeaver_analyst;
GRANT USAGE ON SCHEMA public TO dbeaver_analyst;

SELECT current_database() AS database,
       rolname,
       rolconnlimit,
       rolcanlogin
FROM pg_roles WHERE rolname='dbeaver_analyst';
