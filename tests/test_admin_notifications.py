"""Coordinator notifications: short-staffed, and newly covered.

Before this, the only signal was bad news. Silence meant either "everything
is covered" or "the alerting has broken", which look identical right up until
nobody turns up for a shift.
"""
from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from extensions import db
from models import AssignmentStatus, Employee, ShiftAssignment, ShiftStatus
from utils.gsm7 import is_gsm7, non_gsm7_characters, segment_count, to_gsm7


@pytest.fixture()
def captured(monkeypatch):
    """Capture every outbound body instead of sending it.

    NOTE: this replaces _send, which is where the GSM-7 sanitiser runs, so the
    captured body is pre-sanitisation. Encoding assertions below therefore
    apply to_gsm7 themselves, mirroring what actually reaches the wire. That
    the sanitiser is wired into _send at all is covered by
    tests/test_sms_encoding.py::TestSanitiserIsWiredIn.
    """
    from utils import sms as sms_module

    sent: list[dict] = []
    monkeypatch.setattr(
        sms_module.WhatsAppService,
        "_send",
        lambda self, to, **kw: sent.append({"to": to, **kw}) or True,
    )
    monkeypatch.setenv("ADMIN_WHATSAPP_NUMBER", "+17205844158")
    monkeypatch.setenv("BUSINESS_NAME", "Rohan Binu")
    return sent


class TestShiftCoveredNotification:
    def test_fires_when_the_last_seat_is_taken(self, client, make_assignment, captured):
        assignment = make_assignment(status=AssignmentStatus.pending)
        assignment.shift.required_headcount = 1
        db.session.commit()

        client.post(f"/accept/{assignment.token}")

        covered = [m for m in captured if "COVERED" in (m.get("body") or "")]
        assert covered, "no shift-covered notification was sent"
        assert covered[0]["to"] == "+17205844158"

    def test_silent_while_seats_remain(self, client, make_assignment, captured):
        """Only the last acceptance should notify, not every one."""
        assignment = make_assignment(status=AssignmentStatus.pending)
        assignment.shift.required_headcount = 5
        db.session.commit()

        client.post(f"/accept/{assignment.token}")

        assert not [m for m in captured if "COVERED" in (m.get("body") or "")]

    def test_carries_what_the_coordinator_needs(self, client, make_assignment, captured):
        assignment = make_assignment(status=AssignmentStatus.pending)
        shift = assignment.shift
        shift.required_headcount = 1
        shift.area = "Parking"
        db.session.commit()
        job_title = shift.job.title
        address = shift.job.location_address

        client.post(f"/accept/{assignment.token}")
        body = [m for m in captured if "COVERED" in (m.get("body") or "")][0]["body"]

        assert job_title in body
        assert address in body
        assert "Parking" in body
        assert f"/admin/shifts/{shift.id}" in body, "no link to act on"

    def test_acceptance_survives_a_notification_failure(
        self, client, make_assignment, monkeypatch
    ):
        """The worker's seat is already committed; messaging must not undo it."""
        from utils import sms as sms_module

        def explode(self, **kwargs):
            raise RuntimeError("twilio is down")

        monkeypatch.setattr(sms_module.WhatsAppService, "send_shift_covered", explode)

        assignment = make_assignment(status=AssignmentStatus.pending)
        assignment.shift.required_headcount = 1
        db.session.commit()

        response = client.post(f"/accept/{assignment.token}")

        assert response.status_code == 200
        db.session.refresh(assignment)
        assert assignment.status == AssignmentStatus.accepted

    def test_a_full_shift_does_not_re_notify(self, client, make_assignment, captured):
        """Arriving late at a full shift is a 409, not another 'covered' text."""
        assignment = make_assignment(status=AssignmentStatus.pending)
        shift = assignment.shift
        shift.required_headcount = 1
        other = Employee(first_name="Ana", last_name="Diaz", phone_number="+17205550199")
        db.session.add(other)
        db.session.flush()
        db.session.add(ShiftAssignment(
            shift_id=shift.id, employee_id=other.id,
            status=AssignmentStatus.accepted, token="taken-seat",
        ))
        db.session.commit()
        captured.clear()

        response = client.post(f"/accept/{assignment.token}")

        assert response.status_code == 409
        assert not [m for m in captured if "COVERED" in (m.get("body") or "")]


class TestAdminMessagesAreCheap:
    """Admin alerts go out on every understaffed shift; they should not be
    three segments because of a decorative character."""

    def _service(self):
        from utils.sms import WhatsAppService

        return WhatsAppService(channel="sms")

    def test_needs_workers_alert_is_gsm7(self, app, captured):
        self._service().send_staffing_alert(
            title="Ball Arena", location_address="1000 Chopper Cir, Denver, CO",
            work_date=date(2026, 9, 18), start_time=time(15, 0), end_time=None,
            accepted=1, required=3, shift_id=7,
        )
        body = to_gsm7(captured[-1]["body"])
        assert is_gsm7(body), f"forced to UCS-2 by {non_gsm7_characters(body)}"
        assert segment_count(body) <= 2

    def test_covered_alert_is_gsm7(self, app, captured):
        self._service().send_shift_covered(
            title="Ball Arena", location_address="1000 Chopper Cir, Denver, CO",
            work_date=date(2026, 9, 18), start_time=time(15, 0), end_time=time(23, 0),
            required=3, area="Parking", shift_id=7,
        )
        body = to_gsm7(captured[-1]["body"])
        assert is_gsm7(body), f"forced to UCS-2 by {non_gsm7_characters(body)}"
        assert segment_count(body) <= 2

    def test_no_emoji_in_admin_alerts(self, app, captured):
        """An emoji costs roughly triple. It is not worth a warning triangle."""
        self._service().send_staffing_alert(
            title="Ball Arena", location_address="1000 Chopper Cir, Denver, CO",
            work_date=date(2026, 9, 18), start_time=time(15, 0), end_time=None,
            accepted=0, required=2,
        )
        assert not non_gsm7_characters(to_gsm7(captured[-1]["body"]))


class TestWorkerPinHint:
    def test_reminder_tells_the_worker_where_their_pin_comes_from(self, app, captured):
        from utils.sms import WhatsAppService

        WhatsAppService(channel="sms").send_shift_reminder(
            "+17205550101", title="Ball Arena",
            location_address="1000 Chopper Cir, Denver, CO",
            work_date=date(2026, 9, 18), start_time=time(15, 0), end_time=time(23, 0),
            punch_url="https://example.test/punch/abc", lang="en",
        )
        assert "last 4 digits of your phone number" in captured[-1]["body"]

    def test_the_hint_is_translated(self, app, captured):
        from utils.sms import WhatsAppService

        WhatsAppService(channel="sms").send_shift_reminder(
            "+17205550101", title="Ball Arena",
            location_address="1000 Chopper Cir, Denver, CO",
            work_date=date(2026, 9, 18), start_time=time(15, 0), end_time=time(23, 0),
            punch_url="https://example.test/punch/abc", lang="es",
        )
        body = to_gsm7(captured[-1]["body"])
        assert "Su PIN son los ultimos 4 digitos" in body
        assert is_gsm7(body)

    def test_no_hint_when_there_is_no_check_in_link(self, app, captured):
        """Nothing to punch means the PIN is irrelevant; don't pay for the line."""
        from utils.sms import WhatsAppService

        WhatsAppService(channel="sms").send_shift_reminder(
            "+17205550101", title="Ball Arena",
            location_address="1000 Chopper Cir, Denver, CO",
            work_date=date(2026, 9, 18), start_time=time(15, 0), end_time=time(23, 0),
            punch_url=None, lang="en",
        )
        assert "last 4 digits" not in captured[-1]["body"]
