"""Shared Flask extension instances.

Kept in its own module so models and blueprints can import the *same*
SQLAlchemy instance without creating circular imports with the application
factory. The instance is stateless until ``db.init_app(app)`` binds it to a
concrete application in ``app.py``.
"""
from __future__ import annotations

from flask_sqlalchemy import SQLAlchemy

# Stateless extension instance. Bound to the app in create_app().
db: SQLAlchemy = SQLAlchemy()
