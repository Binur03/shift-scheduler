"""Web PIN check-in/out for the mobile keypad at /punch/<punch_token>.

Transaction (one per request, retried on InnoDB deadlock):

1. Resolve the assignment by its secret ``punch_token`` (404 if unknown).
2. Lock the worker row, then the assignment row — the same order the SMS
   punch uses, so an SMS "IN" and a keypad tap can't deadlock or both win.
3. Enforce the per-link lockout, then verify the PIN in constant time.
   A wrong PIN increments ``pin_failed_attempts`` inside the lock, so
   parallel guesses can't slip past the limit.
4. Apply the requested action. The client always states ``in`` or ``out``
   (never "toggle"), so a double-tap can't turn IN into IN+OUT, and an
   existing timestamp is never overwritten — a repeat returns the original.
5. Commit (releases locks).

Timestamps are naive UTC; responses carry venue-local display strings.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from extensions import db
from models import (
    AssignmentStatus,
    PunchSource,
    ShiftAssignment,
    ShiftStatus,
    utcnow_naive,
)
from utils.pins import PinConfigError, is_valid_pin_format, verify_pin
from utils.punch import (
    check_in_window_utc,
    lock_employee,
    open_punch_for,
    record_check_in,
    run_with_deadlock_retry,
)
from utils.timeutil import format_local_clock, utc_naive_to_local

logger = logging.getLogger(__name__)

MAX_PIN_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)


class PunchState:
    """What the keypad page should offer for an assignment right now."""

    READY_IN = "ready_in"
    READY_OUT = "ready_out"
    DONE = "done"
    TOO_EARLY = "too_early"
    ENDED = "ended"          # shift over and never checked in
    CLOSED = "closed"        # assignment/shift cancelled or not confirmed
    LOCKED = "locked"


@dataclass(frozen=True)
class PunchResponse:
    http_status: int
    body: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def ensure_punch_token(assignment: ShiftAssignment) -> str:
    """Give an accepted assignment its secret punch link token (caller commits)."""
    if not assignment.punch_token:
        # 128 bits, matching the acceptance token: unguessable but short
        # enough to keep the reminder SMS inside its segment budget.
        assignment.punch_token = secrets.token_urlsafe(16)
    return assignment.punch_token


def punch_url(base_url: str, assignment: ShiftAssignment) -> str:
    return f"{base_url.rstrip('/')}/punch/{ensure_punch_token(assignment)}"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def punch_state(assignment: ShiftAssignment, now_utc: datetime) -> str:
    if assignment.pin_locked_until_utc and assignment.pin_locked_until_utc > now_utc:
        return PunchState.LOCKED
    if (
        assignment.status != AssignmentStatus.accepted
        or assignment.shift.status not in (ShiftStatus.OPEN, ShiftStatus.FILLED)
    ):
        return PunchState.CLOSED
    if assignment.check_out_at_utc is not None:
        return PunchState.DONE
    if assignment.check_in_at_utc is not None:
        return PunchState.READY_OUT
    opens_at, _start, end = check_in_window_utc(assignment)
    if now_utc < opens_at:
        return PunchState.TOO_EARLY
    if now_utc > end:
        return PunchState.ENDED
    return PunchState.READY_IN


def opens_at_label(opens_at_utc: datetime, now_utc: datetime, tz_name: str | None) -> str:
    """Clock time for the check-in window, with the weekday when it is not today.

    A bare "Opens 2:00 PM" on a Wednesday for a Friday shift reads as "come
    back in an hour". Naming the day prevents a wasted trip.
    """
    from utils.i18n import day_name, t

    clock = format_local_clock(opens_at_utc, tz_name)
    opens_local = utc_naive_to_local(opens_at_utc, tz_name)
    today_local = utc_naive_to_local(now_utc, tz_name)
    if opens_local is None or today_local is None:
        return clock
    if opens_local.date() == today_local.date():
        return clock
    return t("{day} at {time}", day=day_name(opens_local.date()), time=clock)


def shift_summary(assignment: ShiftAssignment, now_utc: datetime) -> dict:
    """Venue-local facts for rendering the page / JSON responses.

    Keys are prefixed so they never collide with a punch response's own
    ``status`` / ``time`` (the moment of the punch).
    """
    shift, job = assignment.shift, assignment.shift.job
    opens_at, _start, _end = check_in_window_utc(assignment)
    return {
        "state": punch_state(assignment, now_utc),
        "worker_first_name": assignment.employee.first_name,
        "job": job.title,
        "shift_date": f"{shift.date:%a %d %b}",
        # 12-hour like the punch times; just the start when no end was given.
        "shift_time": shift.time_label,
        "area": shift.area,
        "check_in": format_local_clock(assignment.check_in_at_utc, job.timezone) or None,
        "check_out": format_local_clock(assignment.check_out_at_utc, job.timezone) or None,
        "opens_at": opens_at_label(opens_at, now_utc, job.timezone),
        "hours": assignment.worked_hours,
    }


# --------------------------------------------------------------------------- #
# Punch
# --------------------------------------------------------------------------- #
def web_punch(
    *,
    token: str,
    pin: object,
    action: object,
    now_utc: datetime | None = None,
    client_ip: str | None = None,
) -> PunchResponse:
    """Validate a keypad submission and record the punch. Commits."""
    if action not in ("in", "out") or not is_valid_pin_format(pin):
        return PunchResponse(400, {"error": "bad_request"})
    if not token or len(token) > 64:
        return PunchResponse(404, {"error": "invalid_link"})

    assignment_id = (
        db.session.query(ShiftAssignment.id).filter(ShiftAssignment.punch_token == token).scalar()
    )
    if assignment_id is None:
        return PunchResponse(404, {"error": "invalid_link"})

    def attempt() -> PunchResponse:
        return _punch_once(assignment_id, pin, action, now_utc or utcnow_naive(), client_ip)

    return run_with_deadlock_retry(attempt, label=f"web assignment={assignment_id}")


def _punch_once(
    assignment_id: int, pin: str, action: str, now_utc: datetime, client_ip: str | None
) -> PunchResponse:
    try:
        employee_id = db.session.get(ShiftAssignment, assignment_id).employee_id
        # (1) worker row lock, (2) assignment row lock — fixed order.
        employee = lock_employee(employee_id)
        assignment = (
            db.session.query(ShiftAssignment)
            .filter(ShiftAssignment.id == assignment_id)
            .with_for_update()
            .populate_existing()
            .one()
        )

        # (3) Lockout, then PIN.
        if assignment.pin_locked_until_utc and assignment.pin_locked_until_utc > now_utc:
            retry_after = int((assignment.pin_locked_until_utc - now_utc).total_seconds()) + 1
            db.session.rollback()
            return PunchResponse(423, {
                "error": "locked", "state": PunchState.LOCKED, "retry_after_seconds": retry_after,
            })

        if not verify_pin(employee, pin):
            assignment.pin_failed_attempts += 1
            locked_now = assignment.pin_failed_attempts >= MAX_PIN_ATTEMPTS
            if locked_now:
                assignment.pin_locked_until_utc = now_utc + LOCKOUT
                assignment.pin_failed_attempts = 0
            db.session.commit()
            logger.warning(
                "Wrong PIN for assignment %s from %s%s.",
                assignment_id, client_ip or "?", " — link locked" if locked_now else "",
            )
            if locked_now:
                return PunchResponse(423, {
                    "error": "locked", "state": PunchState.LOCKED,
                    "retry_after_seconds": int(LOCKOUT.total_seconds()),
                })
            return PunchResponse(401, {
                "error": "wrong_pin",
                "attempts_left": MAX_PIN_ATTEMPTS - assignment.pin_failed_attempts,
            })

        assignment.pin_failed_attempts = 0
        assignment.pin_locked_until_utc = None

        # (4) Apply.
        response = _apply(employee.id, assignment, action, now_utc)
        db.session.commit()
    except PinConfigError:
        db.session.rollback()
        logger.error("PIN_PEPPER not configured; refusing web punches.")
        return PunchResponse(503, {"error": "unavailable"})
    except Exception:
        db.session.rollback()
        raise

    if response.http_status == 200 and response.body.get("status") in ("checked_in", "checked_out"):
        logger.info("Web punch %s for assignment %s.", response.body["status"], assignment_id)
    response.body.update(shift_summary(assignment, now_utc))
    return response


def _apply(employee_id: int, assignment: ShiftAssignment, action: str, now_utc: datetime) -> PunchResponse:
    tz = assignment.shift.job.timezone
    state = punch_state(assignment, now_utc)

    if state == PunchState.CLOSED:
        return PunchResponse(409, {"error": "not_available"})

    if action == "in":
        if assignment.check_in_at_utc is not None:
            return PunchResponse(200, {
                "status": "already_checked_in",
                "time": format_local_clock(assignment.check_in_at_utc, tz),
            })
        other = open_punch_for(employee_id)
        if other is not None and other.id != assignment.id:
            return PunchResponse(409, {"error": "checked_in_elsewhere"})
        if state == PunchState.TOO_EARLY:
            opens_at, _s, _e = check_in_window_utc(assignment)
            return PunchResponse(409, {"error": "too_early", "opens_at": opens_at_label(opens_at, now_utc, tz)})
        if state == PunchState.ENDED:
            return PunchResponse(409, {"error": "shift_ended"})
        record_check_in(assignment, now_utc, PunchSource.WEB)  # applies the 5-minute snap-to-start
        return PunchResponse(200, {
            "status": "checked_in",
            "time": format_local_clock(assignment.check_in_at_utc, tz),
        })

    # action == "out"
    if assignment.check_out_at_utc is not None:
        return PunchResponse(200, {
            "status": "already_checked_out",
            "time": format_local_clock(assignment.check_out_at_utc, tz),
        })
    if assignment.check_in_at_utc is None:
        return PunchResponse(409, {"error": "not_checked_in"})
    assignment.check_out_at_utc = max(now_utc, assignment.check_in_at_utc)
    assignment.check_out_source = PunchSource.WEB
    return PunchResponse(200, {
        "status": "checked_out",
        "time": format_local_clock(assignment.check_out_at_utc, tz),
    })
