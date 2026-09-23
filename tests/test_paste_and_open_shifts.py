"""Pasted vendor schedules, optional end time ("until done"), and optional area."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from conftest import WORKER_PHONE
from extensions import db
from models import AssignmentStatus, InboundEmail, Job, Shift, ShiftAssignment, ShiftStatus, utcnow_naive
from utils.email_parser import infer_year, parse_shift_email
from utils.punch import check_in_window_utc, handle_inbound_sms
from utils.sms import ShiftBroadcastDetails, WhatsAppService

SAMPLE = """Good morning here is the schedule for Labor day weekend please let me know if you have any questions...

9/2 20 @ 1pm

9/3 2 @ 6am parking

9/4 20 @ 1pm

9/5 4 @ 6am parking
20 @ 3pm

9/6 4 @ 6am parking
20 @ 10 am-6pm

9/7 4 @ 6am parking

9/11 20 @ 3pm
9/12 3 @ 6am parking

Example Name
General Manager
Example Facility Services"""


@pytest.fixture()
def arena(app):
    job = Job(title="Ball Arena Cleaning", location_address="1000 Chopper Cir", timezone="America/Denver")
    db.session.add(job)
    db.session.commit()
    return job


# =========================================================================== #
# Parser: the real vendor format
# =========================================================================== #
class TestShorthandParser:
    def test_real_vendor_email(self):
        result = parse_shift_email(SAMPLE, today=date(2026, 9, 1), reference_date=date(2026, 9, 1))
        assert result.errors == []
        got = [(s.work_date, s.headcount, s.start, s.end, s.area) for s in result.shifts]
        assert got == [
            (date(2026, 9, 2), 20, time(13), None, None),
            (date(2026, 9, 3), 2, time(6), None, "Parking"),
            (date(2026, 9, 4), 20, time(13), None, None),
            (date(2026, 9, 5), 4, time(6), None, "Parking"),
            (date(2026, 9, 5), 20, time(15), None, None),          # continuation line
            (date(2026, 9, 6), 4, time(6), None, "Parking"),
            (date(2026, 9, 6), 20, time(10), time(18), None),       # only line with an end
            (date(2026, 9, 7), 4, time(6), None, "Parking"),
            (date(2026, 9, 11), 20, time(15), None, None),
            (date(2026, 9, 12), 3, time(6), None, "Parking"),
        ]

    def test_past_dates_reported_not_created(self):
        result = parse_shift_email(SAMPLE, today=date(2026, 9, 6), reference_date=date(2026, 9, 6))
        assert [s.work_date.day for s in result.shifts] == [5, 5, 6, 6, 7, 11, 12]  # yesterday allowed
        assert all("already passed" in e.error for e in result.errors)
        assert len(result.errors) == 3  # 9/2, 9/3, 9/4

    @pytest.mark.parametrize("month,day,reference,expected", [
        (1, 3, date(2026, 12, 20), date(2027, 1, 3)),   # December email about January
        (12, 28, date(2027, 1, 2), date(2026, 12, 28)), # January email about late December
        (9, 5, date(2026, 9, 1), date(2026, 9, 5)),
    ])
    def test_year_inference(self, month, day, reference, expected):
        assert infer_year(month, day, reference) == expected

    @pytest.mark.parametrize("text,error", [
        ("20 @ 3pm", "no date for this line"),                 # nothing above it
        ("9/5 20 @ 3", "ambiguous"),                           # no am/pm
        ("9/5 0 @ 3pm", "number of people"),
        ("9/31 4 @ 6am", "not a real calendar date"),
        ("9/5 4 @ 6am-6am parking", "same"),
    ])
    def test_shorthand_errors(self, text, error):
        result = parse_shift_email(text, today=date(2026, 9, 1), reference_date=date(2026, 9, 1))
        assert result.shifts == [] and error in result.errors[0].error

    def test_blank_line_ends_a_date_group(self):
        result = parse_shift_email("9/5 4 @ 6am parking\n\n20 @ 3pm", today=date(2026, 9, 1))
        assert len(result.shifts) == 1 and "no date" in result.errors[0].error


# =========================================================================== #
# Paste -> drafts -> edit -> approve
# =========================================================================== #
class TestPasteSchedule:
    def paste(self, admin_client, job, text=SAMPLE, sent_on=None):
        sent_on = sent_on or (date.today() - timedelta(days=1)).isoformat()
        return admin_client.post("/admin/inbound/paste", data={
            "schedule_text": text, "job_id": job.id, "sent_on": sent_on,
        }, follow_redirects=True)

    def future_text(self):
        d1, d2 = date.today() + timedelta(days=3), date.today() + timedelta(days=4)
        return f"{d1.month}/{d1.day} 20 @ 1pm\n{d2.month}/{d2.day} 4 @ 6am parking\n20 @ 10 am-6pm"

    def test_paste_creates_drafts_with_optional_fields_only_when_given(self, admin_client, arena):
        response = self.paste(admin_client, arena, self.future_text())
        assert b"Read 3 shift(s) as drafts" in response.data
        shifts = Shift.query.order_by(Shift.date, Shift.start_time).all()
        assert [(s.end_time, s.area) for s in shifts] == [(None, None), (None, "Parking"), (time(18), None)]
        assert all(s.status == ShiftStatus.DRAFT and s.job_id == arena.id for s in shifts)
        record = InboundEmail.query.one()
        assert record.source == "paste" and record.vendor_id is None and record.sender_label == "Pasted schedule"

    def test_inbox_shows_pasted_schedule_without_inventing_end_or_area(self, admin_client, arena):
        self.paste(admin_client, arena, self.future_text())
        page = admin_client.get("/admin/inbound").data.decode()
        assert "Pasted schedule" in page and 'value="Parking"' in page
        assert page.count('name="end_time" type="time" value=""') == 2  # two lines had no end

    def test_paste_requires_text_and_job(self, admin_client, arena):
        assert b"Paste the schedule text first" in admin_client.post(
            "/admin/inbound/paste", data={"schedule_text": " ", "job_id": arena.id}, follow_redirects=True).data
        assert b"Pick which job" in admin_client.post(
            "/admin/inbound/paste", data={"schedule_text": "9/5 20 @ 3pm", "job_id": ""}, follow_redirects=True).data
        assert Shift.query.count() == 0

    def test_repasting_does_not_double_book(self, admin_client, arena):
        self.paste(admin_client, arena, self.future_text())
        response = self.paste(admin_client, arena, self.future_text())
        assert Shift.query.count() == 3
        assert b"already scheduled" in response.data

    def test_edit_draft_then_approve(self, admin_client, arena):
        self.paste(admin_client, arena, self.future_text())
        draft = Shift.query.filter_by(area=None, end_time=None).first()
        admin_client.post(f"/admin/shifts/{draft.id}/edit-draft", data={
            "date": draft.date.isoformat(), "start_time": "13:30", "end_time": "21:00",
            "area": "Suites", "required_headcount": "18",
        })
        db.session.refresh(draft)
        assert (draft.start_time, draft.end_time, draft.area, draft.required_headcount) == (time(13, 30), time(21), "Suites", 18)

        admin_client.post(f"/admin/shifts/{draft.id}/edit-draft", data={
            "date": draft.date.isoformat(), "start_time": "13:30", "end_time": "", "area": "", "required_headcount": "18",
        })
        db.session.refresh(draft)
        assert draft.end_time is None and draft.area is None  # cleared back to "not given"

        admin_client.post(f"/admin/inbound/{draft.inbound_email_id}/approve")
        assert Shift.query.filter_by(status=ShiftStatus.DRAFT).count() == 0

    def test_only_drafts_are_editable_here(self, admin_client, arena):
        self.paste(admin_client, arena, self.future_text())
        shift = Shift.query.first()
        shift.status = ShiftStatus.OPEN
        db.session.commit()
        response = admin_client.post(f"/admin/shifts/{shift.id}/edit-draft", data={
            "date": shift.date.isoformat(), "start_time": "05:00", "end_time": "", "area": "", "required_headcount": "1",
        }, follow_redirects=True)
        assert b"Only draft shifts" in response.data
        db.session.refresh(shift)
        assert shift.start_time != time(5)

    def test_edit_draft_validation(self, admin_client, arena):
        self.paste(admin_client, arena, self.future_text())
        draft = Shift.query.first()
        response = admin_client.post(f"/admin/shifts/{draft.id}/edit-draft", data={
            "date": draft.date.isoformat(), "start_time": "13:00", "end_time": "13:00", "area": "", "required_headcount": "5",
        }, follow_redirects=True)
        assert b"end can&#39;t equal start" in response.data or b"end can't equal start" in response.data


# =========================================================================== #
# Open-ended shifts and areas across the app
# =========================================================================== #
class TestOpenEndedShifts:
    def test_manual_create_without_end_and_with_area(self, admin_client, arena):
        day = date.today() + timedelta(days=2)
        admin_client.post("/admin/shifts", data={
            "job_id": arena.id, "dates": [day.isoformat()], "start_time": "15:00",
            "end_time": "", "area": "Parking", "required_headcount": "4",
        })
        shift = Shift.query.one()
        assert shift.end_time is None and shift.area == "Parking"
        assert shift.time_label == "3:00 PM" and shift.estimated_hours is None
        page = admin_client.get("/admin/shifts").data.decode()
        assert "3:00 PM · Parking" in page

    def test_label_with_end(self, app, arena):
        shift = Shift(job_id=arena.id, date=date.today(), start_time=time(10), end_time=time(18))
        assert shift.time_label == "10:00 AM – 6:00 PM" and shift.estimated_hours == 8

    def test_open_ended_check_in_window_is_capped(self, app, arena, worker, monkeypatch):
        monkeypatch.setenv("OPEN_SHIFT_MAX_HOURS", "16")
        shift = Shift(job_id=arena.id, date=date(2031, 6, 1), start_time=time(15), end_time=None, status=ShiftStatus.OPEN)
        db.session.add(shift)
        db.session.flush()
        assignment = ShiftAssignment(shift_id=shift.id, employee_id=worker.id, status=AssignmentStatus.accepted, token="open-ended")
        db.session.add(assignment)
        db.session.commit()
        _opens, start, end = check_in_window_utc(assignment)
        assert end - start == timedelta(hours=16)
        assert handle_inbound_sms(message_sid="SMopen", from_number=WORKER_PHONE, body="IN",
                                  now_utc=start + timedelta(hours=10)).result == "checked_in"

    def test_copy_week_keeps_blank_end_and_area(self, admin_client, arena):
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        db.session.add(Shift(job_id=arena.id, date=monday + timedelta(days=1), start_time=time(6), area="Parking",
                             status=ShiftStatus.OPEN))
        db.session.commit()
        admin_client.post("/admin/shifts/copy-week", data={"week_start": monday.isoformat()})
        copy = Shift.query.filter(Shift.date == monday + timedelta(days=8)).one()
        assert copy.end_time is None and copy.area == "Parking"

    def test_sms_bodies_skip_missing_end_and_hours(self):
        service = WhatsAppService(account_sid="", auth_token="")
        details = ShiftBroadcastDetails(title="Ball Arena Cleaning", location_address="1000 Chopper Cir",
                                        work_date=date(2031, 6, 1), start_time=time(15), end_time=None,
                                        estimated_hours=None, area="Parking")
        body = service._format_body(details, "tok")
        assert "Time: 3:00 PM\n" in body and "hrs" not in body and "Area: Parking" in body
        no_area = service._format_body(
            ShiftBroadcastDetails("Crew", "Arena", date(2031, 6, 1), time(10), time(18), 8.0), "tok")
        assert "Area:" not in no_area and "10:00 AM – 6:00 PM (~8 hrs)" in no_area

    def test_timesheet_leaves_scheduled_end_blank(self, admin_client, arena, worker):
        day = date.today() - timedelta(days=2)
        shift = Shift(job_id=arena.id, date=day, start_time=time(9), end_time=None, area="Parking", status=ShiftStatus.FILLED)
        db.session.add(shift)
        db.session.flush()
        db.session.add(ShiftAssignment(shift_id=shift.id, employee_id=worker.id, status=AssignmentStatus.accepted, token="ts-open"))
        db.session.commit()
        import csv, io
        rows = list(csv.DictReader(io.StringIO(admin_client.get(
            f"/admin/timesheets.csv?start={day}&end={day}").data.decode())))
        assert rows[0]["Scheduled end"] == "" and rows[0]["Area"] == "Parking"
