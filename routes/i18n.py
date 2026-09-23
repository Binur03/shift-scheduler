"""The language toggle: one GET that switches EN <-> ES and comes back.

The choice is stored in a long-lived cookie rather than the session, because
workers are never logged in — their pages are authenticated by the token in
the URL, and a session cookie would be forgotten between texts.
"""
from __future__ import annotations

from flask import Blueprint, redirect, request, url_for

from utils.i18n import COOKIE_MAX_AGE, COOKIE_NAME, is_supported
from utils.urls import safe_relative_path

i18n_bp = Blueprint("i18n", __name__)


@i18n_bp.route("/language/<lang>")
def switch(lang: str):
    """Set the language cookie and return the visitor to the page they were on."""
    target = safe_relative_path(request.args.get("next")) or url_for("public.home")
    response = redirect(target)
    if is_supported(lang):
        response.set_cookie(
            COOKIE_NAME,
            lang,
            max_age=COOKIE_MAX_AGE,
            httponly=False,   # harmless display preference; no auth value
            samesite="Lax",
            secure=request.is_secure,
        )
    return response
