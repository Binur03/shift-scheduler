"""Two-way SMS time punches: workers text IN / OUT to log their hours.

Concurrency model
-----------------
Twilio can deliver two webhooks for the same worker at the same moment (a
double-tap, a retry racing the original, back-to-back shifts). Every punch
runs in one transaction that takes row locks in a fixed order:

1. ``employees`` row  — ``SELECT ... FOR UPDATE``. A per-worker mutex: all
   punches for one worker serialize; punches for different workers never
   block each other.
2. ``shift_assignments`` rows — ``SELECT ... FOR UPDATE OF shift_assignments``
   for the candidate(s), then eligibility is evaluated under that lock.

The fixed lock order (employee -> assignment) rules out deadlocks between
punch transactions.

Why the eligibility reads are *locking* reads: under InnoDB's default
REPEATABLE READ, a plain SELECT issued after waiting on a lock still reads
the snapshot taken before the wait and could miss a check-in that the other
transaction just committed. Locking reads always see the latest committed
rows, so a racing duplicate is correctly seen as "already checked in".

Idempotency: every webhook is logged in ``inbound_sms`` keyed by Twilio's
unique ``MessageSid``. A retry finds the stored row (or collides on the
unique index) and gets the original reply without punching twice.

Deadlocks: the per-worker mutex removes lock-order deadlocks, but InnoDB's
next-key (gap) locks on secondary indexes can still make two *different*
workers' transactions collide (e.g. a burst of OUTs). InnoDB resolves that by
aborting one transaction (1213 deadlock / 1205 lock-wait timeout); the whole
punch is simply retried, which is safe because nothing was committed.

All timestamps are naive UTC; replies render them in the venue's timezone.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError, OperationalError

from extensions import db
from models import (
    AssignmentStatus,
    Employee,
    InboundSms,
    PunchResult,
    PunchSource,
    Shift,
    ShiftAssignment,
    ShiftStatus,
    utcnow_naive,
)
from utils.timeutil import format_local_clock, shift_window_utc

logger = logging.getLogger(__name__)

# "IN", "in.", "Check in", "CLOCK-OUT!" ... — anything else is malformed.
_COMMAND_RE = re.compile(
    r"^\s*(?:(?:check|clock)[\s-]*)?(?P<cmd>in|out)\s*[.!]*\s*$", re.IGNORECASE
)
# Carrier / Twilio-handled keywords. We log them but never reply.
_OPT_KEYWORDS = {
    "STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT",
    "HELP", "INFO", "START", "UNSTOP", "YES",
}

MAX_BODY_CHARS = 1600
MAX_ATTEMPTS = 4  # first try + 3 retries on deadlock / lock-wait timeout
_RETRYABLE_MYSQL_ERRORS = {1213, 1205}

REPLY_MALFORMED = (
    "Sorry, we didn't understand. Text IN when you arrive for your shift "
    "or OUT when you leave."
)
REPLY_UNKNOWN_SENDER = (
    "This number isn't registered for shift check-in. Please contact your coordinator."
)


@dataclass(frozen=True)
class PunchOutcome:
    result: str
    reply: str | None  # None -> respond with empty TwiML (no SMS back)
    assignment_id: int | None = None


def parse_command(body: str) -> str | None:
    """Return "IN", "OUT", "OPT" (carrier keyword) or None for anything else."""
    text = (body or "").strip()
    match = _COMMAND_RE.match(text)
    if match:
        return match.group("cmd").upper()
    if text.upper().rstrip(".!") in _OPT_KEYWORDS:
        return "OPT"
    return None


def _early_window() -> timedelta:
    return timedelta(minutes=int(os.environ.get("PUNCH_EARLY_MINUTES", 60)))


def _prior_outcome(message_sid: str) -> PunchOutcome | None:
    row = InboundSms.query.filter_by(message_sid=message_sid).first()
    if row is None:
        return None
    return PunchOutcome(row.result, row.reply_body, row.assignment_id)


def _log(
    *,
    message_sid: str,
    from_number: str,
    body: str,
    now_utc: datetime,
    outcome: PunchOutcome,
    employee_id: int | None,
) -> None:
    db.session.add(
        InboundSms(
            message_sid=message_sid,
            from_number=from_number[:20],
            body=body,
            received_at_utc=now_utc,
            employee_id=employee_id,
            assignment_id=outcome.assignment_id,
            result=outcome.result,
            reply_body=outcome.reply,
        )
    )


def _open_punch(employee_id: int) -> ShiftAssignment | None:
    """The assignment this worker is currently checked in to (locked)."""
    return (
        db.session.query(ShiftAssignment)
        .filter(
            ShiftAssignment.employee_id == employee_id,
            ShiftAssignment.check_in_at_utc.is_not(None),
            ShiftAssignment.check_out_at_utc.is_(None),
        )
        .order_by(ShiftAssignment.check_in_at_utc.desc())
        .with_for_update(of=ShiftAssignment)
        .first()
    )


def _punch_in(employee: Employee, message_sid: str, now_utc: datetime) -> PunchOutcome:
    open_punch = _open_punch(employee.id)
    if open_punch is not None:
        shift = open_punch.shift
        return PunchOutcome(
            PunchResult.DUPLICATE,
            f"You're already checked in for {shift.job.title} "
            f"(since {format_local_clock(open_punch.check_in_at_utc, shift.job.timezone)}). "
            "Text OUT when you leave.",
            open_punch.id,
        )

    # Bound candidates by date in SQL (±1 day covers any venue offset and
    # overnight shifts), then test the exact UTC window in Python.
    today = now_utc.date()
    candidates = (
        db.session.query(ShiftAssignment)
        .join(Shift, Shift.id == ShiftAssignment.shift_id)
        .filter(
            ShiftAssignment.employee_id == employee.id,
            ShiftAssignment.status == AssignmentStatus.accepted,
            ShiftAssignment.check_in_at_utc.is_(None),
            Shift.status.in_([ShiftStatus.OPEN, ShiftStatus.FILLED]),
            Shift.date >= today - timedelta(days=1),
            Shift.date <= today + timedelta(days=1),
        )
        .order_by(Shift.date, Shift.start_time)
        .with_for_update(of=ShiftAssignment)
        .all()
    )

    early = _early_window()
    for assignment in candidates:
        shift = assignment.shift
        opens_at, _start, end_utc = check_in_window_utc(assignment)
        if opens_at <= now_utc <= end_utc:
            assignment.check_in_at_utc = now_utc
            assignment.check_in_message_sid = message_sid
            assignment.check_in_source = PunchSource.SMS
            return PunchOutcome(
                PunchResult.CHECKED_IN,
                f"Checked in for {shift.job.title} at "
                f"{format_local_clock(now_utc, shift.job.timezone)}. "
                "Text OUT when you leave.",
                assignment.id,
            )

    minutes = int(early.total_seconds() // 60)
    return PunchOutcome(
        PunchResult.NO_SHIFT,
        "No shift found to check in to right now. You can check in up to "
        f"{minutes} min before a confirmed shift starts.",
    )


def _punch_out(employee: Employee, message_sid: str, now_utc: datetime) -> PunchOutcome:
    open_punch = _open_punch(employee.id)
    if open_punch is not None:
        shift = open_punch.shift
        open_punch.check_out_at_utc = now_utc
        open_punch.check_out_message_sid = message_sid
        open_punch.check_out_source = PunchSource.SMS
        return PunchOutcome(
            PunchResult.CHECKED_OUT,
            f"Checked out of {shift.job.title} at "
            f"{format_local_clock(now_utc, shift.job.timezone)} "
            f"({open_punch.worked_hours:g} hrs). Thanks!",
            open_punch.id,
        )

    # A repeated OUT shortly after a real one is a duplicate, not an error.
    recent = (
        db.session.query(ShiftAssignment)
        .filter(
            ShiftAssignment.employee_id == employee.id,
            ShiftAssignment.check_out_at_utc >= now_utc - timedelta(hours=12),
        )
        .order_by(ShiftAssignment.check_out_at_utc.desc())
        .with_for_update(of=ShiftAssignment)
        .first()
    )
    if recent is not None:
        shift = recent.shift
        return PunchOutcome(
            PunchResult.DUPLICATE,
            f"You already checked out of {shift.job.title} at "
            f"{format_local_clock(recent.check_out_at_utc, shift.job.timezone)}.",
            recent.id,
        )
    return PunchOutcome(
        PunchResult.NO_SHIFT,
        "You're not checked in to a shift. Text IN when you arrive.",
    )


def _is_retryable(exc: OperationalError) -> bool:
    args = getattr(exc.orig, "args", ())
    return bool(args) and args[0] in _RETRYABLE_MYSQL_ERRORS


# --------------------------------------------------------------------------- #
# Shared by SMS punches and web PIN punches (utils/web_punch.py)
# --------------------------------------------------------------------------- #
def check_in_window_utc(assignment: ShiftAssignment) -> tuple[datetime, datetime, datetime]:
    """(check-in opens, scheduled start, scheduled end) for an assignment, naive UTC."""
    shift = assignment.shift
    start_utc, end_utc = shift_window_utc(
        shift.date, shift.start_time, shift.end_time, shift.job.timezone
    )
    return start_utc - _early_window(), start_utc, end_utc


def open_punch_for(employee_id: int) -> ShiftAssignment | None:
    """Locked read of the assignment a worker is currently checked in to."""
    return _open_punch(employee_id)


def lock_employee(employee_id: int) -> Employee:
    """Per-worker mutex. Always taken BEFORE any assignment row lock."""
    return (
        db.session.query(Employee)
        .filter(Employee.id == employee_id)
        .with_for_update()
        .populate_existing()  # refresh any stale identity-map copy from the locked row
        .one()
    )


def run_with_deadlock_retry(fn, label: str):
    """Run a punch transaction, retrying InnoDB deadlocks / lock-wait timeouts.

    ``fn`` must be self-contained (start from fresh reads and commit itself);
    it is re-run from scratch after a rollback, which is safe because nothing
    from the failed attempt was committed.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except OperationalError as exc:
            db.session.rollback()
            if not _is_retryable(exc) or attempt == MAX_ATTEMPTS:
                raise
            logger.warning(
                "Punch %s hit InnoDB error %s; retrying (attempt %d).",
                label, exc.orig.args[0], attempt,
            )
            time.sleep(0.02 * attempt)
    raise AssertionError("unreachable")


def handle_inbound_sms(
    *,
    message_sid: str,
    from_number: str,
    body: str,
    now_utc: datetime | None = None,
) -> PunchOutcome:
    """Process one (already signature-verified) inbound SMS and commit.

    Always returns an outcome for the TwiML reply; raises only on genuine
    infrastructure errors (DB down), in which case Twilio's retry is safe
    because nothing was committed. InnoDB deadlocks are retried here.
    """
    now_utc = now_utc or utcnow_naive()
    body = (body or "")[:MAX_BODY_CHARS]
    return run_with_deadlock_retry(
        lambda: _process_once(
            message_sid=message_sid, from_number=from_number, body=body, now_utc=now_utc
        ),
        label=message_sid,
    )


def _process_once(
    *, message_sid: str, from_number: str, body: str, now_utc: datetime
) -> PunchOutcome:
    log_kwargs = dict(
        message_sid=message_sid, from_number=from_number, body=body, now_utc=now_utc
    )

    prior = _prior_outcome(message_sid)
    if prior is not None:
        db.session.rollback()
        return prior

    command = parse_command(body)
    employee = Employee.query.filter_by(phone_number=from_number, is_active=True).first()

    try:
        if command == "OPT":
            outcome = PunchOutcome(PunchResult.OPT_KEYWORD, None)
            _log(**log_kwargs, outcome=outcome, employee_id=employee.id if employee else None)
        elif employee is None:
            outcome = PunchOutcome(PunchResult.UNKNOWN_SENDER, REPLY_UNKNOWN_SENDER)
            _log(**log_kwargs, outcome=outcome, employee_id=None)
        elif command is None:
            outcome = PunchOutcome(PunchResult.MALFORMED, REPLY_MALFORMED)
            _log(**log_kwargs, outcome=outcome, employee_id=employee.id)
        else:
            # (1) Per-worker mutex. Concurrent punches for this worker wait here.
            locked = lock_employee(employee.id)
            # (2) Assignment rows are locked inside _punch_in / _punch_out.
            if command == "IN":
                outcome = _punch_in(locked, message_sid, now_utc)
            else:
                outcome = _punch_out(locked, message_sid, now_utc)
            _log(**log_kwargs, outcome=outcome, employee_id=locked.id)

        db.session.commit()
    except IntegrityError:
        # Same MessageSid committed by a concurrent delivery of this webhook:
        # discard our work and return the original reply.
        db.session.rollback()
        prior = _prior_outcome(message_sid)
        if prior is not None:
            return prior
        raise
    except OperationalError:
        raise  # rolled back and possibly retried by handle_inbound_sms
    except Exception:
        db.session.rollback()
        raise

    logger.info(
        "Inbound SMS %s from %s: %s (assignment=%s)",
        message_sid, from_number, outcome.result, outcome.assignment_id,
    )
    return outcome
