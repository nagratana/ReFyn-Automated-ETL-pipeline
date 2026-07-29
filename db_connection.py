from sqlalchemy import create_engine
import os


def get_engine():
    """
    Returns a SQLAlchemy engine.

    Priority:
    1. DATABASE_URL env var  (Supabase / Render production)
    2. Individual POSTGRES_* env vars (Docker / local dev)
    3. Defaults to localhost:5433 (local docker-compose mapping)
    """
    # Production: Render/Supabase provides DATABASE_URL directly
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        # Supabase/Render use 'postgres://' but SQLAlchemy needs 'postgresql://'
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        return create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )

    # Local / Docker: use individual env vars
    DB_USER = os.getenv("POSTGRES_USER", "refyn")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "refyn_pass")
    DB_NAME = os.getenv("POSTGRES_DB", "refyn_data")
    DB_HOST = os.getenv("POSTGRES_HOST", "postgres")  # Docker service name
    DB_PORT = os.getenv("POSTGRES_PORT", "5432")

    connection_string = (
        f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )
    return create_engine(
        connection_string,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


def get_streamlit_engine():
    """
    Returns SQLAlchemy engine for apps running on the HOST machine.
    Connects to Postgres via localhost:5433 (mapped port from docker-compose).
    In production, falls through to DATABASE_URL via get_engine().
    """
    # If DATABASE_URL is set, always use it (production)
    if os.getenv("DATABASE_URL"):
        return get_engine()

    DB_USER = os.getenv("POSTGRES_USER", "refyn")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "refyn_pass")
    DB_NAME = os.getenv("POSTGRES_DB", "refyn_data")
    DB_HOST = os.getenv("POSTGRES_HOST", "localhost")  # Host machine
    DB_PORT = os.getenv("POSTGRES_PORT", "5433")  # Mapped port from docker-compose

    connection_string = (
        f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )
    return create_engine(
        connection_string,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )