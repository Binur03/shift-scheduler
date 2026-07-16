"""Application factory and entrypoint.

Run locally:
    FLASK_ENV=development python app.py

In production the container runs Gunicorn against the `app` callable created
by `create_app()` (see Dockerfile).
"""
import logging
import os

from flask import Flask, redirect, render_template, url_for
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix

from config import get_config
from extensions import csrf, db, limiter, migrate


def _init_observability() -> None:
    """Configure log level and (if SENTRY_DSN is set) Sentry error reporting."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    dsn = os.environ.get("SENTRY_DSN")
    if dsn:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("FLASK_ENV", "production"),
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_RATE", 0)),
        )


def create_app(config_object: type | None = None) -> Flask:
    _init_observability()

    app = Flask(__name__)
    app.config.from_object(config_object or get_config())

    # Behind Cloud Run's proxy: trust X-Forwarded-Proto/Host so url_for()
    # generates https URLs and secure session cookies work.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Initialize extensions.
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)

    # Import models so they are registered on the metadata before create_all
    # and so Alembic autogenerate sees the full schema.
    with app.app_context():
        from models import (  # noqa: F401  (imported for side effects)
            Employee,
            Job,
            Shift,
            ShiftAssignment,
        )

    # Register blueprints.
    from routes.admin import admin_bp
    from routes.auth import auth_bp
    from routes.tasks import tasks_bp
    from routes.worker import worker_bp

    app.register_blueprint(admin_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(worker_bp)

    # CSRF applies to the session-authenticated admin/login forms. Worker
    # pages are authenticated by the unguessable URL token itself, and the
    # cron endpoint by the X-Tasks-Auth header — both are exempt so links in
    # week-old WhatsApp messages and Cloud Scheduler keep working.
    csrf.exempt(worker_bp)
    csrf.exempt(tasks_bp)

    # Register the `flask admin ...` CLI command group.
    from utils.cli import register_cli

    register_cli(app)

    @app.route("/")
    def index():
        return redirect(url_for("admin.list_shifts"))

    @app.route("/healthz")
    def healthz():
        try:
            db.session.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - any DB failure means unhealthy
            logging.getLogger(__name__).exception("Health check DB probe failed.")
            return {"status": "unhealthy", "database": "error"}, 503
        return {"status": "ok"}, 200

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(_e):
        return render_template("errors/500.html"), 500

    # CLI helper: `flask init-db` creates tables for first-time local setup.
    # Production uses migrations: `flask db upgrade`.
    @app.cli.command("init-db")
    def init_db():
        """Create all tables (local dev). In production run `flask db upgrade`."""
        db.create_all()
        print("Database tables created.")

    return app


# Module-level app for Gunicorn: `gunicorn app:app`.
app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
