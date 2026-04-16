-- Postgres entrypoint init script
-- The default database 'refyn_data' is created automatically by the POSTGRES_DB env var.
-- All ETL raw + cleaned tables live in refyn_data.

-- Create a separate database for Airflow metadata (keeps ETL data isolated)
CREATE DATABASE airflow_meta;
