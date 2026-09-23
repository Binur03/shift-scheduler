"""Current reads for seat changes; all callers lock shift before assignment."""
from flask import abort
from extensions import db
from models import AssignmentStatus, Shift, ShiftAssignment


def lock_assignment_shift(assignment_id: int, shift_id: int):
    shift = (Shift.query.filter_by(id=shift_id).with_for_update()
             .populate_existing().one_or_none())
    if shift is None:
        abort(404)
    assignment = (ShiftAssignment.query.filter_by(id=assignment_id, shift_id=shift_id)
                  .with_for_update().populate_existing().one_or_none())
    if assignment is None:
        abort(404)
    return assignment, shift


def locked_accepted_count(shift_id: int) -> int:
    return len(db.session.query(ShiftAssignment.id).filter(
        ShiftAssignment.shift_id == shift_id,
        ShiftAssignment.status == AssignmentStatus.accepted,
    ).with_for_update().all())
