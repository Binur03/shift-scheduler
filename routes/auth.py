"""Admin authentication: single shared-password login.

The password is read from ADMIN_PASSWORD. All /admin routes are gated by an
``admin_bp.before_request`` hook (see routes/admin.py) that redirects to
/login. Worker token pages stay public — the unguessable token in the URL is
their credential.
"""
from __future__ import annotations

import hmac
import os

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from extensions import limiter

auth_bp = Blueprint("auth", __name__)


def _safe_next(target: str | None) -> str:
    """Only allow same-site relative redirect targets."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("admin.list_shifts")


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if session.get("is_admin"):
        return redirect(_safe_next(request.args.get("next")))

    configured = os.environ.get("ADMIN_PASSWORD")

    if request.method == "POST":
        if not configured:
            flash("ADMIN_PASSWORD is not configured on the server.", "error")
            return render_template("admin/login.html"), 503

        submitted = request.form.get("password", "")
        if hmac.compare_digest(submitted, configured):
            session.clear()
            session["is_admin"] = True
            session.permanent = True
            return redirect(_safe_next(request.form.get("next")))

        # Brute-force protection is handled by the rate limit above.
        flash("Incorrect password.", "error")

    return render_template("admin/login.html")


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("auth.login"))
