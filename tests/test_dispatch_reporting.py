"""Dispatch must never report a failed broadcast as a success.

The original code flashed "success" with the failure count embedded in the
text, so a total send failure rendered as a green banner with a checkmark and
the roster still showed everyone "waiting to reply". A manager reading that
waits for answers that were never requested.
"""
from __future__ import annotations

import pytest

from extensions import db
from models import Employee, ShiftStatus
from utils.sms import BroadcastResult


@pytest.fixture()
def failing_sms(monkeypatch):
    """Make every outbound message fail, the way an unverified sender does."""
    from utils import sms as sms_module

    def always_fail(self, to, **kwargs):
        return False

    monkeypatch.setattr(sms_module.WhatsAppService, "_send", always_fail)


@pytest.fixture()
def succeeding_sms(monkeypatch):
    from utils import sms as sms_module

    monkeypatch.setattr(sms_module.WhatsAppService, "_send", lambda self, to, **kw: True)


class TestDispatchReporting:
    def test_total_failure_is_an_error_not_a_success(
        self, admin_client, seed, failing_sms
    ):
        shift = seed["shift"]
        page = admin_client.post(
            f"/admin/shifts/{shift.id}/dispatch", follow_redirects=True
        ).get_data(as_text=True)

        assert "NOBODY WAS TEXTED" in page
        # The red banner is the one with a 2px rose border; the green success
        # banner must not be on the page at all.
        assert "border-rose-300" in page
        assert "bg-emerald-50" not in page

    def test_total_failure_names_who_was_not_reached(
        self, admin_client, seed, failing_sms
    ):
        """A count alone leaves the manager with nobody to call."""
        shift = seed["shift"]
        page = admin_client.post(
            f"/admin/shifts/{shift.id}/dispatch", follow_redirects=True
        ).get_data(as_text=True)

        for employee in Employee.query.all():
            assert employee.full_name in page

    def test_partial_failure_is_still_an_error(
        self, admin_client, seed, monkeypatch
    ):
        from utils import sms as sms_module

        calls = {"n": 0}

        def first_ok_then_fail(self, to, **kwargs):
            calls["n"] += 1
            return calls["n"] == 1

        monkeypatch.setattr(sms_module.WhatsAppService, "_send", first_ok_then_fail)

        shift = seed["shift"]
        page = admin_client.post(
            f"/admin/shifts/{shift.id}/dispatch", follow_redirects=True
        ).get_data(as_text=True)

        assert "FAILED" in page
        assert "border-rose-300" in page

    def test_full_success_is_a_success(self, admin_client, seed, succeeding_sms):
        shift = seed["shift"]
        page = admin_client.post(
            f"/admin/shifts/{shift.id}/dispatch", follow_redirects=True
        ).get_data(as_text=True)

        assert "border-rose-300" not in page
        assert "bg-emerald-50" in page
        assert "NOBODY WAS TEXTED" not in page

    def test_weekly_dispatch_reports_failure_too(
        self, admin_client, seed, failing_sms
    ):
        """The weekly path had an identical hardcoded 'success'."""
        shift = seed["shift"]
        week_start = shift.date.isoformat()
        page = admin_client.post(
            "/admin/shifts/dispatch-week",
            data={"week_start": week_start},
            follow_redirects=True,
        ).get_data(as_text=True)

        assert "NOBODY WAS TEXTED" in page
        assert "border-rose-300" in page


class TestBroadcastResult:
    def test_failed_names_are_collected(self, app, seed, monkeypatch):
        from utils.sms import ShiftBroadcastDetails, WhatsAppService
        from models import ShiftAssignment

        service = WhatsAppService()
        monkeypatch.setattr(service, "_send", lambda to, **kw: False)

        shift = seed["shift"]
        details = ShiftBroadcastDetails(
            title=shift.job.title,
            location_address=shift.job.location_address,
            work_date=shift.date,
            start_time=shift.start_time,
            end_time=shift.end_time,
            estimated_hours=None,
        )
        result = service.send_shift_broadcast(ShiftAssignment.query.all(), details)

        assert isinstance(result, BroadcastResult)
        assert result.sent == 0
        assert result.failed == len(result.failed_names)
        assert set(result.failed_names) == {e.full_name for e in Employee.query.all()}

    def test_a_worker_with_no_phone_is_named_as_failed(self, app, seed, monkeypatch):
        """Skipped-for-no-phone used to increment the count with no name."""
        from utils.sms import ShiftBroadcastDetails, WhatsAppService
        from models import ShiftAssignment

        employee = Employee.query.first()
        employee.phone_number = ""
        db.session.commit()

        service = WhatsAppService()
        monkeypatch.setattr(service, "_send", lambda to, **kw: True)

        shift = seed["shift"]
        details = ShiftBroadcastDetails(
            title=shift.job.title,
            location_address=shift.job.location_address,
            work_date=shift.date,
            start_time=shift.start_time,
            end_time=shift.end_time,
            estimated_hours=None,
        )
        result = service.send_shift_broadcast(ShiftAssignment.query.all(), details)

        assert employee.full_name in result.failed_names
