"""Application configuration.

Reads settings from environment variables so the same image runs locally
and on Cloud Run. Cloud SQL connections use a Unix socket when
INSTANCE_CONNECTION_NAME is provided (the default for Cloud Run with the
Cloud SQL connector), and fall back to TCP for local development.
"""
import os
from sqlalchemy.engine import URL


def _build_db_uri() -> str:
    """Construct the SQLAlchemy URI for MySQL (PyMySQL driver).

    Cloud Run + Cloud SQL: connection is made over a Unix domain socket at
    /cloudsql/<INSTANCE_CONNECTION_NAME>. Locally, a host/port TCP connection
    is used instead.
    """
    # Full URI override (e.g. sqlite:///local.db for quick local dev/tests).
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit

    user = os.environ.get("DB_USER", "app")
    password = os.environ.get("DB_PASS", "")
    name = os.environ.get("DB_NAME", "shift_scheduler")
    instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME")

    connection = URL.create(
        "mysql+pymysql", username=user, password=password, database=name,
        host=None if instance_connection_name else os.environ.get("DB_HOST", "127.0.0.1"),
        port=None if instance_connection_name else int(os.environ.get("DB_PORT", "3306")),
        query={"unix_socket": f"/cloudsql/{instance_connection_name}"} if instance_connection_name else {},
    )
    return connection.render_as_string(hide_password=False)



class Config:
    """Base configuration shared across environments."""

    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")

    SQLALCHEMY_DATABASE_URI = _build_db_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Connection pooling tuned for Cloud Run + Cloud SQL.
    # Cloud Run instances are short-lived and connection limits on Cloud SQL
    # are finite, so keep the pool small and recycle connections to avoid
    # stale sockets after Cloud SQL idle timeouts.
    SQLALCHEMY_ENGINE_OPTIONS = (
        {}
        if SQLALCHEMY_DATABASE_URI.startswith("sqlite")
        else {
            "pool_size": int(os.environ.get("DB_POOL_SIZE", 5)),
            "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", 2)),
            "pool_timeout": int(os.environ.get("DB_POOL_TIMEOUT", 30)),
            "pool_recycle": int(os.environ.get("DB_POOL_RECYCLE", 1800)),
            "pool_pre_ping": True,
        }
    )

    # Public base URL used when rendering acceptance links (SMS, etc.) and as
    # the exact URL Twilio signs for inbound-SMS webhook verification.
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080")

    # Cap request bodies (inbound vendor emails with attachments are the
    # largest legitimate payloads).
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 10 * 1024 * 1024))

    # Session cookie hardening (admin login).
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"  # also mitigates CSRF on admin POST forms
    PERMANENT_SESSION_LIFETIME = int(
        os.environ.get("SESSION_LIFETIME_SECONDS", 12 * 3600)
    )

    # CSRF tokens (Flask-WTF) on admin/login forms. No separate expiry —
    # tokens live as long as the session, so a dashboard tab left open for
    # hours doesn't start failing form submits.
    WTF_CSRF_TIME_LIMIT = None


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite://"  # in-memory
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True  # cookies only over HTTPS
    PREFERRED_URL_SCHEME = "https"


def get_config() -> type[Config]:
    env = os.environ.get("FLASK_ENV", "production").lower()
    if env in ("dev", "development"):
        return DevelopmentConfig
    return ProductionConfig
