"""Understaffing detection and admin alerting.

`check_understaffed_shifts()` finds open shifts starting between
ALERT_WINDOW_MIN_HOURS and ALERT_WINDOW_MAX_HOURS from now (default 24-48h)
that still have fewer accepted workers than required, sends the admin one
combined WhatsApp alert, and stamps each shift so it is only alerted once.

Intended to be run on a schedule — either via the HTTP endpoint
POST /tasks/check-staffing (Cloud Scheduler) or the CLI:

    flask admin check-staffing
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from extensions import db
from models import AssignmentStatus, Shift, ShiftAssignment, ShiftStatus
from utils.sms import WhatsAppService

logger = logging.getLogger(__name__)


def business_now() -> datetime:
    """Naive "now" in the business's local timezone.

    Shift dates/times are stored naive in local business time. Cloud Run
    clocks run UTC, so without this the 24-48h window would be shifted by
    the UTC offset. Set APP_TIMEZONE (e.g. "America/Denver").
    """
    tz_name = os.environ.get("APP_TIMEZONE")
    if tz_name:
        return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
    return datetime.now()


@dataclass(frozen=True)
class StaffingCheckResult:
    """Summary of one understaffing sweep."""

    checked_window: str
    understaffed: int
    alerted: int


def _find_understaffed(now: datetime, min_hours: int, max_hours: int) -> list[Shift]:
    """Return open, not-yet-alerted shifts starting inside the alert window."""
    window_start = now + timedelta(hours=min_hours)
    window_end = now + timedelta(hours=max_hours)

    # Bound by date in SQL (indexed), then filter precisely by datetime and
    # headcount in Python — shift volumes at this scale are tiny.
    candidates = (
        Shift.query.filter(
            Shift.status == ShiftStatus.OPEN,
            Shift.understaffed_alert_sent_at.is_(None),
            Shift.date >= window_start.date(),
            Shift.date <= window_end.date(),
        ).all()
    )

    understaffed = []
    for shift in candidates:
        starts_at = datetime.combine(shift.date, shift.start_time)
        if window_start <= starts_at <= window_end and not shift.is_full:
            understaffed.append(shift)
    return understaffed


def _format_alert(shifts: list[Shift]) -> str:
    lines = [
        "⚠️ STAFFING ALERT — the following shift(s) start in 24-48h "
        "and don't have enough workers:",
        "",
    ]
    for s in shifts:
        shortfall = s.required_headcount - s.accepted_count
        lines.append(
            f"• {s.job.title} @ {s.job.location_address}\n"
            f"  {s.date:%a %d %b} {s.start_time:%H:%M}-{s.end_time:%H:%M} — "
            f"{s.accepted_count}/{s.required_headcount} confirmed "
            f"(need {shortfall} more)"
        )
    lines.append("")
    lines.append("Open the dashboard to adjust the schedule or re-dispatch.")
    return "\n".join(lines)


def check_understaffed_shifts(
    now: datetime | None = None,
    service: WhatsAppService | None = None,
) -> StaffingCheckResult:
    """Run one sweep; alert the admin about understaffed shifts 24-48h out.

    Each shift is stamped with ``understaffed_alert_sent_at`` after being
    included in an alert so repeated sweeps (e.g. hourly cron) don't spam
    the admin about the same shift.
    """
    now = now or business_now()
    min_hours = int(os.environ.get("ALERT_WINDOW_MIN_HOURS", 24))
    max_hours = int(os.environ.get("ALERT_WINDOW_MAX_HOURS", 48))
    window = f"{min_hours}-{max_hours}h"

    shifts = _find_understaffed(now, min_hours, max_hours)
    if not shifts:
        logger.info("Staffing sweep (%s): all shifts adequately staffed.", window)
        return StaffingCheckResult(checked_window=window, understaffed=0, alerted=0)

    service = service or WhatsAppService()
    sent = service.send_admin_alert(_format_alert(shifts))

    # Stamp regardless of delivery outcome only when actually sent; if the
    # send failed (e.g. unconfigured), leave shifts unstamped so the next
    # sweep retries.
    alerted = 0
    if sent:
        for shift in shifts:
            shift.understaffed_alert_sent_at = now
            alerted += 1
        db.session.commit()

    logger.info(
        "Staffing sweep (%s): %d understaffed, alert %s.",
        window,
        len(shifts),
        "sent" if sent else "NOT sent (check Twilio/admin config)",
    )
    return StaffingCheckResult(
        checked_window=window, understaffed=len(shifts), alerted=alerted
    )


def send_shift_reminders(
    now: datetime | None = None,
    service: WhatsAppService | None = None,
) -> int:
    """WhatsApp a reminder to every accepted worker whose shift starts within
    REMINDER_HOURS (default 24). Each assignment is reminded once.

    Runs from the same scheduled sweep as the understaffing check, so no
    extra Cloud Scheduler job is needed.
    """
    now = now or business_now()
    horizon = now + timedelta(hours=int(os.environ.get("REMINDER_HOURS", 24)))

    candidates = (
        db.session.query(ShiftAssignment)
        .join(Shift, Shift.id == ShiftAssignment.shift_id)
        .filter(
            ShiftAssignment.status == AssignmentStatus.accepted,
            ShiftAssignment.reminder_sent_at.is_(None),
            Shift.date >= now.date(),
            Shift.date <= horizon.date(),
        )
        .all()
    )

    service = service or WhatsAppService()
    sent = 0
    for assignment in candidates:
        shift = assignment.shift
        starts_at = datetime.combine(shift.date, shift.start_time)
        if not (now <= starts_at <= horizon):
            continue
        body = (
            f"Reminder: you're confirmed for {shift.job.title}\n"
            f"Location: {shift.job.location_address}\n"
            f"{shift.date:%a %d %b} "
            f"{shift.start_time:%H:%M}-{shift.end_time:%H:%M}\n\n"
            "If you can no longer make it, contact your coordinator ASAP."
        )
        if service.send_message(assignment.employee.phone_number, body):
            assignment.reminder_sent_at = now
            sent += 1

    db.session.commit()
    logger.info("Reminder sweep: %d reminder(s) sent.", sent)
    return sent
