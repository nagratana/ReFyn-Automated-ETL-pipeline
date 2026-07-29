from sqlalchemy import create_engine
import os

# ─────────────────────────────────────────────────────────────
# Singleton engine — created once per process, reused everywhere.
# Prevents connection pool exhaustion on Supabase free tier (15 conn limit).
# ─────────────────────────────────────────────────────────────
_engine = None


def _build_engine():
    """Build the SQLAlchemy engine from environment config."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        # Fix postgres:// → postgresql:// for SQLAlchemy
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        return create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=3,        # Keep small to stay within Supabase's 15-conn limit
            max_overflow=2,     # Allow 2 extra burst connections max
            pool_timeout=30,
            pool_recycle=600,   # Recycle connections every 10 min
        )

    # Local / Docker fallback
    DB_USER = os.getenv("POSTGRES_USER", "refyn")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "refyn_pass")
    DB_NAME = os.getenv("POSTGRES_DB", "refyn_data")
    DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
    DB_PORT = os.getenv("POSTGRES_PORT", "5433")

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


def get_engine():
    """Return the singleton engine, creating it on first call."""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_streamlit_engine():
    """Alias kept for backward compatibility."""
    return get_engine()