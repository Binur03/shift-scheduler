"""Inbound webhooks: Twilio SMS time punches and vendor shift emails.

Both endpoints are machine-to-machine, so they are CSRF-exempt (see app.py)
and authenticate every request themselves:

POST /webhooks/twilio/sms
    X-Twilio-Signature (HMAC-SHA1 over the public URL + POST params, keyed by
    TWILIO_AUTH_TOKEN) is verified with twilio's RequestValidator before any
    processing, and AccountSid must match ours. Responds with TwiML.

POST /webhooks/email/<vendor inbound_token>
    SendGrid Inbound Parse. Requires HTTP Basic Auth (credentials embedded in
    the SendGrid destination URL) plus a valid active vendor token; the
    vendor's DKIM domain is enforced during ingestion.
"""
from __future__ import annotations

import hmac
import logging
import os

from flask import Blueprint, Response, abort, current_app, jsonify, request
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from extensions import limiter
from models import Vendor
from utils.email_ingest import ingest_vendor_email
from utils.punch import handle_inbound_sms

logger = logging.getLogger(__name__)

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


# --------------------------------------------------------------------------- #
# Twilio
# --------------------------------------------------------------------------- #
def _signed_url() -> str:
    """The exact URL Twilio signed.

    Built from PUBLIC_BASE_URL rather than request.url: behind Cloud Run's
    proxy the scheme/host Flask sees can differ from what Twilio called, and
    any difference makes every legitimate signature fail.
    """
    base = current_app.config["PUBLIC_BASE_URL"].rstrip("/")
    query = request.query_string.decode("utf-8")
    return f"{base}{request.path}" + (f"?{query}" if query else "")


def verify_twilio_request() -> None:
    """Abort unless the request is a genuine Twilio webhook for our account."""
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not auth_token:
        logger.error("TWILIO_AUTH_TOKEN not set; refusing inbound SMS webhooks.")
        abort(503)

    signature = request.headers.get("X-Twilio-Signature", "")
    params = request.form.to_dict(flat=True)
    if not signature or not RequestValidator(auth_token).validate(_signed_url(), params, signature):
        logger.warning("Rejected SMS webhook with invalid Twilio signature from %s.", request.remote_addr)
        abort(403)

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    if account_sid and not hmac.compare_digest(params.get("AccountSid", ""), account_sid):
        logger.warning("Rejected SMS webhook for a foreign AccountSid.")
        abort(403)


def _twiml(message: str | None) -> Response:
    reply = MessagingResponse()
    if message:
        reply.message(message)
    return Response(str(reply), mimetype="application/xml")


@webhooks_bp.route("/twilio/sms", methods=["POST"])
@limiter.limit("1200 per minute")  # shift start can bring a burst of INs
def twilio_sms():
    verify_twilio_request()

    message_sid = request.form.get("MessageSid", "").strip()
    from_number = request.form.get("From", "").strip()
    if not message_sid or not from_number:
        abort(400)

    outcome = handle_inbound_sms(
        message_sid=message_sid,
        from_number=from_number,
        body=request.form.get("Body", ""),
    )
    # Business outcomes (duplicate, no shift, malformed) are 200 with a reply;
    # only real server errors surface as 5xx, which Twilio retries safely.
    return _twiml(outcome.reply)


# --------------------------------------------------------------------------- #
# Vendor email (SendGrid Inbound Parse)
# --------------------------------------------------------------------------- #
def verify_email_basic_auth() -> None:
    expected_user = os.environ.get("INBOUND_EMAIL_USERNAME")
    expected_pass = os.environ.get("INBOUND_EMAIL_PASSWORD")
    if not (expected_user and expected_pass):
        logger.error("INBOUND_EMAIL_USERNAME/PASSWORD not set; refusing email webhooks.")
        abort(503)

    auth = request.authorization
    ok = (
        auth is not None
        and auth.type == "basic"
        and hmac.compare_digest(auth.username or "", expected_user)
        and hmac.compare_digest(auth.password or "", expected_pass)
    )
    if not ok:
        response = jsonify(error="unauthorized")
        response.status_code = 401
        response.headers["WWW-Authenticate"] = 'Basic realm="inbound-email"'
        abort(response)


@webhooks_bp.route("/email/<token>", methods=["POST"])
@limiter.limit("60 per minute")
def vendor_email(token: str):
    verify_email_basic_auth()

    vendor = Vendor.query.filter_by(inbound_token=token, is_active=True).first()
    if vendor is None:
        abort(404)

    email, created = ingest_vendor_email(vendor, request.form.to_dict(flat=True))
    # 200 even for parse/DKIM failures: they aren't transient, and a non-2xx
    # would make SendGrid retry the same message for days.
    return jsonify(
        id=email.id,
        status=email.parse_status,
        shifts_created=email.shifts_created,
        errors=len(email.errors),
        duplicate=not created,
    ), 200
