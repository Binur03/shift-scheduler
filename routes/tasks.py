"""Scheduled-task endpoints (called by Google Cloud Scheduler).

POST /tasks/check-staffing runs the understaffing sweep. Protected by a
shared secret: the request must carry header ``X-Tasks-Auth: <CRON_SECRET>``.

Cloud Scheduler setup (hourly):

    gcloud scheduler jobs create http staffing-check \
      --schedule "0 * * * *" \
      --uri https://YOUR-SERVICE-URL/tasks/check-staffing \
      --http-method POST \
      --headers X-Tasks-Auth=YOUR_CRON_SECRET
"""
from __future__ import annotations

import hmac
import logging
import os

from flask import Blueprint, abort, request

from utils.alerts import check_understaffed_shifts, send_shift_reminders

logger = logging.getLogger(__name__)

tasks_bp = Blueprint("tasks", __name__, url_prefix="/tasks")


def _require_cron_auth() -> None:
    secret = os.environ.get("CRON_SECRET")
    if not secret:
        logger.error("CRON_SECRET not set; refusing to run scheduled tasks.")
        abort(503)
    provided = request.headers.get("X-Tasks-Auth", "")
    if not hmac.compare_digest(provided, secret):
        abort(403)


@tasks_bp.route("/check-staffing", methods=["POST"])
def check_staffing():
    _require_cron_auth()
    result = check_understaffed_shifts()
    reminders_sent = send_shift_reminders()
    return {
        "window": result.checked_window,
        "understaffed": result.understaffed,
        "alerted": result.alerted,
        "reminders_sent": reminders_sent,
    }, 200
