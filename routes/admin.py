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
from utils.cli import normalize_e164
from utils.sms import ShiftBroadcastDetails, WhatsAppService
from utils.timeutil import is_valid_timezone, shift_window_utc, utc_naive_to_local

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
    return render_template(
        "admin/jobs.html",
        jobs=jobs,
        venue_timezones=VENUE_TIMEZONES,
        default_timezone=DEFAULT_VENUE_TIMEZONE,
    )


@admin_bp.route("/jobs", methods=["POST"])
def create_job():
    tz_name = _form_timezone(DEFAULT_VENUE_TIMEZONE)
    if tz_name is None:
        return redirect(url_for("admin.list_jobs"))
    job = Job(
        title=request.form["title"].strip(),
        location_address=request.form["location_address"].strip(),
        default_headcount=int(request.form.get("default_headcount", 1)),
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
    job.title = request.form["title"].strip()
    job.location_address = request.form["location_address"].strip()
    job.default_headcount = int(request.form.get("default_headcount", job.default_headcount))
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


@admin_bp.route("/shifts", methods=["POST"])
def create_shift():
    """Create the same shift on each specific date the admin picked.

    The calendar picker submits one ``dates`` value per selected day
    (``YYYY-MM-DD``); a single ``date`` field is also accepted. Duplicate
    job/date/time combinations are skipped rather than doubled.
    """
    job = db.session.get(Job, int(request.form["job_id"])) or abort(400)
    start_time = datetime.strptime(request.form["start_time"], "%H:%M").time()
    end_time = datetime.strptime(request.form["end_time"], "%H:%M").time()
    headcount = int(request.form.get("required_headcount") or job.default_headcount)

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
        f"Created {len(created_shifts)} shift(s) on {len(dates)} selected date(s)"
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
    if shift.status == ShiftStatus.DRAFT:
        flash("This shift is still a draft from a vendor email — approve it on the Inbox page first.", "error")
        return redirect(url_for("admin.shift_detail", shift_id=shift.id))
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
        f"Approved {len(drafts)} shift(s) from {email.vendor.name}. "
        "They'll go out with the next dispatch.",
        "success",
    )
    return redirect(url_for("admin.list_inbound"))


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
        "Date", "Job", "Venue timezone", "Worker", "Phone",
        "Scheduled start", "Scheduled end", "Check in (local)",
        "Check out (local)", "Hours worked", "Flags",
    ])
    now_utc = utcnow_naive()
    for a in rows:
        shift, job = a.shift, a.shift.job
        tz_name = job.timezone
        start_utc, end_utc = shift_window_utc(shift.date, shift.start_time, shift.end_time, tz_name)
        check_in = utc_naive_to_local(a.check_in_at_utc, tz_name)
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
            tz_name,
            a.employee.full_name,
            a.employee.phone_number,
            shift.start_time.strftime("%H:%M"),
            shift.end_time.strftime("%H:%M"),
            check_in.strftime("%Y-%m-%d %H:%M %Z") if check_in else "",
            check_out.strftime("%Y-%m-%d %H:%M %Z") if check_out else "",
            "" if a.worked_hours is None else f"{a.worked_hours:.2f}",
            "; ".join(flags),
        )])

    filename = f"timesheet_{start.isoformat()}_{end.isoformat()}.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
