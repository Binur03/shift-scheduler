"""Public business pages: home, SMS program terms, privacy policy.

These exist so carriers and Twilio's toll-free verification reviewers can see
who is sending texts, why, how workers opt in and out, and how their mobile
numbers are handled. They show only what the ``BUSINESS_*`` settings provide;
if ``BUSINESS_NAME`` is unset the home page keeps redirecting to the admin
console and the policy pages 404, so placeholder content is never published.

Settings (environment):
    BUSINESS_NAME            required to enable the pages
    BUSINESS_LOCATION        e.g. "Littleton, Colorado" (shown publicly)
    BUSINESS_ADDRESS         optional full street address (shown only if set)
    BUSINESS_CONTACT_EMAIL   public contact email
    BUSINESS_CONTACT_PHONE   optional public contact phone
    POLICY_EFFECTIVE_DATE    e.g. "September 14, 2026"
"""
from __future__ import annotations

import os
import re

from flask import Blueprint, abort, redirect, render_template, url_for

public_bp = Blueprint("public", __name__)


def _format_us_number(e164: str | None) -> str | None:
    match = re.fullmatch(r"\+1(\d{3})(\d{3})(\d{4})", (e164 or "").strip())
    return f"({match[1]}) {match[2]}-{match[3]}" if match else (e164 or None)


def business_profile() -> dict | None:
    """Public business facts, or None when the pages aren't configured."""
    name = os.environ.get("BUSINESS_NAME", "").strip()
    if not name:
        return None
    return {
        "name": name,
        "location": os.environ.get("BUSINESS_LOCATION", "").strip(),
        "address": os.environ.get("BUSINESS_ADDRESS", "").strip(),
        "email": os.environ.get("BUSINESS_CONTACT_EMAIL", "").strip(),
        "phone": _format_us_number(os.environ.get("BUSINESS_CONTACT_PHONE")),
        "sms_number": _format_us_number(os.environ.get("TWILIO_SMS_NUMBER")),
        "program": f"{name} Shift Alerts",
        "effective_date": os.environ.get("POLICY_EFFECTIVE_DATE", "").strip(),
    }


def _render(template: str):
    business = business_profile()
    if business is None:
        abort(404)
    return render_template(template, b=business)


@public_bp.route("/")
def home():
    if business_profile() is None:
        return redirect(url_for("admin.list_shifts"))
    return _render("public/home.html")


@public_bp.route("/sms-terms")
def sms_terms():
    return _render("public/sms_terms.html")


@public_bp.route("/privacy")
def privacy():
    return _render("public/privacy.html")
