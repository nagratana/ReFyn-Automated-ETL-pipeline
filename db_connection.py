from sqlalchemy import create_engine
import os


def get_engine():
    """
    Returns SQLAlchemy engine for the ETL database.
    Used inside Docker (Airflow DAGs). Defaults to Docker service name.
    """

    DB_USER = os.getenv("POSTGRES_USER", "refyn")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "refyn_pass")
    DB_NAME = os.getenv("POSTGRES_DB", "refyn_data")
    DB_HOST = os.getenv("POSTGRES_HOST", "postgres")  # Docker service name
    DB_PORT = os.getenv("POSTGRES_PORT", "5432")

    connection_string = (
        f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )

    engine = create_engine(
        connection_string,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )

    return engine


def get_streamlit_engine():
    """
    Returns SQLAlchemy engine for apps running on the HOST machine.
    Connects to Postgres via localhost:5433 (mapped port from docker-compose).
    """

    DB_USER = os.getenv("POSTGRES_USER", "refyn")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "refyn_pass")
    DB_NAME = os.getenv("POSTGRES_DB", "refyn_data")
    DB_HOST = os.getenv("POSTGRES_HOST", "localhost")  # Host machine
    DB_PORT = os.getenv("POSTGRES_PORT", "5433")  # Mapped port from docker-compose

    connection_string = (
        f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )

    engine = create_engine(
        connection_string,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )

    return engine