"""UTC storage, venue-local conversion, and the timesheet CSV export."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time

import pytest

from extensions import db
from models import AssignmentStatus, Employee, Job, Shift, ShiftAssignment, ShiftStatus
from utils.timeutil import (
    format_local_clock,
    local_to_utc_naive,
    shift_window_utc,
    utc_naive_to_local,
)


class TestTimeConversion:
    def test_denver_summer_offset(self):
        assert local_to_utc_naive(datetime(2026, 7, 4, 18, 0), "America/Denver") == datetime(2026, 7, 5, 0, 0)

    def test_denver_winter_offset(self):
        assert local_to_utc_naive(datetime(2026, 12, 4, 18, 0), "America/Denver") == datetime(2026, 12, 5, 1, 0)

    def test_round_trip(self):
        utc = datetime(2026, 9, 14, 21, 7)
        local = utc_naive_to_local(utc, "America/New_York")
        assert local.utcoffset().total_seconds() == -4 * 3600
        assert local_to_utc_naive(local.replace(tzinfo=None), "America/New_York") == utc

    def test_overnight_window_crosses_midnight(self):
        start, end = shift_window_utc(date(2026, 9, 14), time(22), time(6), "America/Denver")
        assert (end - start).total_seconds() == 8 * 3600

    def test_dst_fall_back_night_shift_is_nine_real_hours(self):
        # 22:00 -> 06:00 local over the Nov 1 2026 fall-back: 9 elapsed hours.
        start, end = shift_window_utc(date(2026, 10, 31), time(22), time(6), "America/Denver")
        assert (end - start).total_seconds() == 9 * 3600

    def test_unknown_timezone_falls_back_safely(self):
        assert local_to_utc_naive(datetime(2026, 7, 4, 18), "Mars/Olympus_Mons") == datetime(2026, 7, 5, 0)

    def test_clock_format(self):
        assert format_local_clock(datetime(2026, 7, 4, 14, 2), "America/Denver") == "8:02 AM"
        assert format_local_clock(None, "America/Denver") == ""


@pytest.fixture()
def punched(app):
    """A *past* Denver shift across the Nov 2 2025 DST fall-back, punches in UTC.

    Must be in the past: missing-punch flags only apply once a shift has ended.
    """
    job = Job(title="Night Crew", location_address="Arena", timezone="America/Denver")
    worker = Employee(first_name="Noor", last_name="Night", phone_number="+13035550400")
    late = Employee(first_name="Lee", last_name="Late", phone_number="+13035550401")
    ghost = Employee(first_name="Gus", last_name="Ghost", phone_number="+13035550402")
    db.session.add_all([job, worker, late, ghost])
    db.session.flush()
    shift = Shift(job_id=job.id, date=date(2025, 11, 1), start_time=time(22), end_time=time(6),
                  required_headcount=3, status=ShiftStatus.FILLED)
    db.session.add(shift)
    db.session.flush()

    start_utc, _ = shift_window_utc(shift.date, shift.start_time, shift.end_time, job.timezone)
    db.session.add_all([
        # Full shift over the DST change: 22:00 MDT -> 06:00 MST = 9 real hours.
        ShiftAssignment(shift_id=shift.id, employee_id=worker.id, status=AssignmentStatus.accepted,
                        token="t-noor", check_in_at_utc=start_utc,
                        check_out_at_utc=local_to_utc_naive(datetime(2025, 11, 2, 6, 0), job.timezone)),
        # Checked in 25 min late, never checked out.
        ShiftAssignment(shift_id=shift.id, employee_id=late.id, status=AssignmentStatus.accepted,
                        token="t-lee", check_in_at_utc=local_to_utc_naive(datetime(2025, 11, 1, 22, 25), job.timezone)),
        # Confirmed but never showed.
        ShiftAssignment(shift_id=shift.id, employee_id=ghost.id, status=AssignmentStatus.accepted, token="t-gus"),
    ])
    db.session.commit()
    return shift


def export(admin_client, start="2025-10-31", end="2025-11-03") -> list[dict]:
    response = admin_client.get(f"/admin/timesheets.csv?start={start}&end={end}")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    return list(csv.DictReader(io.StringIO(response.data.decode())))


class TestTimesheetExport:
    def test_local_times_hours_and_dst(self, admin_client, punched):
        rows = {r["Worker"]: r for r in export(admin_client)}
        noor = rows["Noor Night"]
        assert noor["Check in (local)"] == "2025-11-01 22:00 MDT"
        assert noor["Check out (local)"] == "2025-11-02 06:00 MST"   # zone abbreviation flips
        assert noor["Hours worked"] == "9.00"                          # real elapsed, not wall 8h
        assert noor["Flags"] == ""

    def test_flags_missing_and_late_punches(self, admin_client, punched):
        rows = {r["Worker"]: r for r in export(admin_client)}
        assert set(rows["Lee Late"]["Flags"].split("; ")) == {"missing OUT", "late IN"}
        assert rows["Gus Ghost"]["Flags"] == "missing IN"
        assert rows["Gus Ghost"]["Hours worked"] == ""

    def test_csv_formula_injection_neutralized(self, admin_client, punched):
        worker = Employee.query.filter_by(first_name="Noor").one()
        worker.first_name = "=HYPERLINK(\"http://evil\")"
        db.session.commit()
        rows = export(admin_client)
        names = [r["Worker"] for r in rows]
        assert any(n.startswith("'=HYPERLINK") for n in names)
        assert all(not n.startswith("=") for n in names)
        assert all(r["Phone"].startswith("+1") for r in rows)  # real phone numbers untouched

    @pytest.mark.parametrize("query", [
        "", "?start=2026-11-02&end=2026-10-30", "?start=2026-01-01&end=2026-12-31", "?start=bad&end=2026-10-30",
    ])
    def test_invalid_ranges_redirect(self, admin_client, query):
        assert admin_client.get(f"/admin/timesheets.csv{query}").status_code == 302

    def test_requires_login(self, client):
        assert client.get("/admin/timesheets.csv?start=2026-10-30&end=2026-11-02").status_code == 302
