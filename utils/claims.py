"""Claiming and releasing a seat on a shift.

Both the single-shift link (``/accept/<token>``) and the weekly portal
(``/my/<portal_token>``) let a worker take a seat, so the locking lives here
rather than in either route. Two copies of "first come, first served" would
eventually disagree, and the way you find out is two people turning up for
one seat.

Lock order is the same as every other write path: employee row first, then
the shift, then the assignment. Seat counting uses a locking read, because
under REPEATABLE READ a plain COUNT can serve a snapshot taken before a
competing acceptance committed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from extensions import db
from models import AssignmentStatus, ShiftAssignment, ShiftStatus
from utils.assignments import lock_assignment_shift, locked_accepted_count
from utils.web_punch import ensure_punch_token

logger = logging.getLogger(__name__)


class ClaimOutcome(Enum):
    ACCEPTED = "accepted"          # the seat is theirs
    ALREADY_ACCEPTED = "already"   # they already had it; not an error
    FULL = "full"                  # someone else took the last seat
    CLOSED = "closed"              # cancelled shift, or an inactive worker


@dataclass
class ClaimResult:
    outcome: ClaimOutcome
    assignment: ShiftAssignment
    shift: object
    just_filled: bool = False      # this claim took the final seat


def claim_seat(assignment_id: int, shift_id: int) -> ClaimResult:
    """Take a seat if one remains. Commits, and never overfills.

    Caller is responsible for retrying on deadlock (``run_with_deadlock_retry``)
    and for any notification, which must happen after this returns so no
    outbound call is made while the shift row is locked.
    """
    assignment, shift = lock_assignment_shift(assignment_id, shift_id)

    if shift.status not in (ShiftStatus.OPEN, ShiftStatus.FILLED) or not assignment.employee.is_active:
        db.session.rollback()
        return ClaimResult(ClaimOutcome.CLOSED, assignment, shift)

    if assignment.status == AssignmentStatus.accepted:
        db.session.rollback()
        return ClaimResult(ClaimOutcome.ALREADY_ACCEPTED, assignment, shift)

    if assignment.status != AssignmentStatus.pending:
        db.session.rollback()
        return ClaimResult(ClaimOutcome.CLOSED, assignment, shift)

    taken = locked_accepted_count(shift.id)
    if taken >= shift.required_headcount:
        # Record that it is full so later GETs stop offering it.
        shift.status = ShiftStatus.FILLED
        db.session.commit()
        return ClaimResult(ClaimOutcome.FULL, assignment, shift)

    assignment.status = AssignmentStatus.accepted
    ensure_punch_token(assignment)
    just_filled = taken + 1 >= shift.required_headcount
    if just_filled:
        shift.status = ShiftStatus.FILLED
    db.session.commit()
    logger.info("Assignment %s accepted for shift %s.", assignment_id, shift_id)
    return ClaimResult(ClaimOutcome.ACCEPTED, assignment, shift, just_filled)


def release_seat(assignment_id: int, shift_id: int) -> ClaimResult:
    """Decline an offer. Idempotent, and reopens a shift that was full."""
    assignment, shift = lock_assignment_shift(assignment_id, shift_id)

    if assignment.status == AssignmentStatus.rejected:
        db.session.rollback()
        return ClaimResult(ClaimOutcome.CLOSED, assignment, shift)

    was_accepted = assignment.status == AssignmentStatus.accepted
    assignment.status = AssignmentStatus.rejected
    db.session.flush()

    if was_accepted and locked_accepted_count(shift.id) < shift.required_headcount:
        if shift.status == ShiftStatus.FILLED:
            shift.status = ShiftStatus.OPEN
        # Re-arm the understaffing alert: the seat is open again.
        shift.understaffed_alert_sent_at = None

    db.session.commit()
    return ClaimResult(ClaimOutcome.CLOSED, assignment, shift)
