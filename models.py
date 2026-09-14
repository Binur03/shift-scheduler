"""SQLAlchemy models for the shift fulfillment system (MySQL / InnoDB).

Uses SQLAlchemy 2.0 typed mappings (``Mapped`` / ``mapped_column``) for strict
type hinting. Design highlights:

* InnoDB engine (MySQL default) so foreign keys and ``SELECT ... FOR UPDATE``
  row locking are available.
* Foreign-key columns are explicitly indexed; additional composite indexes
  back the hot query paths (per-job/date listing, per-shift accepted counts).
* ``ShiftAssignment.token`` is unique and indexed for O(1) public lookups.
* Assignment status is a native enum constrained to the four legal states.

Time conventions
----------------
* ``Shift.date`` / ``start_time`` / ``end_time`` are naive **venue-local**
  wall-clock values (that is how schedules are written and read).
* Every column ending in ``_utc`` is a naive **UTC** timestamp (MySQL
  DATETIME has no zone). Convert with ``utils.timeutil`` for display/export.
"""
from __future__ import annotations

import enum
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from extensions import db


class AssignmentStatus(enum.Enum):
    """Lifecycle states for a worker's shift assignment."""

    pending = "pending"
    accepted = "accepted"
    cancelled = "cancelled"
    rejected = "rejected"


class ShiftStatus:
    """String constants for ``Shift.status`` (kept as a loose string column)."""

    DRAFT = "draft"  # parsed from a vendor email; awaiting admin approval
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"


class ShiftSource:
    """Where a shift came from."""

    MANUAL = "manual"
    EMAIL = "email"


class PunchResult:
    """Outcome recorded for every inbound SMS (``InboundSms.result``)."""

    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"
    DUPLICATE = "duplicate"  # IN while already in / OUT while already out
    NO_SHIFT = "no_shift"  # no eligible assignment for this punch
    UNKNOWN_SENDER = "unknown_sender"
    MALFORMED = "malformed"
    OPT_KEYWORD = "opt_keyword"  # STOP/HELP/START — handled by the carrier


class ParseStatus:
    """Outcome of parsing one inbound vendor email."""

    PARSED = "parsed"  # every shift line was read
    PARTIAL = "partial"  # some lines read, some rejected
    FAILED = "failed"  # nothing usable
    REJECTED = "rejected"  # failed sender authentication (DKIM)


# Venue timezone used when a job doesn't specify one.
DEFAULT_VENUE_TIMEZONE = "America/Denver"


def utcnow_naive() -> datetime:
    """Current UTC time as a naive datetime (the storage convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _duration_hours(start: time, end: time) -> float:
    """Return the length of a [start, end) window in hours.

    Handles overnight shifts where ``end`` is chronologically earlier than
    ``start`` (e.g. 22:00 -> 06:00) by rolling the end time over midnight.
    """
    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    if end_minutes <= start_minutes:
        end_minutes += 24 * 60
    return (end_minutes - start_minutes) / 60.0


class Employee(db.Model):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    phone_number: Mapped[str] = mapped_column(
        String(20), nullable=False, unique=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    assignments: Mapped[list["ShiftAssignment"]] = relationship(
        back_populates="employee",
        cascade="all, delete-orphan",
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def accepted_hours_for_week(self, iso_week: str) -> float:
        """Total accepted shift hours for the ISO week ``YYYY-Www``.

        Used for overtime checks before broadcasting additional shifts. The
        query is bounded to the week's date range in SQL (cheap, index-friendly
        on ``shifts.date``) and only the duration arithmetic happens in Python.

        Args:
            iso_week: ISO-8601 week string, e.g. ``"2026-W27"``.

        Returns:
            Accepted hours for that week, rounded to two decimals.

        Raises:
            ValueError: if ``iso_week`` is not a valid ``YYYY-Www`` string.
        """
        try:
            year_part, week_part = iso_week.split("-W")
            year = int(year_part)
            week = int(week_part)
            week_start = date.fromisocalendar(year, week, 1)  # Monday
            week_end = date.fromisocalendar(year, week, 7)  # Sunday
        except (ValueError, IndexError) as exc:
            raise ValueError(
                f"Invalid ISO week string {iso_week!r}; expected 'YYYY-Www'."
            ) from exc

        rows = (
            db.session.query(Shift.start_time, Shift.end_time)
            .join(ShiftAssignment, ShiftAssignment.shift_id == Shift.id)
            .filter(
                ShiftAssignment.employee_id == self.id,
                ShiftAssignment.status == AssignmentStatus.accepted,
                Shift.date >= week_start,
                Shift.date <= week_end,
            )
            .all()
        )

        total = sum(_duration_hours(start, end) for start, end in rows)
        return round(total, 2)

    def __repr__(self) -> str:
        return f"<Employee {self.id} {self.full_name}>"


class Job(db.Model):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    location_address: Mapped[str] = mapped_column(String(255), nullable=False)
    default_headcount: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # IANA zone of the venue, e.g. "America/Denver". Shift times for this job
    # are interpreted in this zone; punch timestamps are exported in it.
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=DEFAULT_VENUE_TIMEZONE,
        server_default=DEFAULT_VENUE_TIMEZONE,
    )

    shifts: Mapped[list["Shift"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.title}>"


class Vendor(db.Model):
    """A company that sends shift requests by email (e.g. a concessionaire)."""

    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # Random secret embedded in this vendor's inbound-email webhook URL.
    inbound_token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    # Email domain whose DKIM signature must pass, e.g. "levyrestaurants.com".
    allowed_sender_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow_naive, server_default=func.now()
    )

    shifts: Mapped[list["Shift"]] = relationship(back_populates="vendor")
    inbound_emails: Mapped[list["InboundEmail"]] = relationship(
        back_populates="vendor", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Vendor {self.id} {self.name}>"


class Shift(db.Model):
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    required_headcount: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ShiftStatus.OPEN,
        server_default=ShiftStatus.OPEN,
    )
    # Set when the 24-48h understaffing alert has been sent to the admin,
    # so each shift only triggers one alert.
    understaffed_alert_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None
    )
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ShiftSource.MANUAL,
        server_default=ShiftSource.MANUAL,
    )
    vendor_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True, index=True
    )
    inbound_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("inbound_emails.id", ondelete="SET NULL"), nullable=True, index=True
    )

    job: Mapped["Job"] = relationship(back_populates="shifts")
    vendor: Mapped["Vendor | None"] = relationship(back_populates="shifts")
    inbound_email: Mapped["InboundEmail | None"] = relationship(back_populates="shifts")
    assignments: Mapped[list["ShiftAssignment"]] = relationship(
        back_populates="shift",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # Common admin query: shifts for a job on a given date.
        Index("ix_shifts_job_date", "job_id", "date"),
    )

    @property
    def estimated_hours(self) -> float:
        return round(_duration_hours(self.start_time, self.end_time), 2)

    @property
    def accepted_count(self) -> int:
        return sum(
            1 for a in self.assignments if a.status == AssignmentStatus.accepted
        )

    @property
    def pending_count(self) -> int:
        return sum(
            1 for a in self.assignments if a.status == AssignmentStatus.pending
        )

    @property
    def declined_count(self) -> int:
        return sum(
            1 for a in self.assignments if a.status == AssignmentStatus.rejected
        )

    @property
    def is_full(self) -> bool:
        return self.accepted_count >= self.required_headcount

    def __repr__(self) -> str:
        return f"<Shift {self.id} job={self.job_id} {self.date}>"


class ShiftAssignment(db.Model):
    __tablename__ = "shift_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[AssignmentStatus] = mapped_column(
        Enum(AssignmentStatus, native_enum=True, validate_strings=True),
        nullable=False,
        default=AssignmentStatus.pending,
        server_default=AssignmentStatus.pending.value,
    )
    token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        # Stored naive-UTC (column is not timezone-aware).
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        server_default=func.now(),
    )
    # Set when the day-before reminder has been sent to this (accepted) worker.
    reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None
    )
    # SMS time punches (naive UTC). Written only under a row lock — see
    # utils/punch.py.
    check_in_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    check_out_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    check_in_message_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    check_out_message_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)

    shift: Mapped["Shift"] = relationship(back_populates="assignments")
    employee: Mapped["Employee"] = relationship(back_populates="assignments")

    __table_args__ = (
        # An employee should only ever be offered a given shift once.
        UniqueConstraint("shift_id", "employee_id", name="uq_shift_employee"),
        # Backs the per-shift accepted-count query inside the concurrency guard.
        Index("ix_assignments_shift_status", "shift_id", "status"),
        # Backs the "open punch for this worker" lookup on OUT.
        Index("ix_assignments_employee_punch", "employee_id", "check_out_at_utc"),
    )

    @property
    def worked_hours(self) -> float | None:
        """Hours between check-in and check-out (UTC math, so DST-safe)."""
        if self.check_in_at_utc is None or self.check_out_at_utc is None:
            return None
        seconds = (self.check_out_at_utc - self.check_in_at_utc).total_seconds()
        return round(max(seconds, 0) / 3600, 2)

    def __repr__(self) -> str:
        return (
            f"<ShiftAssignment {self.id} shift={self.shift_id} "
            f"{self.status.value}>"
        )


class InboundSms(db.Model):
    """Append-only log of every inbound SMS webhook (valid or not).

    ``message_sid`` is unique: Twilio retries a webhook on timeout, and the
    duplicate insert is how a retry is detected and answered with the stored
    reply instead of being processed twice.
    """

    __tablename__ = "inbound_sms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_sid: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    from_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    body: Mapped[str] = mapped_column(String(1600), nullable=False, default="")
    received_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow_naive
    )
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("shift_assignments.id", ondelete="SET NULL"), nullable=True
    )
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    reply_body: Mapped[str | None] = mapped_column(String(480), nullable=True)

    def __repr__(self) -> str:
        return f"<InboundSms {self.message_sid} {self.result}>"


class InboundEmail(db.Model):
    """A vendor shift email as received, plus how parsing went.

    The raw body is persisted *before* parsing so a parser fix can re-run
    historical emails; ``message_id`` makes redelivery a no-op.
    """

    __tablename__ = "inbound_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    from_address: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    subject: Mapped[str] = mapped_column(String(998), nullable=False, default="")
    raw_body: Mapped[str] = mapped_column(
        Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False, default=""
    )
    dkim_result: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    received_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow_naive
    )
    parse_status: Mapped[str] = mapped_column(String(20), nullable=False)
    shifts_created: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # [{"line": 7, "text": "...", "error": "..."}, ...]
    errors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    vendor: Mapped["Vendor"] = relationship(back_populates="inbound_emails")
    shifts: Mapped[list["Shift"]] = relationship(back_populates="inbound_email")

    @property
    def draft_shifts(self) -> list["Shift"]:
        return [s for s in self.shifts if s.status == ShiftStatus.DRAFT]

    def __repr__(self) -> str:
        return f"<InboundEmail {self.id} vendor={self.vendor_id} {self.parse_status}>"
