"""SQLAlchemy models for the shift fulfillment system (MySQL / InnoDB).

Uses SQLAlchemy 2.0 typed mappings (``Mapped`` / ``mapped_column``) for strict
type hinting. Design highlights:

* InnoDB engine (MySQL default) so foreign keys and ``SELECT ... FOR UPDATE``
  row locking are available.
* Foreign-key columns are explicitly indexed; additional composite indexes
  back the hot query paths (per-job/date listing, per-shift accepted counts).
* ``ShiftAssignment.token`` is unique and indexed for O(1) public lookups.
* Assignment status is a native enum constrained to the four legal states.
"""
from __future__ import annotations

import enum
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    func,
)
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

    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"


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

    shifts: Mapped[list["Shift"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.title}>"


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

    job: Mapped["Job"] = relationship(back_populates="shifts")
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

    shift: Mapped["Shift"] = relationship(back_populates="assignments")
    employee: Mapped["Employee"] = relationship(back_populates="assignments")

    __table_args__ = (
        # An employee should only ever be offered a given shift once.
        UniqueConstraint("shift_id", "employee_id", name="uq_shift_employee"),
        # Backs the per-shift accepted-count query inside the concurrency guard.
        Index("ix_assignments_shift_status", "shift_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<ShiftAssignment {self.id} shift={self.shift_id} "
            f"{self.status.value}>"
        )
