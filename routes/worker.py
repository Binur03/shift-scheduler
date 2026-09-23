"""Public worker blueprint: token-based shift acceptance.

Concurrency model
-----------------
Multiple workers may tap "Accept" for the same shift simultaneously. To
guarantee a shift is never over-filled we serialize acceptors on the *shift
row* using MySQL InnoDB row-level locking:

1. ``SELECT ... FROM shifts WHERE id = :id FOR UPDATE`` takes an exclusive lock
   on the single shift row. Every concurrent acceptor must acquire this same
   lock first, so they proceed strictly one at a time.
2. While holding the lock we use a current locking read of accepted assignments.
3. Only if ``accepted_count < required_headcount`` do we flip this assignment
   to ``accepted`` (and the shift to ``filled`` if this was the last seat),
   then commit — releasing the lock. Otherwise we roll back and return HTTP 409
   with the "Shift Full" view.

Note on transactions: SQLAlchemy 2.0 removed the ``subtransactions=True`` flag.
The session is already in an implicit transaction, so we manage an explicit
boundary with ``commit()`` / ``rollback()`` and a guarding ``try/except`` that
rolls back on errors and retries transient InnoDB deadlocks.
"""
from __future__ import annotations

import logging

from flask import Blueprint, Response, abort, jsonify, make_response, redirect, render_template, request, url_for

from extensions import db, limiter
from models import AssignmentStatus, Shift, ShiftAssignment, ShiftStatus, utcnow_naive
from utils.assignments import lock_assignment_shift, locked_accepted_count
from utils.i18n import use_worker_language
from utils.punch import run_with_deadlock_retry
from utils.web_punch import ensure_punch_token, shift_summary, web_punch

logger = logging.getLogger(__name__)

worker_bp = Blueprint("worker", __name__)


@worker_bp.after_request
def private_worker_response(response):
    # Acceptance and punch links both grant access to a worker's shift.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _load_assignment(token: str) -> ShiftAssignment:
    """Fetch an assignment by token (eager-loading shift + job) or 404."""
    assignment = (
        db.session.query(ShiftAssignment)
        .filter(ShiftAssignment.token == token)
        .first()
    )
    if assignment is None:
        logger.info("Unknown acceptance token requested: %s", token)
        abort(404)
    # A worker's own links open in their language unless this browser chose one.
    use_worker_language(assignment.employee)
    return assignment


@worker_bp.route("/accept/<token>", methods=["GET"])
def view_offer(token: str) -> Response | str:
    """Render the offer page for a token.

    If the parent shift is already filled, show the "full" view; if this
    assignment is itself already accepted, show the confirmation; otherwise
    present the acceptance form.
    """
    assignment = _load_assignment(token)
    shift = assignment.shift

    if shift.status not in (ShiftStatus.OPEN, ShiftStatus.FILLED) or not assignment.employee.is_active:
        return render_template("worker/closed.html", assignment=assignment, shift=shift)
    if assignment.status == AssignmentStatus.accepted:
        return render_template(
            "worker/confirmed.html", assignment=assignment, shift=shift
        )
    if assignment.status in (AssignmentStatus.cancelled, AssignmentStatus.rejected):
        return render_template(
            "worker/closed.html", assignment=assignment, shift=shift
        )
    if shift.status == ShiftStatus.FILLED or shift.is_full:
        return render_template("worker/full.html", shift=shift)

    return render_template("worker/accept.html", assignment=assignment, shift=shift)


@worker_bp.route("/accept/<token>", methods=["POST"])
def accept_offer(token: str) -> Response | str | tuple[str, int]:
    """Atomically accept the shift if capacity remains.

    Returns the confirmation view on success, or the "Shift Full" view with an
    HTTP 409 status if no seats remain.
    """
    initial = _load_assignment(token)
    assignment_id, shift_id = initial.id, initial.shift_id

    def attempt():
        assignment, shift = lock_assignment_shift(assignment_id, shift_id)
        if shift.status not in (ShiftStatus.OPEN, ShiftStatus.FILLED) or not assignment.employee.is_active:
            db.session.rollback()
            return render_template("worker/closed.html", assignment=assignment, shift=shift)
        if assignment.status == AssignmentStatus.accepted:
            db.session.rollback()
            return render_template("worker/confirmed.html", assignment=assignment, shift=shift)
        if assignment.status != AssignmentStatus.pending:
            db.session.rollback()
            return render_template("worker/closed.html", assignment=assignment, shift=shift)

        # A normal COUNT can read the snapshot from the initial token lookup
        # under MySQL REPEATABLE READ. Locking reads see committed seats NOW.
        count = locked_accepted_count(shift.id)
        if count >= shift.required_headcount:
            shift.status = ShiftStatus.FILLED
            db.session.commit()
            return render_template("worker/full.html", shift=shift), 409

        assignment.status = AssignmentStatus.accepted
        ensure_punch_token(assignment)
        if count + 1 >= shift.required_headcount:
            shift.status = ShiftStatus.FILLED
        db.session.commit()
        logger.info("Assignment %s accepted for shift %s.", assignment_id, shift_id)
        return render_template("worker/confirmed.html", assignment=assignment, shift=shift)

    try:
        return run_with_deadlock_retry(attempt, label=f"accept assignment={assignment_id}")
    except Exception:
        db.session.rollback()
        raise


@worker_bp.route("/accept/<token>/check-in", methods=["POST"])
def open_check_in(token: str):
    """Open the PIN screen from a confirmed invitation, including older offers.

    Creates only the private link if missing; timestamps still require a PIN.
    """
    initial = _load_assignment(token)
    assignment_id, shift_id = initial.id, initial.shift_id

    def attempt():
        assignment, shift = lock_assignment_shift(assignment_id, shift_id)
        if (assignment.status != AssignmentStatus.accepted
                or shift.status not in (ShiftStatus.OPEN, ShiftStatus.FILLED)
                or not assignment.employee.is_active):
            db.session.rollback()
            return render_template("worker/closed.html", assignment=assignment, shift=shift), 409
        punch_token = ensure_punch_token(assignment)
        db.session.commit()
        return redirect(url_for("worker.punch_page", token=punch_token))

    try:
        return run_with_deadlock_retry(attempt, label=f"open check-in assignment={assignment_id}")
    except Exception:
        db.session.rollback()
        raise


@worker_bp.route("/decline/<token>", methods=["POST"])
def decline_offer(token: str) -> Response | str:
    """Decline a pending offer, serialized with acceptance and cancellation."""
    initial = _load_assignment(token)
    assignment_id, shift_id = initial.id, initial.shift_id

    def attempt():
        assignment, shift = lock_assignment_shift(assignment_id, shift_id)
        if assignment.status == AssignmentStatus.accepted:
            db.session.rollback()
            return render_template("worker/confirmed.html", assignment=assignment, shift=shift)
        if assignment.status == AssignmentStatus.pending:
            assignment.status = AssignmentStatus.rejected
        db.session.commit()
        return render_template("worker/declined.html", assignment=assignment, shift=shift)

    try:
        return run_with_deadlock_retry(attempt, label=f"decline assignment={assignment_id}")
    except Exception:
        db.session.rollback()
        raise


# --------------------------------------------------------------------------- #
# Mobile PIN check-in / check-out
# --------------------------------------------------------------------------- #
def _private(response: Response) -> Response:
    """The punch URL is a credential: keep it out of caches, referrers, indexes."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@worker_bp.route("/punch/<token>", methods=["GET"])
@limiter.limit("60 per minute")
def punch_page(token: str):
    """Render the keypad for one confirmed shift."""
    assignment = (
        ShiftAssignment.query.filter_by(punch_token=token).first() if len(token) <= 64 else None
    )
    if assignment is None:
        return _private(make_response(render_template("errors/404.html"), 404))
    use_worker_language(assignment.employee)
    summary = shift_summary(assignment, utcnow_naive())
    return _private(make_response(render_template("worker/punch.html", token=token, s=summary)))


@worker_bp.route("/api/punch", methods=["POST"])
@limiter.limit("30 per minute")
def api_punch():
    """JSON: {"token": str, "pin": "1234", "action": "in"|"out"}.

    CSRF-exempt by design (blueprint): there is no session or cookie to ride —
    the token + PIN in the body are the credentials. JSON-only, so a plain
    cross-site HTML form can't submit here.
    """
    if not request.is_json:
        return _private(jsonify(error="json_required")), 415
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _private(jsonify(error="bad_request")), 400

    result = web_punch(
        token=str(data.get("token") or ""),
        pin=data.get("pin"),
        action=data.get("action"),
        client_ip=request.remote_addr,
    )
    response = _private(jsonify(result.body))
    if result.http_status == 423:
        response.headers["Retry-After"] = str(result.body.get("retry_after_seconds", 900))
    return response, result.http_status
