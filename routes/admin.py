"""Admin blueprint: CRUD for employees, jobs, and shifts plus token dispatch.

These routes are intended to sit behind authentication (not implemented in
this scaffold). Add an auth guard via a before_request hook before exposing
them publicly.
"""
import secrets
from datetime import date, datetime, timedelta

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import (
    AssignmentStatus,
    Employee,
    Job,
    Shift,
    ShiftAssignment,
    ShiftStatus,
)
from utils.cli import normalize_e164
from utils.sms import ShiftBroadcastDetails, WhatsAppService

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.before_request
def require_admin():
    """Gate every /admin route behind the password session (see routes/auth.py)."""
    if not session.get("is_admin"):
        return redirect(url_for("auth.login", next=request.path))


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@admin_bp.route("/employees")
def list_employees():
    employees = Employee.query.order_by(Employee.last_name, Employee.first_name).all()
    return render_template("admin/employees.html", employees=employees)


@admin_bp.route("/employees", methods=["POST"])
def create_employee():
    import os

    try:
        phone = normalize_e164(
            request.form["phone_number"],
            default_country_code=os.environ.get("DEFAULT_COUNTRY_CODE", "1"),
        )
    except ValueError:
        flash(
            "That phone number doesn't look valid. Use digits with area code, "
            "e.g. +13035550123 or 303-555-0123.",
            "error",
        )
        return redirect(url_for("admin.list_employees"))

    employee = Employee(
        first_name=request.form["first_name"].strip(),
        last_name=request.form["last_name"].strip(),
        phone_number=phone,
        is_active=True,
    )
    db.session.add(employee)
    try:
        db.session.commit()
        flash("Employee added.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("That phone number already exists.", "error")
    return redirect(url_for("admin.list_employees"))


@admin_bp.route("/employees/<int:employee_id>/deactivate", methods=["POST"])
def deactivate_employee(employee_id: int):
    employee = db.session.get(Employee, employee_id) or abort(404)
    employee.is_active = False
    db.session.commit()
    flash("Employee deactivated.", "success")
    return redirect(url_for("admin.list_employees"))


@admin_bp.route("/employees/<int:employee_id>/activate", methods=["POST"])
def activate_employee(employee_id: int):
    employee = db.session.get(Employee, employee_id) or abort(404)
    employee.is_active = True
    db.session.commit()
    flash("Employee reactivated.", "success")
    return redirect(url_for("admin.list_employees"))


@admin_bp.route("/employees/<int:employee_id>/delete", methods=["POST"])
def delete_employee(employee_id: int):
    employee = db.session.get(Employee, employee_id) or abort(404)

    # Deleting cascades to assignments, which would silently drop this worker
    # from shifts they've already committed to. Force the seats to be freed
    # explicitly first (which reopens shifts and re-arms alerts).
    upcoming_accepted = (
        db.session.query(ShiftAssignment)
        .join(Shift, Shift.id == ShiftAssignment.shift_id)
        .filter(
            ShiftAssignment.employee_id == employee.id,
            ShiftAssignment.status == AssignmentStatus.accepted,
            Shift.date >= date.today(),
        )
        .count()
    )
    if upcoming_accepted:
        flash(
            f"{employee.full_name} is confirmed on {upcoming_accepted} upcoming "
            "shift(s). Remove them from those shifts first (so the seats "
            "reopen), or deactivate them instead.",
            "error",
        )
        return redirect(url_for("admin.list_employees"))

    db.session.delete(employee)
    db.session.commit()
    flash("Employee deleted.", "success")
    return redirect(url_for("admin.list_employees"))


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
@admin_bp.route("/jobs")
def list_jobs():
    jobs = Job.query.order_by(Job.title).all()
    return render_template("admin/jobs.html", jobs=jobs)


@admin_bp.route("/jobs", methods=["POST"])
def create_job():
    job = Job(
        title=request.form["title"].strip(),
        location_address=request.form["location_address"].strip(),
        default_headcount=int(request.form.get("default_headcount", 1)),
    )
    db.session.add(job)
    db.session.commit()
    flash("Job created.", "success")
    return redirect(url_for("admin.list_jobs"))


@admin_bp.route("/jobs/<int:job_id>/update", methods=["POST"])
def update_job(job_id: int):
    job = db.session.get(Job, job_id) or abort(404)
    job.title = request.form["title"].strip()
    job.location_address = request.form["location_address"].strip()
    job.default_headcount = int(request.form.get("default_headcount", job.default_headcount))
    db.session.commit()
    flash("Job updated.", "success")
    return redirect(url_for("admin.list_jobs"))


@admin_bp.route("/jobs/<int:job_id>/delete", methods=["POST"])
def delete_job(job_id: int):
    job = db.session.get(Job, job_id) or abort(404)
    db.session.delete(job)
    db.session.commit()
    flash("Job deleted.", "success")
    return redirect(url_for("admin.list_jobs"))


# --------------------------------------------------------------------------- #
# Shifts
# --------------------------------------------------------------------------- #
@admin_bp.route("/shifts")
def list_shifts():
    today = date.today()
    upcoming = (
        Shift.query.filter(Shift.date >= today)
        .order_by(Shift.date, Shift.start_time)
        .all()
    )
    past = (
        Shift.query.filter(Shift.date < today)
        .order_by(Shift.date.desc(), Shift.start_time)
        .limit(20)
        .all()
    )
    jobs = Job.query.order_by(Job.title).all()
    this_monday = today - timedelta(days=today.weekday())
    return render_template(
        "admin/shifts.html",
        upcoming=upcoming,
        past=past,
        jobs=jobs,
        today=today,
        soon=today + timedelta(days=2),
        this_monday=this_monday,
        next_monday=this_monday + timedelta(days=7),
        active_workers=Employee.query.filter_by(is_active=True).count(),
    )


@admin_bp.route("/shifts", methods=["POST"])
def create_shift():
    """Create one shift, or the same shift across a date range.

    ``date`` is the first (or only) day. An optional ``end_date`` extends it
    to every day through that date, filtered by the ``weekdays`` checkboxes
    (0=Mon .. 6=Sun; none checked means every day). Duplicate job/date/time
    combinations are skipped rather than doubled.
    """
    job = db.session.get(Job, int(request.form["job_id"])) or abort(400)
    first_date = datetime.strptime(request.form["date"], "%Y-%m-%d").date()
    start_time = datetime.strptime(request.form["start_time"], "%H:%M").time()
    end_time = datetime.strptime(request.form["end_time"], "%H:%M").time()
    headcount = int(request.form.get("required_headcount") or job.default_headcount)

    end_raw = (request.form.get("end_date") or "").strip()
    last_date = (
        datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else first_date
    )
    if last_date < first_date:
        flash("The end date is before the start date.", "error")
        return redirect(url_for("admin.list_shifts"))
    if (last_date - first_date).days > 31:
        flash("Date range too large — pick 31 days or fewer at a time.", "error")
        return redirect(url_for("admin.list_shifts"))

    checked = request.form.getlist("weekdays")
    allowed_weekdays = {int(w) for w in checked} if checked else set(range(7))

    dates = [
        first_date + timedelta(days=i)
        for i in range((last_date - first_date).days + 1)
        if (first_date + timedelta(days=i)).weekday() in allowed_weekdays
    ]
    if not dates:
        flash("No days in that range match the selected weekdays.", "error")
        return redirect(url_for("admin.list_shifts"))

    created_shifts = []
    skipped = 0
    for d in dates:
        exists = Shift.query.filter_by(
            job_id=job.id, date=d, start_time=start_time, end_time=end_time
        ).first()
        if exists:
            skipped += 1
            continue
        shift = Shift(
            job_id=job.id,
            date=d,
            start_time=start_time,
            end_time=end_time,
            required_headcount=headcount,
            status=ShiftStatus.OPEN,
        )
        db.session.add(shift)
        created_shifts.append(shift)
    db.session.commit()

    if len(dates) == 1:
        if not created_shifts:
            existing = Shift.query.filter_by(
                job_id=job.id, date=dates[0], start_time=start_time, end_time=end_time
            ).first()
            flash("That shift already exists — showing it below.", "error")
            return redirect(url_for("admin.shift_detail", shift_id=existing.id))
        flash("Shift created.", "success")
        return redirect(url_for("admin.shift_detail", shift_id=created_shifts[0].id))

    flash(
        f"Created {len(created_shifts)} shift(s) from {first_date} to {last_date}"
        + (f" ({skipped} already existed)." if skipped else "."),
        "success" if created_shifts else "error",
    )
    return redirect(url_for("admin.list_shifts"))


@admin_bp.route("/shifts/<int:shift_id>")
def shift_detail(shift_id: int):
    shift = db.session.get(Shift, shift_id) or abort(404)
    return render_template("admin/shift_detail.html", shift=shift)


@admin_bp.route("/shifts/<int:shift_id>/delete", methods=["POST"])
def delete_shift(shift_id: int):
    shift = db.session.get(Shift, shift_id) or abort(404)
    db.session.delete(shift)
    db.session.commit()
    flash("Shift deleted.", "success")
    return redirect(url_for("admin.list_shifts"))


def _dispatch_one_shift(shift: Shift, service: WhatsAppService) -> tuple[int, int, int]:
    """Create pending assignments for all active employees and broadcast links.

    Idempotent: employees who already have an assignment for this shift are
    skipped, and only *pending* assignments are (re)broadcast.

    Returns:
        (created, sent, failed) counts.
    """
    existing_employee_ids = {a.employee_id for a in shift.assignments}
    active_employees = Employee.query.filter_by(is_active=True).all()

    created = 0
    for employee in active_employees:
        if employee.id in existing_employee_ids:
            continue
        db.session.add(
            ShiftAssignment(
                shift_id=shift.id,
                employee_id=employee.id,
                status=AssignmentStatus.pending,
                token=secrets.token_urlsafe(32),
            )
        )
        created += 1

    db.session.commit()

    # Broadcast acceptance links over WhatsApp after the tokens are persisted.
    pending = [a for a in shift.assignments if a.status == AssignmentStatus.pending]
    details = ShiftBroadcastDetails(
        title=shift.job.title,
        location_address=shift.job.location_address,
        work_date=shift.date,
        start_time=shift.start_time,
        end_time=shift.end_time,
        estimated_hours=shift.estimated_hours,
    )
    result = service.send_shift_broadcast(pending, details)
    return created, result.sent, result.failed


@admin_bp.route("/shifts/<int:shift_id>/dispatch", methods=["POST"])
def dispatch_shift(shift_id: int):
    """Dispatch a single shift's invitations to all active employees."""
    shift = db.session.get(Shift, shift_id) or abort(404)
    created, sent, failed = _dispatch_one_shift(shift, WhatsAppService())
    flash(
        f"Dispatched {created} new invitation(s); sent {sent}, failed {failed}.",
        "success",
    )
    return redirect(url_for("admin.shift_detail", shift_id=shift.id))


@admin_bp.route("/assignments/<int:assignment_id>/cancel", methods=["POST"])
def cancel_assignment(assignment_id: int):
    """Remove a worker from a shift (sick call, no-show, manager decision).

    Frees the seat: if the shift was full it reopens, and the understaffing
    alert is re-armed so the admin gets warned again if nobody takes the
    freed seat within the 24-48h window.
    """
    assignment = db.session.get(ShiftAssignment, assignment_id) or abort(404)
    shift = assignment.shift
    assignment.status = AssignmentStatus.cancelled
    db.session.flush()

    if shift.accepted_count < shift.required_headcount:
        if shift.status == ShiftStatus.FILLED:
            shift.status = ShiftStatus.OPEN
        shift.understaffed_alert_sent_at = None  # re-arm the alert

    db.session.commit()
    flash(f"{assignment.employee.full_name} removed from this shift.", "success")
    return redirect(url_for("admin.shift_detail", shift_id=shift.id))


@admin_bp.route("/shifts/copy-week", methods=["POST"])
def copy_week():
    """Copy every shift in a 7-day window to the following week.

    Weekly schedules mostly repeat — this recreates next week in one click
    (same jobs, times, headcounts; fresh OPEN status, no invitations yet).
    Skips any shift that already exists at the target date/time.
    """
    week_start = datetime.strptime(request.form["week_start"], "%Y-%m-%d").date()
    week_end = week_start + timedelta(days=6)

    source = (
        Shift.query.filter(Shift.date >= week_start, Shift.date <= week_end)
        .order_by(Shift.date, Shift.start_time)
        .all()
    )
    if not source:
        flash(f"No shifts between {week_start} and {week_end} to copy.", "error")
        return redirect(url_for("admin.list_shifts"))

    created = skipped = 0
    for s in source:
        target_date = s.date + timedelta(days=7)
        exists = Shift.query.filter_by(
            job_id=s.job_id,
            date=target_date,
            start_time=s.start_time,
            end_time=s.end_time,
        ).first()
        if exists:
            skipped += 1
            continue
        db.session.add(
            Shift(
                job_id=s.job_id,
                date=target_date,
                start_time=s.start_time,
                end_time=s.end_time,
                required_headcount=s.required_headcount,
                status=ShiftStatus.OPEN,
            )
        )
        created += 1

    db.session.commit()
    flash(
        f"Copied {created} shift(s) to the week of {week_start + timedelta(days=7)}"
        + (f" ({skipped} already existed)." if skipped else "."),
        "success",
    )
    return redirect(url_for("admin.list_shifts"))


@admin_bp.route("/shifts/dispatch-week", methods=["POST"])
def dispatch_week():
    """Dispatch every OPEN shift in a 7-day window in one click.

    This is the weekly flow: the admin builds next week's shifts, then sends
    the whole week to all workers at once. ``week_start`` is the first day of
    the window (typically Monday).
    """
    week_start = datetime.strptime(request.form["week_start"], "%Y-%m-%d").date()
    week_end = week_start + timedelta(days=6)
    # Never broadcast shifts whose date has already passed.
    effective_start = max(week_start, date.today())

    shifts = (
        Shift.query.filter(
            Shift.status == ShiftStatus.OPEN,
            Shift.date >= effective_start,
            Shift.date <= week_end,
        )
        .order_by(Shift.date, Shift.start_time)
        .all()
    )

    if not shifts:
        flash(f"No open shifts between {week_start} and {week_end}.", "error")
        return redirect(url_for("admin.list_shifts"))

    service = WhatsAppService()
    total_created = total_sent = total_failed = 0
    for shift in shifts:
        created, sent, failed = _dispatch_one_shift(shift, service)
        total_created += created
        total_sent += sent
        total_failed += failed

    flash(
        f"Week {week_start} – {week_end}: dispatched {len(shifts)} shift(s), "
        f"{total_created} new invitation(s); sent {total_sent}, "
        f"failed {total_failed}.",
        "success",
    )
    return redirect(url_for("admin.list_shifts"))
