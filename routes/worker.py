"""Public worker blueprint: token-based shift acceptance.

Concurrency model
-----------------
Multiple workers may tap "Accept" for the same shift simultaneously. To
guarantee a shift is never over-filled we serialize acceptors on the *shift
row* using MySQL InnoDB row-level locking:

1. ``SELECT ... FROM shifts WHERE id = :id FOR UPDATE`` takes an exclusive lock
   on the single shift row. Every concurrent acceptor must acquire this same
   lock first, so they proceed strictly one at a time.
2. While holding the lock we count assignments already in ``accepted`` state.
3. Only if ``accepted_count < required_headcount`` do we flip this assignment
   to ``accepted`` (and the shift to ``filled`` if this was the last seat),
   then commit — releasing the lock. Otherwise we roll back and return HTTP 409
   with the "Shift Full" view.

Note on transactions: SQLAlchemy 2.0 removed the ``subtransactions=True`` flag.
The session is already in an implicit transaction, so we manage an explicit
boundary with ``commit()`` / ``rollback()`` and a guarding ``try/except`` that
logs and rolls back on any unexpected error.
"""
from __future__ import annotations

import logging

from flask import Blueprint, Response, abort, jsonify, make_response, render_template, request
from sqlalchemy import func

from extensions import db, limiter
from models import AssignmentStatus, Shift, ShiftAssignment, ShiftStatus, utcnow_naive
from utils.web_punch import ensure_punch_token, shift_summary, web_punch

logger = logging.getLogger(__name__)

worker_bp = Blueprint("worker", __name__)


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
    assignment = _load_assignment(token)

    # Idempotency / terminal-state short circuits (no locking required).
    if assignment.status == AssignmentStatus.accepted:
        return render_template(
            "worker/confirmed.html", assignment=assignment, shift=assignment.shift
        )
    if assignment.status in (AssignmentStatus.cancelled, AssignmentStatus.rejected):
        return render_template(
            "worker/closed.html", assignment=assignment, shift=assignment.shift
        )

    try:
        # (1) Acquire an exclusive InnoDB row lock on the shift. Concurrent
        # acceptors block here until the current transaction commits/rolls back.
        shift = (
            db.session.query(Shift)
            .filter(Shift.id == assignment.shift_id)
            .with_for_update()
            .first()
        )
        if shift is None:
            db.session.rollback()
            abort(404)

        # (2) Count seats already taken, evaluated under the lock.
        current_accepted_count = (
            db.session.query(func.count(ShiftAssignment.id))
            .filter(
                ShiftAssignment.shift_id == shift.id,
                ShiftAssignment.status == AssignmentStatus.accepted,
            )
            .scalar()
        ) or 0

        # (3a) No capacity: leave this assignment pending, undo any pending
        # changes, and report a conflict.
        if current_accepted_count >= shift.required_headcount:
            db.session.rollback()
            logger.info(
                "Shift %s full (%d/%d); rejecting acceptance for assignment %s.",
                shift.id,
                current_accepted_count,
                shift.required_headcount,
                assignment.id,
            )
            # Ensure the shift is flagged filled for future GETs.
            if shift.status != ShiftStatus.FILLED:
                shift.status = ShiftStatus.FILLED
                db.session.commit()
            return render_template("worker/full.html", shift=shift), 409

        # (3b) Capacity available: accept this worker.
        assignment.status = AssignmentStatus.accepted
        ensure_punch_token(assignment)  # secret link for the PIN check-in keypad
        if current_accepted_count + 1 >= shift.required_headcount:
            shift.status = ShiftStatus.FILLED

        db.session.commit()
        logger.info(
            "Assignment %s accepted for shift %s (%d/%d).",
            assignment.id,
            shift.id,
            current_accepted_count + 1,
            shift.required_headcount,
        )
        return render_template(
            "worker/confirmed.html", assignment=assignment, shift=shift
        )

    except Exception:  # noqa: BLE001 - log, release locks, surface a 500
        db.session.rollback()
        logger.exception(
            "Unexpected error accepting token=%s (assignment=%s).",
            token,
            getattr(assignment, "id", "?"),
        )
        raise


@worker_bp.route("/decline/<token>", methods=["POST"])
def decline_offer(token: str) -> Response | str:
    """Mark the worker as not available for this shift.

    Only a *pending* assignment can be declined. Accepted assignments must go
    through the coordinator (freeing a seat has scheduling consequences), and
    already-closed assignments simply re-render their terminal view.
    """
    assignment = _load_assignment(token)

    if assignment.status == AssignmentStatus.accepted:
        return render_template(
            "worker/confirmed.html", assignment=assignment, shift=assignment.shift
        )
    if assignment.status in (AssignmentStatus.cancelled, AssignmentStatus.rejected):
        return render_template(
            "worker/declined.html", assignment=assignment, shift=assignment.shift
        )

    assignment.status = AssignmentStatus.rejected
    db.session.commit()
    logger.info(
        "Assignment %s declined (worker not available) for shift %s.",
        assignment.id,
        assignment.shift_id,
    )
    return render_template(
        "worker/declined.html", assignment=assignment, shift=assignment.shift
    )


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
