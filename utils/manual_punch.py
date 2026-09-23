"""Manager override: stamp a check-in or check-out for a worker (dead phone, etc.).

Rules
-----
* Only for a *confirmed* worker on an open/filled shift.
* Fills an empty timestamp only — never overwrites a punch the worker (or a
  manager) already recorded. Corrections are a separate, deliberate feature.
* The manager enters the time in the venue's local time; it's stored in UTC.
* Check-ins go through the same 5-minute snap-to-start as worker punches, so
  a timesheet never depends on *who* recorded the arrival.
* Not in the future (small clock-skew allowance), and within a day of the
  scheduled start, which catches wrong-date typos.

Concurrency: identical lock order to SMS and keypad punches — the worker row,
then the assignment row, both SELECT ... FOR UPDATE — so a manual punch racing
a worker's own punch serializes cleanly, can't deadlock, and one of them sees
the other's timestamp and backs off.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from extensions import db
from models import AssignmentStatus, PunchSource, ShiftAssignment, ShiftStatus, utcnow_naive
from utils.punch import check_in_window_utc, lock_employee, open_punch_for, record_check_in, run_with_deadlock_retry
from utils.timeutil import format_local_clock, local_to_utc_naive

logger = logging.getLogger(__name__)

FUTURE_ALLOWANCE = timedelta(minutes=5)
MAX_DISTANCE_FROM_START = timedelta(hours=24)
MAX_NOTE_CHARS = 120


@dataclass(frozen=True)
class ManualPunchResult:
    ok: bool
    message: str


def manual_punch(
    *,
    shift_id: int,
    employee_id: int,
    action: str,
    local_time: datetime,
    note: str = "",
    now_utc: datetime | None = None,
) -> ManualPunchResult:
    """Record a manager's check-in/out. ``local_time`` is naive venue-local. Commits."""
    if action not in ("in", "out"):
        return ManualPunchResult(False, "Choose check-in or check-out.")
    note = " ".join((note or "").split())[:MAX_NOTE_CHARS]

    def attempt() -> ManualPunchResult:
        return _punch_once(shift_id, employee_id, action, local_time, note, now_utc or utcnow_naive())

    return run_with_deadlock_retry(attempt, label=f"manual shift={shift_id} worker={employee_id}")


def _punch_once(
    shift_id: int, employee_id: int, action: str, local_time: datetime, note: str, now_utc: datetime
) -> ManualPunchResult:
    try:
        # (1) worker row, (2) assignment row — the shared lock order.
        employee = lock_employee(employee_id)
        assignment = (
            db.session.query(ShiftAssignment)
            .filter(ShiftAssignment.shift_id == shift_id, ShiftAssignment.employee_id == employee_id)
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
        if assignment is None or assignment.status != AssignmentStatus.accepted:
            db.session.rollback()
            return ManualPunchResult(False, f"{employee.full_name} isn't confirmed on this shift.")
        shift, tz = assignment.shift, assignment.shift.job.timezone
        if shift.status not in (ShiftStatus.OPEN, ShiftStatus.FILLED):
            db.session.rollback()
            return ManualPunchResult(False, "This shift isn't open (draft or cancelled).")

        punch_utc = local_to_utc_naive(local_time.replace(second=0, microsecond=0), tz)
        if punch_utc > now_utc + FUTURE_ALLOWANCE:
            db.session.rollback()
            return ManualPunchResult(False, "That time is in the future — punches record what already happened.")
        _opens, start_utc, _end = check_in_window_utc(assignment)
        if abs(punch_utc - start_utc) > MAX_DISTANCE_FROM_START:
            db.session.rollback()
            return ManualPunchResult(False, "That time is more than a day from the shift start — check the date.")

        if action == "in":
            if assignment.check_in_at_utc is not None:
                db.session.rollback()
                return ManualPunchResult(
                    False,
                    f"{employee.full_name} is already checked in at "
                    f"{format_local_clock(assignment.check_in_at_utc, tz)}; nothing was changed.",
                )
            other = open_punch_for(employee.id)
            if other is not None and other.id != assignment.id:
                db.session.rollback()
                return ManualPunchResult(False, f"{employee.full_name} is still checked in to another shift.")
            record_check_in(assignment, punch_utc, PunchSource.ADMIN)
            recorded = assignment.check_in_at_utc
        else:
            if assignment.check_in_at_utc is None:
                db.session.rollback()
                return ManualPunchResult(False, f"Check {employee.full_name} in before checking them out.")
            if assignment.check_out_at_utc is not None:
                db.session.rollback()
                return ManualPunchResult(
                    False,
                    f"{employee.full_name} is already checked out at "
                    f"{format_local_clock(assignment.check_out_at_utc, tz)}; nothing was changed.",
                )
            if punch_utc < assignment.check_in_at_utc:
                db.session.rollback()
                return ManualPunchResult(
                    False,
                    f"Check-out can't be before check-in ({format_local_clock(assignment.check_in_at_utc, tz)}).",
                )
            assignment.check_out_at_utc = punch_utc
            assignment.check_out_source = PunchSource.ADMIN
            recorded = punch_utc

        label = "IN" if action == "in" else "OUT"
        entry = f"{label}: {note}" if note else f"{label}: manual"
        assignment.punch_note = "; ".join(p for p in (assignment.punch_note, entry) if p)[:255]
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    logger.info("Manual %s for assignment %s at %s UTC.", action, assignment.id, recorded)
    verb = "checked in" if action == "in" else "checked out"
    snapped = " (snapped to shift start)" if action == "in" and assignment.check_in_was_snapped else ""
    return ManualPunchResult(True, f"{employee.full_name} {verb} at {format_local_clock(recorded, tz)}{snapped}.")
