"""Shared Flask extension instances.

Kept in its own module so models and blueprints can import the *same*
extension instances without creating circular imports with the application
factory. Each instance is stateless until ``init_app(app)`` binds it to a
concrete application in ``app.py``.
"""
from __future__ import annotations

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

# Stateless extension instances. Bound to the app in create_app().
db: SQLAlchemy = SQLAlchemy()
migrate: Migrate = Migrate()
csrf: CSRFProtect = CSRFProtect()

# Rate limiter keyed by client IP (ProxyFix restores the real IP behind
# Cloud Run's proxy). In-memory storage is per-instance, which is adequate
# protection at this scale; no default limit — applied per-route.
limiter: Limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
