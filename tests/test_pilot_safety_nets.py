"""Pilot safety nets: default PINs, 5-minute snap-to-start, manager manual punch."""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, time, timedelta

import pytest

from conftest import WORKER_PHONE
from extensions import db
from models import AssignmentStatus, Employee, PunchSource, Shift, ShiftAssignment, ShiftStatus, utcnow_naive
from utils import pins
from utils.manual_punch import manual_punch
from utils.punch import check_in_window_utc, handle_inbound_sms, payroll_check_in_time
from utils.timeutil import local_to_utc_naive, utc_naive_to_local
from utils.web_punch import ensure_punch_token, web_punch

TZ = "America/Denver"


def start_of(assignment) -> datetime:
    return check_in_window_utc(assignment)[1]


# =========================================================================== #
# 1. Default PIN = last 4 digits of the phone
# =========================================================================== #
class TestDefaultPin:
    @pytest.mark.parametrize("phone,expected", [
        ("+13035550911", "0911"), ("303-555-4821", "4821"), ("(720) 584 4158", "4158"),
    ])
    def test_default_pin_is_last_four_digits(self, phone, expected):
        assert pins.default_pin_for(phone) == expected

    def test_too_short_phone_rejected(self):
        with pytest.raises(ValueError):
            pins.default_pin_for("123")

    def test_admin_add_worker_sets_phone_default_pin(self, admin_client):
        response = admin_client.post("/admin/employees", data={
            "first_name": "Dana", "last_name": "Default", "phone_number": "303-555-7362",
        }, follow_redirects=True)
        employee = Employee.query.filter_by(first_name="Dana").one()
        assert pins.verify_pin(employee, "7362")
        assert employee.pin_hash != "7362" and len(employee.pin_hash) == 64  # still stored hashed
        assert b"check-in PIN is 7362" in response.data
        assert b"last 4 digits of their phone" in response.data

    def test_reset_pin_replaces_default_with_random(self, admin_client, monkeypatch):
        admin_client.post("/admin/employees", data={
            "first_name": "Rex", "last_name": "Reset", "phone_number": "303-555-2468",
        })
        employee = Employee.query.filter_by(first_name="Rex").one()
        monkeypatch.setattr(pins, "generate_pin", lambda: "9173")
        admin_client.post(f"/admin/employees/{employee.id}/reset-pin")
        db.session.refresh(employee)
        assert not pins.verify_pin(employee, "2468")
        assert pins.verify_pin(employee, "9173")

    def test_csv_import_sets_default_pins(self, app, tmp_path):
        roster = tmp_path / "workers.csv"
        roster.write_text("first_name,last_name,phone_number\nIvy,Import,303-555-1357\nOz,Import,720-555-8642\n")
        result = app.test_cli_runner().invoke(args=["admin", "import-workers", str(roster)])
        assert result.exit_code == 0, result.output
        assert "last 4 digits" in result.output
        ivy = Employee.query.filter_by(first_name="Ivy").one()
        oz = Employee.query.filter_by(first_name="Oz").one()
        assert pins.verify_pin(ivy, "1357") and pins.verify_pin(oz, "8642")

    def test_issue_pins_phone_default_for_existing_workers(self, app, worker):
        result = app.test_cli_runner().invoke(args=["admin", "issue-pins", "--phone-default"])
        assert result.exit_code == 0, result.output
        db.session.refresh(worker)
        assert pins.verify_pin(worker, WORKER_PHONE[-4:])

    def test_default_pin_works_on_keypad(self, app, make_assignment, worker):
        pins.set_default_pin(worker)
        assignment = make_assignment()
        ensure_punch_token(assignment)
        db.session.commit()
        result = web_punch(token=assignment.punch_token, pin=WORKER_PHONE[-4:], action="in",
                           now_utc=start_of(assignment))
        assert result.body["status"] == "checked_in"


# =========================================================================== #
# 2. Five-minute grace period (snap-to-start)
# =========================================================================== #
class TestGracePeriod:
    START = datetime(2031, 5, 10, 21, 0)  # 3:00 PM in Denver (UTC-6)

    @pytest.mark.parametrize("offset,snapped", [
        (timedelta(minutes=-6), False),               # outside grace: exact
        (timedelta(minutes=-5, seconds=-1), False),   # just outside
        (timedelta(minutes=-5), True),                # boundary: snapped
        (timedelta(minutes=-2, seconds=-30), True),
        (timedelta(seconds=-1), True),
        (timedelta(0), False),                        # on time: exact (equals start anyway)
        (timedelta(minutes=7), False),                # late: exact
    ])
    def test_payroll_time_rule(self, offset, snapped):
        actual = self.START + offset
        expected = self.START if snapped else actual
        assert payroll_check_in_time(actual, self.START) == expected

    def test_grace_minutes_configurable(self, monkeypatch):
        monkeypatch.setenv("CHECK_IN_GRACE_MINUTES", "10")
        assert payroll_check_in_time(self.START - timedelta(minutes=9), self.START) == self.START

    def _pin_ready(self, make_assignment, worker, **kwargs):
        worker.pin_hash = pins.hash_pin(worker.id, "4821")
        assignment = make_assignment(**kwargs)
        ensure_punch_token(assignment)
        db.session.commit()
        return assignment

    def test_keypad_early_within_grace_snaps_and_keeps_actual(self, app, make_assignment, worker):
        assignment = self._pin_ready(make_assignment, worker)
        start = start_of(assignment)
        tapped = start - timedelta(minutes=3, seconds=12)
        result = web_punch(token=assignment.punch_token, pin="4821", action="in", now_utc=tapped)
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc == start            # payroll time
        assert assignment.check_in_actual_at_utc == tapped    # real tap kept
        assert assignment.check_in_was_snapped
        assert result.body["time"] == datetime.combine(assignment.shift.date, assignment.shift.start_time).strftime("%I:%M %p").lstrip("0")

    def test_keypad_earlier_than_grace_records_exact(self, app, make_assignment, worker):
        assignment = self._pin_ready(make_assignment, worker)
        tapped = start_of(assignment) - timedelta(minutes=20)
        web_punch(token=assignment.punch_token, pin="4821", action="in", now_utc=tapped)
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc == tapped and not assignment.check_in_was_snapped

    @pytest.mark.parametrize("late", [timedelta(0), timedelta(minutes=4)])
    def test_keypad_on_time_or_late_records_exact(self, app, make_assignment, worker, late):
        assignment = self._pin_ready(make_assignment, worker)
        tapped = start_of(assignment) + late
        web_punch(token=assignment.punch_token, pin="4821", action="in", now_utc=tapped)
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc == tapped

    def test_sms_in_uses_same_rule(self, app, make_assignment, worker):
        assignment = make_assignment()
        start = start_of(assignment)
        outcome = handle_inbound_sms(message_sid="SMgrace", from_number=WORKER_PHONE, body="IN",
                                     now_utc=start - timedelta(minutes=4))
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc == start
        clock = datetime.combine(assignment.shift.date, assignment.shift.start_time).strftime("%I:%M %p").lstrip("0")
        assert clock in outcome.reply

    def test_checkout_right_after_snapped_in_never_goes_negative(self, app, make_assignment, worker):
        assignment = make_assignment()
        start = start_of(assignment)
        handle_inbound_sms(message_sid="SMg1", from_number=WORKER_PHONE, body="IN", now_utc=start - timedelta(minutes=4))
        handle_inbound_sms(message_sid="SMg2", from_number=WORKER_PHONE, body="OUT", now_utc=start - timedelta(minutes=2))
        db.session.refresh(assignment)
        assert assignment.check_out_at_utc >= assignment.check_in_at_utc
        assert assignment.worked_hours == 0

    def test_timesheet_shows_actual_tap_only_when_snapped(self, admin_client, make_assignment, worker):
        snapped = make_assignment()
        start = start_of(snapped)
        handle_inbound_sms(message_sid="SMts", from_number=WORKER_PHONE, body="IN", now_utc=start - timedelta(minutes=2))
        day = snapped.shift.date
        rows = list(csv.DictReader(io.StringIO(admin_client.get(
            f"/admin/timesheets.csv?start={day - timedelta(days=1)}&end={day + timedelta(days=1)}").data.decode())))
        (row,) = rows
        assert row["Actual tap (local)"] != ""
        assert row["Check in (local)"].startswith(str(day))


# =========================================================================== #
# 3. Manager manual punch (dead phone)
# =========================================================================== #
def local_input(utc_dt: datetime) -> str:
    return utc_naive_to_local(utc_dt, TZ).strftime("%Y-%m-%dT%H:%M")


class TestManualPunchService:
    def test_check_in_and_out(self, app, make_assignment, worker):
        assignment = make_assignment()
        start = start_of(assignment)
        local_in = utc_naive_to_local(start + timedelta(minutes=12), TZ).replace(tzinfo=None)
        now = start + timedelta(hours=5)

        first = manual_punch(shift_id=assignment.shift_id, employee_id=worker.id, action="in",
                             local_time=local_in, note="dead phone", now_utc=now)
        local_out = utc_naive_to_local(start + timedelta(hours=4), TZ).replace(tzinfo=None)
        second = manual_punch(shift_id=assignment.shift_id, employee_id=worker.id, action="out",
                              local_time=local_out, note="", now_utc=now)

        assert first.ok and second.ok, (first, second)
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc == start + timedelta(minutes=12)  # venue-local converted to UTC
        assert assignment.check_out_at_utc == start + timedelta(hours=4)
        assert (assignment.check_in_source, assignment.check_out_source) == (PunchSource.ADMIN, PunchSource.ADMIN)
        assert assignment.punch_note == "IN: dead phone; OUT: manual"
        assert assignment.worked_hours == pytest.approx(3.8)

    def test_manual_check_in_gets_grace_snap(self, app, make_assignment, worker):
        assignment = make_assignment()
        start = start_of(assignment)
        local_in = utc_naive_to_local(start - timedelta(minutes=3), TZ).replace(tzinfo=None)
        result = manual_punch(shift_id=assignment.shift_id, employee_id=worker.id, action="in",
                              local_time=local_in, now_utc=start + timedelta(hours=1))
        db.session.refresh(assignment)
        assert result.ok and "snapped to shift start" in result.message
        assert assignment.check_in_at_utc == start

    def test_never_overwrites_an_existing_punch(self, app, make_assignment, worker):
        assignment = make_assignment()
        start = start_of(assignment)
        handle_inbound_sms(message_sid="SMown", from_number=WORKER_PHONE, body="IN", now_utc=start + timedelta(minutes=1))
        local = utc_naive_to_local(start + timedelta(minutes=30), TZ).replace(tzinfo=None)
        result = manual_punch(shift_id=assignment.shift_id, employee_id=worker.id, action="in",
                              local_time=local, now_utc=start + timedelta(hours=1))
        db.session.refresh(assignment)
        assert not result.ok and "already checked in" in result.message
        assert assignment.check_in_at_utc == start + timedelta(minutes=1)
        assert assignment.check_in_source == PunchSource.SMS

    @pytest.mark.parametrize("case,expected", [
        ("out_before_in", "before checking them out"),
        ("future", "in the future"),
        ("wrong_day", "more than a day"),
        ("bad_action", "Choose check-in or check-out"),
    ])
    def test_rejections(self, app, make_assignment, worker, case, expected):
        assignment = make_assignment()
        start = start_of(assignment)
        local = utc_naive_to_local(start, TZ).replace(tzinfo=None)
        action = "in"
        now = start + timedelta(hours=1)
        if case == "out_before_in":
            action = "out"
        elif case == "future":
            local = utc_naive_to_local(now + timedelta(hours=2), TZ).replace(tzinfo=None)
        elif case == "wrong_day":
            local = local - timedelta(days=3)
            now = start + timedelta(days=5)
        elif case == "bad_action":
            action = "sideways"
        result = manual_punch(shift_id=assignment.shift_id, employee_id=worker.id, action=action,
                              local_time=local, now_utc=now)
        assert not result.ok and expected in result.message
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc is None

    def test_check_out_cannot_precede_check_in(self, app, make_assignment, worker):
        assignment = make_assignment()
        start = start_of(assignment)
        now = start + timedelta(hours=3)
        manual_punch(shift_id=assignment.shift_id, employee_id=worker.id, action="in",
                     local_time=utc_naive_to_local(start + timedelta(hours=1), TZ).replace(tzinfo=None), now_utc=now)
        result = manual_punch(shift_id=assignment.shift_id, employee_id=worker.id, action="out",
                              local_time=utc_naive_to_local(start + timedelta(minutes=30), TZ).replace(tzinfo=None), now_utc=now)
        assert not result.ok and "can't be before check-in" in result.message

    @pytest.mark.parametrize("status,shift_status", [
        (AssignmentStatus.pending, ShiftStatus.OPEN),
        (AssignmentStatus.cancelled, ShiftStatus.OPEN),
        (AssignmentStatus.accepted, ShiftStatus.DRAFT),
    ])
    def test_only_confirmed_workers_on_open_shifts(self, app, make_assignment, worker, status, shift_status):
        assignment = make_assignment(status=status, shift_status=shift_status)
        start = start_of(assignment)
        result = manual_punch(shift_id=assignment.shift_id, employee_id=worker.id, action="in",
                              local_time=utc_naive_to_local(start, TZ).replace(tzinfo=None), now_utc=start + timedelta(hours=1))
        assert not result.ok


class TestManualPunchRoute:
    def test_requires_admin_login(self, client, make_assignment, worker):
        assignment = make_assignment()
        response = client.post(f"/admin/shift/{assignment.shift_id}/manual-punch/{worker.id}",
                               data={"action": "in", "punch_time": local_input(utcnow_naive())})
        assert response.status_code == 302 and "/login" in response.headers["Location"]
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc is None

    def test_csrf_required_for_manual_punch(self, make_assignment):
        from app import create_app
        from config import TestingConfig

        class CsrfOn(TestingConfig):
            WTF_CSRF_ENABLED = True

        app = create_app(CsrfOn)
        with app.app_context():
            db.create_all()
            client = app.test_client()
            with client.session_transaction() as sess:
                sess["is_admin"] = True
            response = client.post("/admin/shift/1/manual-punch/1", data={"action": "in", "punch_time": "2031-01-01T10:00"})
            assert response.status_code == 400  # rejected: no CSRF token
            db.session.remove()
            db.drop_all()

    def test_dead_phone_flow_through_admin_page(self, admin_client, make_assignment, worker):
        assignment = make_assignment(start_in=timedelta(minutes=-45))
        punch_at = utcnow_naive() - timedelta(minutes=30)
        page = admin_client.get(f"/admin/shifts/{assignment.shift_id}").data.decode()
        assert "Manual punch" in page and "Record check-in" in page

        response = admin_client.post(
            f"/admin/shift/{assignment.shift_id}/manual-punch/{worker.id}",
            data={"action": "in", "punch_time": local_input(punch_at), "note": "Dead phone"},
            follow_redirects=True,
        )
        assert b"checked in at" in response.data
        db.session.refresh(assignment)
        assert assignment.check_in_source == PunchSource.ADMIN
        page = response.data.decode()
        assert "Record check-out" in page and "manual" in page and "IN: Dead phone" in page

    def test_bad_time_input_flashes_error(self, admin_client, make_assignment, worker):
        assignment = make_assignment()
        response = admin_client.post(f"/admin/shift/{assignment.shift_id}/manual-punch/{worker.id}",
                                     data={"action": "in", "punch_time": "yesterday-ish"}, follow_redirects=True)
        assert b"Enter the date and time" in response.data

    def test_unknown_ids_404(self, admin_client, make_assignment, worker):
        assignment = make_assignment()
        assert admin_client.post(f"/admin/shift/99999/manual-punch/{worker.id}",
                                 data={"action": "in", "punch_time": "2031-01-01T10:00"}).status_code == 404
        assert admin_client.post(f"/admin/shift/{assignment.shift_id}/manual-punch/99999",
                                 data={"action": "in", "punch_time": "2031-01-01T10:00"}).status_code == 404

    def test_button_hidden_for_unconfirmed_and_completed(self, admin_client, make_assignment, worker):
        pending = make_assignment(status=AssignmentStatus.pending)
        assert "Manual punch" not in admin_client.get(f"/admin/shifts/{pending.shift_id}").data.decode()

        done = make_assignment(start_in=timedelta(hours=-10), hours=4)
        done.check_in_at_utc = start_of(done)
        done.check_out_at_utc = start_of(done) + timedelta(hours=4)
        db.session.commit()
        assert "Manual punch" not in admin_client.get(f"/admin/shifts/{done.shift_id}").data.decode()
