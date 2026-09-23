"""Admin blueprint: CRUD for employees, jobs, and shifts, token dispatch,
vendor-email review, and timesheet export.

Every route is gated by the ``require_admin`` before_request hook below.
"""
import csv
import io
import secrets
from datetime import date, datetime, timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import (
    DEFAULT_VENUE_TIMEZONE,
    AssignmentStatus,
    Employee,
    InboundEmail,
    Job,
    Shift,
    ShiftAssignment,
    ShiftStatus,
    Vendor,
    utcnow_naive,
)
from utils.assignments import lock_assignment_shift, locked_accepted_count
from utils.i18n import SUPPORTED_LANGUAGES, normalize as normalize_language
from utils.punch import run_with_deadlock_retry
from utils.cli import normalize_e164
from utils.email_ingest import ingest_pasted_schedule
from utils.manual_punch import manual_punch as apply_manual_punch
from utils.pins import PinConfigError, set_default_pin, set_new_pin
from utils.sms import ShiftBroadcastDetails, WhatsAppService
from utils.timeutil import is_valid_timezone, shift_window_utc, utc_naive_to_local
from utils.web_punch import punch_url

# Venue timezones offered in the Jobs form (any valid IANA name is accepted).
VENUE_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
]

# A check-in this long after scheduled start is flagged "late" on timesheets.
LATE_GRACE = timedelta(minutes=10)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# Upper bound on dates picked in one "Create shifts" submit (about two months).
MAX_DATES_PER_BATCH = 62


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
    today = date.today()
    return render_template(
        "admin/employees.html",
        employees=employees,
        timesheet_start=today - timedelta(days=today.weekday() + 7),
        timesheet_end=today - timedelta(days=today.weekday() + 1),
    )


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

    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    if not (1 <= len(first_name) <= 80 and 1 <= len(last_name) <= 80):
        flash("Enter a first and last name, each up to 80 characters.", "error")
        return redirect(url_for("admin.list_employees"))
    employee = Employee(
        first_name=first_name,
        last_name=last_name,
        phone_number=phone,
        is_active=True,
        language=normalize_language(request.form.get("language")) or "en",
    )
    db.session.add(employee)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("That phone number already exists.", "error")
        return redirect(url_for("admin.list_employees"))

    # Pilot default: PIN = last 4 digits of their phone, so there's nothing to hand out.
    _issue_pin_and_flash(employee, "Employee added.", use_phone_default=True)
    return redirect(url_for("admin.list_employees"))


def _issue_pin_and_flash(employee: Employee, prefix: str, *, use_phone_default: bool = False) -> None:
    """Set a check-in PIN and show it to the admin once.

    New workers get the phone-number default; "Reset PIN" issues a random one.
    """
    try:
        pin = set_default_pin(employee) if use_phone_default else set_new_pin(employee)
        db.session.commit()
    except PinConfigError:
        db.session.rollback()
        flash(f"{prefix} No check-in PIN was set: PIN_PEPPER isn't configured on the server.", "error")
        return
    if use_phone_default:
        flash(
            f"{prefix} {employee.full_name}'s check-in PIN is {pin} "
            "(the last 4 digits of their phone). Use Reset PIN for a private random one.",
            "success",
        )
    else:
        flash(
            f"{prefix} {employee.full_name}'s check-in PIN is {pin}. Tell them privately — "
            "it won't be shown again.",
            "success",
        )


@admin_bp.route("/employees/<int:employee_id>/language", methods=["POST"])
def set_employee_language(employee_id: int):
    """Change which language a worker's pages and text messages use."""
    employee = db.session.get(Employee, employee_id) or abort(404)
    language = normalize_language(request.form.get("language"))
    if language is None:
        flash("Pick English or Spanish.", "error")
        return redirect(url_for("admin.list_employees"))
    employee.language = language
    db.session.commit()
    flash(
        f"{employee.full_name} will now get pages and texts in "
        f"{SUPPORTED_LANGUAGES[language]}.",
        "success",
    )
    return redirect(url_for("admin.list_employees"))


@admin_bp.route("/employees/<int:employee_id>/reset-pin", methods=["POST"])
def reset_employee_pin(employee_id: int):
    employee = db.session.get(Employee, employee_id) or abort(404)
    _issue_pin_and_flash(employee, "PIN reset.")
    return redirect(url_for("admin.list_employees"))


@admin_bp.route("/assignments/<int:assignment_id>/punch-link", methods=["POST"])
def issue_punch_link(assignment_id: int):
    """Create (if needed) and show the worker's keypad link for one shift."""
    assignment = db.session.get(ShiftAssignment, assignment_id) or abort(404)
    if assignment.status != AssignmentStatus.accepted:
        flash("Punch links are only for confirmed workers.", "error")
        return redirect(url_for("admin.shift_detail", shift_id=assignment.shift_id))
    url = punch_url(current_app.config["PUBLIC_BASE_URL"], assignment)
    db.session.commit()
    flash(f"Check-in link for {assignment.employee.full_name}: {url}", "success")
    return redirect(url_for("admin.shift_detail", shift_id=assignment.shift_id))


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
    return render_template(
        "admin/jobs.html",
        jobs=jobs,
        venue_timezones=VENUE_TIMEZONES,
        default_timezone=DEFAULT_VENUE_TIMEZONE,
    )


def _job_fields(default_headcount=1):
    title = request.form.get("title", "").strip()
    address = request.form.get("location_address", "").strip()
    count = int(request.form.get("default_headcount") or default_headcount)
    if not (1 <= len(title) <= 160 and 1 <= len(address) <= 255 and 1 <= count <= 500):
        raise ValueError("Invalid job fields")
    return title, address, count


@admin_bp.route("/jobs", methods=["POST"])
def create_job():
    tz_name = _form_timezone(DEFAULT_VENUE_TIMEZONE)
    if tz_name is None:
        return redirect(url_for("admin.list_jobs"))
    try:
        title, address, headcount = _job_fields()
    except ValueError:
        flash("Enter a job title, address, and a headcount from 1 to 500.", "error")
        return redirect(url_for("admin.list_jobs"))
    job = Job(
        title=title,
        location_address=address,
        default_headcount=headcount,
        timezone=tz_name,
    )
    db.session.add(job)
    db.session.commit()
    flash("Job created.", "success")
    return redirect(url_for("admin.list_jobs"))


def _form_timezone(fallback: str) -> str | None:
    """Validated IANA timezone from the form; flashes and returns None if bad."""
    tz_name = (request.form.get("timezone") or fallback).strip()
    if not is_valid_timezone(tz_name):
        flash(f"'{tz_name}' isn't a valid timezone (e.g. America/Denver).", "error")
        return None
    return tz_name


@admin_bp.route("/jobs/<int:job_id>/update", methods=["POST"])
def update_job(job_id: int):
    job = db.session.get(Job, job_id) or abort(404)
    tz_name = _form_timezone(job.timezone)
    if tz_name is None:
        return redirect(url_for("admin.list_jobs"))
    try:
        title, address, headcount = _job_fields(job.default_headcount)
    except ValueError:
        flash("Enter a job title, address, and a headcount from 1 to 500.", "error")
        return redirect(url_for("admin.list_jobs"))
    job.title, job.location_address, job.default_headcount = title, address, headcount
    job.timezone = tz_name
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
        max_dates=MAX_DATES_PER_BATCH,
    )


def _optional_time(raw: str | None):
    """"HH:MM" -> time; blank -> None (no end given)."""
    raw = (raw or "").strip()
    return datetime.strptime(raw, "%H:%M").time() if raw else None


def _optional_area(raw: str | None) -> str | None:
    area = " ".join((raw or "").split())[:80]
    return area or None


@admin_bp.route("/shifts", methods=["POST"])
def create_shift():
    """Create the same shift on each specific date the admin picked.

    The calendar picker submits one ``dates`` value per selected day
    (``YYYY-MM-DD``); a single ``date`` field is also accepted. Duplicate
    job/date/time combinations are skipped rather than doubled.
    """
    try:
        job = db.session.get(Job, int(request.form.get("job_id", "")))
        if job is None:
            raise ValueError("Unknown job")
        start_time = datetime.strptime(request.form.get("start_time", ""), "%H:%M").time()
        end_time = _optional_time(request.form.get("end_time"))
        headcount = int(request.form.get("required_headcount") or job.default_headcount)
        if not 1 <= headcount <= 500:
            raise ValueError("Invalid headcount")
    except ValueError:
        flash("Choose a job, valid times, and a headcount from 1 to 500.", "error")
        return redirect(url_for("admin.list_shifts"))
    area = _optional_area(request.form.get("area"))
    if end_time is not None and end_time == start_time:
        flash("End time can't be the same as the start time.", "error")
        return redirect(url_for("admin.list_shifts"))

    raw_dates = request.form.getlist("dates") or [request.form.get("date", "")]
    try:
        dates = sorted(
            {datetime.strptime(d.strip(), "%Y-%m-%d").date() for d in raw_dates if d.strip()}
        )
    except ValueError:
        flash("One of the selected dates isn't valid — please pick again.", "error")
        return redirect(url_for("admin.list_shifts"))
    if not dates:
        flash("Pick at least one date on the calendar.", "error")
        return redirect(url_for("admin.list_shifts"))
    if len(dates) > MAX_DATES_PER_BATCH:
        flash(
            f"That's {len(dates)} dates — pick {MAX_DATES_PER_BATCH} or fewer at a time.",
            "error",
        )
        return redirect(url_for("admin.list_shifts"))

    created_shifts = []
    skipped = 0
    for d in dates:
        exists = Shift.query.filter_by(
            job_id=job.id, date=d, start_time=start_time, end_time=end_time, area=area
        ).first()
        if exists:
            skipped += 1
            continue
        shift = Shift(
            job_id=job.id,
            date=d,
            start_time=start_time,
            end_time=end_time,
            area=area,
            required_headcount=headcount,
            status=ShiftStatus.OPEN,
        )
        db.session.add(shift)
        created_shifts.append(shift)
    db.session.commit()

    if len(dates) == 1:
        if not created_shifts:
            existing = Shift.query.filter_by(
                job_id=job.id, date=dates[0], start_time=start_time, end_time=end_time, area=area
            ).first()
            flash("That shift already exists — showing it below.", "error")
            return redirect(url_for("admin.shift_detail", shift_id=existing.id))
        flash("Shift created.", "success")
        return redirect(url_for("admin.shift_detail", shift_id=created_shifts[0].id))

    flash(
        f"Created {len(created_shifts)} shift(s) on {len(dates)} selected date(s)"
        + (f" ({skipped} already existed)." if skipped else "."),
        "success" if created_shifts else "error",
    )
    return redirect(url_for("admin.list_shifts"))


@admin_bp.route("/shifts/<int:shift_id>")
def shift_detail(shift_id: int):
    shift = db.session.get(Shift, shift_id) or abort(404)
    # Default time for the Manual punch form, in venue local time: "now" on
    # shift day, otherwise the scheduled start (the likely thing to stamp).
    tz = shift.job.timezone
    now_local = utc_naive_to_local(utcnow_naive(), tz).replace(tzinfo=None)
    start_local = datetime.combine(shift.date, shift.start_time)
    default_local = now_local if abs(now_local - start_local) <= timedelta(hours=24) else start_local
    return render_template(
        "admin/shift_detail.html",
        shift=shift,
        manual_punch_default=default_local.strftime("%Y-%m-%dT%H:%M"),
    )


@admin_bp.route("/shift/<int:shift_id>/manual-punch/<int:worker_id>", methods=["POST"])
def manual_punch(shift_id: int, worker_id: int):
    """Manager override (dead phone): stamp a check-in or check-out.

    Admin-only via the blueprint's require_admin hook and CSRF-protected like
    every admin form. Form: action=in|out, punch_time=YYYY-MM-DDTHH:MM (venue
    local), note (optional reason).
    """
    shift = db.session.get(Shift, shift_id) or abort(404)
    db.session.get(Employee, worker_id) or abort(404)
    try:
        local_time = datetime.strptime(request.form.get("punch_time", ""), "%Y-%m-%dT%H:%M")
    except ValueError:
        flash("Enter the date and time for the manual punch.", "error")
        return redirect(url_for("admin.shift_detail", shift_id=shift.id))

    result = apply_manual_punch(
        shift_id=shift.id,
        employee_id=worker_id,
        action=request.form.get("action", ""),
        local_time=local_time,
        note=request.form.get("note", ""),
    )
    flash(result.message, "success" if result.ok else "error")
    return redirect(url_for("admin.shift_detail", shift_id=shift.id))


@admin_bp.route("/shifts/<int:shift_id>/delete", methods=["POST"])
def delete_shift(shift_id: int):
    shift = db.session.get(Shift, shift_id) or abort(404)
    db.session.delete(shift)
    db.session.commit()
    flash("Shift deleted.", "success")
    return redirect(url_for("admin.list_shifts"))


def _dispatch_one_shift(
    shift: Shift, service: WhatsAppService
) -> tuple[int, int, int, tuple[str, ...]]:
    """Create pending assignments for all active employees and broadcast links.

    Idempotent: employees who already have an assignment for this shift are
    skipped, and only *pending* assignments are (re)broadcast.

    Returns:
        (created, sent, failed, failed_names).
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
                # 128 bits: unguessable, and 21 characters shorter than
                # token_urlsafe(32), which keeps the SMS at two segments.
                token=secrets.token_urlsafe(16),
            )
        )
        created += 1

    db.session.commit()

    # Broadcast acceptance links over WhatsApp after the tokens are persisted.
    pending = [a for a in shift.assignments if a.status == AssignmentStatus.pending and a.employee.is_active]
    details = ShiftBroadcastDetails(
        title=shift.job.title,
        location_address=shift.job.location_address,
        work_date=shift.date,
        start_time=shift.start_time,
        end_time=shift.end_time,
        estimated_hours=shift.estimated_hours,
        area=shift.area,
    )
    result = service.send_shift_broadcast(pending, details)
    return created, result.sent, result.failed, result.failed_names


def _flash_dispatch_result(sent: int, failed: int, failed_names=()) -> None:
    """Report a broadcast honestly.

    A partial or total send failure is a red banner naming the workers who
    were NOT texted, because the console otherwise shows them sitting in
    "waiting to reply" and the manager waits for answers that can never come.
    """
    if failed and not sent:
        names = ", ".join(failed_names)
        flash(
            f"NOBODY WAS TEXTED. All {failed} message(s) failed to send"
            + (f": {names}." if names else ".")
            + " Nobody has been invited \u2014 contact them another way and check the"
            " messaging setup before relying on this shift.",
            "error",
        )
    elif failed:
        names = ", ".join(failed_names)
        flash(
            f"Sent {sent}, but {failed} message(s) FAILED"
            + (f": {names}." if names else ".")
            + " Those workers were not texted \u2014 contact them another way.",
            "error",
        )
    elif sent:
        flash(f"Texted {sent} worker(s).", "success")
    else:
        flash("No new workers to invite \u2014 everyone has already been asked.", "info")


@admin_bp.route("/shifts/<int:shift_id>/dispatch", methods=["POST"])
def dispatch_shift(shift_id: int):
    """Dispatch a single shift's invitations to all active employees."""
    shift = db.session.get(Shift, shift_id) or abort(404)
    if shift.status == ShiftStatus.DRAFT:
        flash("This shift is still a draft from a vendor email — approve it on the Inbox page first.", "error")
        return redirect(url_for("admin.shift_detail", shift_id=shift.id))
    if shift.status != ShiftStatus.OPEN:
        flash("Only open shifts can be dispatched.", "error")
        return redirect(url_for("admin.shift_detail", shift_id=shift.id))
    created, sent, failed, failed_names = _dispatch_one_shift(shift, WhatsAppService())
    _flash_dispatch_result(sent, failed, failed_names)
    return redirect(url_for("admin.shift_detail", shift_id=shift.id))


@admin_bp.route("/assignments/<int:assignment_id>/cancel", methods=["POST"])
def cancel_assignment(assignment_id: int):
    """Remove a worker from a shift (sick call, no-show, manager decision).

    Frees the seat: if the shift was full it reopens, and the understaffing
    alert is re-armed so the admin gets warned again if nobody takes the
    freed seat within the 24-48h window.
    """
    initial = db.session.get(ShiftAssignment, assignment_id) or abort(404)
    shift_id = initial.shift_id

    def attempt():
        assignment, shift = lock_assignment_shift(assignment_id, shift_id)
        assignment.status = AssignmentStatus.cancelled
        db.session.flush()
        if locked_accepted_count(shift_id) < shift.required_headcount:
            if shift.status == ShiftStatus.FILLED:
                shift.status = ShiftStatus.OPEN
            shift.understaffed_alert_sent_at = None
        name = assignment.employee.full_name
        db.session.commit()
        return name

    name = run_with_deadlock_retry(attempt, label=f"cancel assignment={assignment_id}")
    flash(f"{name} removed from this shift.", "success")
    return redirect(url_for("admin.shift_detail", shift_id=shift_id))


@admin_bp.route("/shifts/copy-week", methods=["POST"])
def copy_week():
    """Copy every shift in a 7-day window to the following week.

    Weekly schedules mostly repeat — this recreates next week in one click
    (same jobs, times, headcounts; fresh OPEN status, no invitations yet).
    Skips any shift that already exists at the target date/time.
    """
    try:
        week_start = datetime.strptime(request.form.get("week_start", ""), "%Y-%m-%d").date()
    except ValueError:
        flash("Choose a valid week start date.", "error")
        return redirect(url_for("admin.list_shifts"))
    week_end = week_start + timedelta(days=6)

    source = (
        Shift.query.filter(
            Shift.date >= week_start,
            Shift.date <= week_end,
            Shift.status.in_([ShiftStatus.OPEN, ShiftStatus.FILLED]),
        )
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
            area=s.area,
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
                area=s.area,
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
    try:
        week_start = datetime.strptime(request.form.get("week_start", ""), "%Y-%m-%d").date()
    except ValueError:
        flash("Choose a valid week start date.", "error")
        return redirect(url_for("admin.list_shifts"))
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
    all_failed_names: list[str] = []
    for shift in shifts:
        created, sent, failed, failed_names = _dispatch_one_shift(shift, service)
        total_created += created
        total_sent += sent
        total_failed += failed
        all_failed_names.extend(failed_names)

    flash(
        f"Week {week_start} – {week_end}: {len(shifts)} shift(s) dispatched.",
        "info",
    )
    _flash_dispatch_result(total_sent, total_failed, tuple(dict.fromkeys(all_failed_names)))
    return redirect(url_for("admin.list_shifts"))


# --------------------------------------------------------------------------- #
# Inbox: vendors + parsed vendor emails awaiting approval
# --------------------------------------------------------------------------- #
@admin_bp.route("/inbound")
def list_inbound():
    emails = (
        InboundEmail.query.order_by(InboundEmail.received_at_utc.desc()).limit(50).all()
    )
    vendors = Vendor.query.order_by(Vendor.name).all()
    base = current_app.config["PUBLIC_BASE_URL"].rstrip("/")
    return render_template(
        "admin/inbound.html",
        emails=emails,
        vendors=vendors,
        webhook_base=f"{base}/webhooks/email/",
        sms_webhook_url=f"{base}/webhooks/twilio/sms",
        draft_count=Shift.query.filter_by(status=ShiftStatus.DRAFT).count(),
        jobs=Job.query.order_by(Job.title).all(),
        open_id=request.args.get("open", type=int),
        today=date.today(),
    )


@admin_bp.route("/vendors", methods=["POST"])
def create_vendor():
    name = request.form.get("name", "").strip()
    domain = request.form.get("allowed_sender_domain", "").strip().lower().lstrip("@")
    if not name or "." not in domain or " " in domain:
        flash("Vendor needs a name and an email domain like levyrestaurants.com.", "error")
        return redirect(url_for("admin.list_inbound"))
    db.session.add(
        Vendor(name=name, allowed_sender_domain=domain, inbound_token=secrets.token_urlsafe(32))
    )
    db.session.commit()
    flash(f"Vendor {name} added. Point SendGrid at its webhook URL below.", "success")
    return redirect(url_for("admin.list_inbound"))


@admin_bp.route("/vendors/<int:vendor_id>/toggle", methods=["POST"])
def toggle_vendor(vendor_id: int):
    vendor = db.session.get(Vendor, vendor_id) or abort(404)
    vendor.is_active = not vendor.is_active
    db.session.commit()
    flash(f"{vendor.name} {'enabled' if vendor.is_active else 'disabled'}.", "success")
    return redirect(url_for("admin.list_inbound"))


@admin_bp.route("/inbound/<int:email_id>/approve", methods=["POST"])
def approve_inbound(email_id: int):
    """Publish an email's draft shifts so they can be dispatched."""
    email = db.session.get(InboundEmail, email_id) or abort(404)
    drafts = email.draft_shifts
    for shift in drafts:
        shift.status = ShiftStatus.OPEN
    db.session.commit()
    flash(
        f"Approved {len(drafts)} shift(s) from {email.sender_label}. "
        "They'll go out with the next dispatch.",
        "success",
    )
    return redirect(url_for("admin.list_inbound"))


@admin_bp.route("/inbound/paste", methods=["POST"])
def paste_schedule():
    """Admin pastes a vendor's schedule text; it becomes DRAFT shifts for review."""
    text = (request.form.get("schedule_text") or "").strip()
    job = db.session.get(Job, request.form.get("job_id", default=0, type=int))
    if not text:
        flash("Paste the schedule text first.", "error")
        return redirect(url_for("admin.list_inbound"))
    if job is None:
        flash("Pick which job these shifts are for.", "error")
        return redirect(url_for("admin.list_inbound"))
    if len(text) > 50_000:
        flash("That's too much text — paste just the schedule part of the email.", "error")
        return redirect(url_for("admin.list_inbound"))

    sent_on = None
    if request.form.get("sent_on"):
        try:
            sent_on = datetime.strptime(request.form["sent_on"], "%Y-%m-%d").date()
        except ValueError:
            flash("The 'email sent on' date isn't valid.", "error")
            return redirect(url_for("admin.list_inbound"))

    record = ingest_pasted_schedule(text, job=job, sent_on=sent_on)
    problems = len(record.errors)
    flash(
        f"Read {record.shifts_created} shift(s) as drafts"
        + (f" — {problems} line(s) need attention." if problems else ". Review them below, then approve."),
        "success" if record.shifts_created else "error",
    )
    return redirect(url_for("admin.list_inbound", open=record.id))


@admin_bp.route("/shifts/<int:shift_id>/edit-draft", methods=["POST"])
def edit_draft_shift(shift_id: int):
    """Fix a draft before approving it: date, times, area, number of people."""
    shift = db.session.get(Shift, shift_id) or abort(404)
    back = url_for("admin.list_inbound", open=shift.inbound_email_id)
    if shift.status != ShiftStatus.DRAFT:
        flash("Only draft shifts can be edited here.", "error")
        return redirect(back)
    try:
        shift.date = datetime.strptime(request.form["date"], "%Y-%m-%d").date()
        shift.start_time = datetime.strptime(request.form["start_time"], "%H:%M").time()
        shift.end_time = _optional_time(request.form.get("end_time"))
        shift.area = _optional_area(request.form.get("area"))
        headcount = int(request.form["required_headcount"])
    except (KeyError, ValueError):
        db.session.rollback()
        flash("Check the date, times and number of people.", "error")
        return redirect(back)
    if not 1 <= headcount <= 500 or (shift.end_time is not None and shift.end_time == shift.start_time):
        db.session.rollback()
        flash("Number of people must be 1–500 and end can't equal start.", "error")
        return redirect(back)
    shift.required_headcount = headcount
    db.session.commit()
    flash(f"Draft updated: {shift.date:%a %d %b} {shift.time_label}.", "success")
    return redirect(back)


@admin_bp.route("/inbound/<int:email_id>/discard", methods=["POST"])
def discard_inbound(email_id: int):
    email = db.session.get(InboundEmail, email_id) or abort(404)
    drafts = email.draft_shifts
    for shift in drafts:
        db.session.delete(shift)
    db.session.commit()
    flash(f"Discarded {len(drafts)} draft shift(s).", "success")
    return redirect(url_for("admin.list_inbound"))


# --------------------------------------------------------------------------- #
# Timesheets (SMS punches, exported in venue-local time)
# --------------------------------------------------------------------------- #
def _csv_safe(value) -> str:
    """Neutralize spreadsheet formula injection in exported cells."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "@", "\t", "\r") or (
        text[:1] in ("+", "-") and not text[1:].replace(".", "").isdigit()
    ):
        return "'" + text
    return text


@admin_bp.route("/timesheets.csv")
def export_timesheets():
    try:
        start = datetime.strptime(request.args["start"], "%Y-%m-%d").date()
        end = datetime.strptime(request.args["end"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        flash("Pick a valid start and end date for the timesheet.", "error")
        return redirect(url_for("admin.list_employees"))
    if end < start or (end - start).days > 93:
        flash("Timesheet range must be 1–93 days, start before end.", "error")
        return redirect(url_for("admin.list_employees"))

    rows = (
        db.session.query(ShiftAssignment)
        .join(Shift, Shift.id == ShiftAssignment.shift_id)
        .filter(
            Shift.date >= start,
            Shift.date <= end,
            or_(
                ShiftAssignment.status == AssignmentStatus.accepted,
                ShiftAssignment.check_in_at_utc.is_not(None),
            ),
        )
        .order_by(Shift.date, Shift.start_time)
        .all()
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "Date", "Job", "Area", "Venue timezone", "Worker", "Phone",
        "Scheduled start", "Scheduled end", "Check in (local)", "Actual tap (local)",
        "Check out (local)", "Hours worked", "In via", "Out via", "Manager note", "Flags",
    ])
    now_utc = utcnow_naive()
    for a in rows:
        shift, job = a.shift, a.shift.job
        tz_name = job.timezone
        start_utc, end_utc = shift_window_utc(shift.date, shift.start_time, shift.end_time, tz_name)
        check_in = utc_naive_to_local(a.check_in_at_utc, tz_name)
        # Only shown when the grace period moved the recorded (payroll) time.
        actual_tap = utc_naive_to_local(a.check_in_actual_at_utc, tz_name) if a.check_in_was_snapped else None
        check_out = utc_naive_to_local(a.check_out_at_utc, tz_name)

        flags = []
        if a.check_in_at_utc is None and now_utc > end_utc:
            flags.append("missing IN")
        if a.check_in_at_utc is not None and a.check_out_at_utc is None and now_utc > end_utc:
            flags.append("missing OUT")
        if a.check_in_at_utc is not None and a.check_in_at_utc > start_utc + LATE_GRACE:
            flags.append("late IN")
        if a.status != AssignmentStatus.accepted:
            flags.append(f"assignment {a.status.value}")

        writer.writerow([_csv_safe(v) for v in (
            shift.date.isoformat(),
            job.title,
            shift.area or "",
            tz_name,
            a.employee.full_name,
            a.employee.phone_number,
            shift.start_time.strftime("%H:%M"),
            shift.end_time.strftime("%H:%M") if shift.end_time else "",  # blank = until done
            check_in.strftime("%Y-%m-%d %H:%M %Z") if check_in else "",
            actual_tap.strftime("%Y-%m-%d %H:%M:%S %Z") if actual_tap else "",
            check_out.strftime("%Y-%m-%d %H:%M %Z") if check_out else "",
            "" if a.worked_hours is None else f"{a.worked_hours:.2f}",
            a.check_in_source or "",
            a.check_out_source or "",
            a.punch_note or "",
            "; ".join(flags),
        )])

    filename = f"timesheet_{start.isoformat()}_{end.isoformat()}.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
