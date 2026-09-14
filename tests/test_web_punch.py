"""Web PIN check-in: PIN hashing, /api/punch rules, lockout, SMS interplay."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import WORKER_PHONE
from extensions import db
from models import AssignmentStatus, Employee, PunchSource, ShiftAssignment, ShiftStatus
from utils import pins
from utils.punch import check_in_window_utc, handle_inbound_sms
from utils.web_punch import (
    LOCKOUT,
    MAX_PIN_ATTEMPTS,
    PunchState,
    ensure_punch_token,
    punch_state,
    web_punch,
)

PIN = "4821"


@pytest.fixture()
def pin_assignment(make_assignment, worker):
    """Accepted assignment on a shift that is live now; worker PIN = 4821."""
    worker.pin_hash = pins.hash_pin(worker.id, PIN)
    db.session.commit()
    assignment = make_assignment()
    ensure_punch_token(assignment)
    db.session.commit()
    return assignment


def post_punch(client, assignment_or_token, pin=PIN, action="in"):
    token = getattr(assignment_or_token, "punch_token", assignment_or_token)
    return client.post("/api/punch", json={"token": token, "pin": pin, "action": action})


# =========================================================================== #
# PIN primitives
# =========================================================================== #
class TestPins:
    def test_generated_pins_are_4_digits_and_never_weak(self):
        generated = {pins.generate_pin() for _ in range(3000)}
        assert all(pins.PIN_RE.match(p) for p in generated)
        assert not generated & pins._WEAK_PINS
        assert {"0000", "1234", "4321", "1111", "9012"} <= pins._WEAK_PINS

    def test_hash_is_keyed_per_employee(self):
        assert pins.hash_pin(1, "4821") != pins.hash_pin(2, "4821")  # same PIN, different workers
        assert pins.hash_pin(1, "4821") == pins.hash_pin(1, "4821")
        assert "4821" not in pins.hash_pin(1, "4821")

    def test_hash_depends_on_pepper(self, monkeypatch):
        before = pins.hash_pin(1, "4821")
        monkeypatch.setenv("PIN_PEPPER", "a-completely-different-pepper")
        assert pins.hash_pin(1, "4821") != before

    @pytest.mark.parametrize("pepper", [None, "", "short"])
    def test_missing_or_weak_pepper_fails_closed(self, monkeypatch, pepper):
        if pepper is None:
            monkeypatch.delenv("PIN_PEPPER")
        else:
            monkeypatch.setenv("PIN_PEPPER", pepper)
        with pytest.raises(pins.PinConfigError):
            pins.hash_pin(1, "4821")

    def test_verify(self, app, worker):
        assert pins.verify_pin(worker, PIN) is False  # no PIN set yet
        worker.pin_hash = pins.hash_pin(worker.id, PIN)
        assert pins.verify_pin(worker, PIN) is True
        assert pins.verify_pin(worker, "4822") is False
        assert pins.verify_pin(worker, "482") is False
        assert pins.verify_pin(worker, 4821) is False  # type: ignore[arg-type]

    def test_set_new_pin_stores_only_hash(self, app, worker):
        pin = pins.set_new_pin(worker)
        db.session.commit()
        assert worker.pin_hash != pin and len(worker.pin_hash) == 64
        assert worker.pin_set_at_utc is not None
        assert pins.verify_pin(worker, pin)


# =========================================================================== #
# /api/punch — request validation
# =========================================================================== #
class TestRequestValidation:
    def test_requires_json(self, client, pin_assignment):
        response = client.post("/api/punch", data={"token": pin_assignment.punch_token, "pin": PIN, "action": "in"})
        assert response.status_code == 415

    @pytest.mark.parametrize("payload", [
        {"pin": PIN, "action": "toggle"},
        {"pin": PIN},
        {"pin": "12", "action": "in"},
        {"pin": "12345", "action": "in"},
        {"pin": "12a4", "action": "in"},
        {"pin": 4821, "action": "in"},
        {"pin": None, "action": "in"},
    ])
    def test_malformed_payload_is_400_and_not_counted(self, client, pin_assignment, payload):
        response = client.post("/api/punch", json={"token": pin_assignment.punch_token, **payload})
        assert response.status_code == 400
        db.session.refresh(pin_assignment)
        assert pin_assignment.pin_failed_attempts == 0

    def test_non_object_json_is_400(self, client):
        assert client.post("/api/punch", json=["a", "b"]).status_code == 400

    @pytest.mark.parametrize("token", ["", "nope", "x" * 200])
    def test_unknown_token_is_404(self, client, pin_assignment, token):
        assert post_punch(client, token).status_code == 404

    def test_invite_token_is_not_a_punch_token(self, client, pin_assignment):
        """The invitation link token must not work as a punch credential."""
        assert post_punch(client, pin_assignment.token).status_code == 404

    def test_responses_are_private(self, client, pin_assignment):
        response = post_punch(client, pin_assignment)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"

    def test_csrf_exempt_with_csrf_enabled(self):
        from app import create_app
        from config import TestingConfig

        class CsrfOn(TestingConfig):
            WTF_CSRF_ENABLED = True

        app = create_app(CsrfOn)
        with app.app_context():
            db.create_all()
            response = app.test_client().post("/api/punch", json={"token": "none", "pin": PIN, "action": "in"})
            assert response.status_code == 404  # reached the view, not a 400 CSRF rejection
            db.session.remove()
            db.drop_all()

    def test_unknown_punch_page_is_404(self, client):
        response = client.get("/punch/not-a-real-token")
        assert response.status_code == 404
        assert response.headers["Cache-Control"] == "no-store"


# =========================================================================== #
# PIN verification & lockout
# =========================================================================== #
class TestPinLockout:
    def test_wrong_pin_counts_down(self, client, pin_assignment):
        for expected_left in range(MAX_PIN_ATTEMPTS - 1, 0, -1):
            response = post_punch(client, pin_assignment, pin="0001")
            assert response.status_code == 401
            assert response.get_json()["error"] == "wrong_pin"
            assert response.get_json()["attempts_left"] == expected_left
        db.session.refresh(pin_assignment)
        assert pin_assignment.check_in_at_utc is None

    def test_fifth_wrong_pin_locks_link(self, client, pin_assignment):
        for _ in range(MAX_PIN_ATTEMPTS - 1):
            post_punch(client, pin_assignment, pin="0001")
        response = post_punch(client, pin_assignment, pin="0001")
        assert response.status_code == 423
        assert int(response.headers["Retry-After"]) == int(LOCKOUT.total_seconds())
        assert response.get_json()["state"] == PunchState.LOCKED

    def test_correct_pin_rejected_while_locked(self, client, pin_assignment):
        for _ in range(MAX_PIN_ATTEMPTS):
            post_punch(client, pin_assignment, pin="0001")
        response = post_punch(client, pin_assignment, pin=PIN)
        assert response.status_code == 423
        db.session.refresh(pin_assignment)
        assert pin_assignment.check_in_at_utc is None

    def test_lock_expires(self, app, pin_assignment):
        opens_at, start, _end = check_in_window_utc(pin_assignment)
        now = start
        for _ in range(MAX_PIN_ATTEMPTS):
            web_punch(token=pin_assignment.punch_token, pin="0001", action="in", now_utc=now)
        assert web_punch(token=pin_assignment.punch_token, pin=PIN, action="in",
                         now_utc=now + LOCKOUT - timedelta(seconds=5)).http_status == 423
        result = web_punch(token=pin_assignment.punch_token, pin=PIN, action="in",
                           now_utc=now + LOCKOUT + timedelta(seconds=1))
        assert result.http_status == 200 and result.body["status"] == "checked_in"

    def test_correct_pin_resets_failure_count(self, client, pin_assignment):
        post_punch(client, pin_assignment, pin="0001")
        post_punch(client, pin_assignment, pin="0002")
        assert post_punch(client, pin_assignment).status_code == 200
        db.session.refresh(pin_assignment)
        assert pin_assignment.pin_failed_attempts == 0

    def test_lockout_is_per_link_not_per_worker(self, client, pin_assignment, make_assignment):
        other = make_assignment(start_in=timedelta(hours=30))
        ensure_punch_token(other)
        db.session.commit()
        for _ in range(MAX_PIN_ATTEMPTS):
            post_punch(client, pin_assignment, pin="0001")
        assert post_punch(client, other).get_json()["error"] == "too_early"  # PIN accepted, not locked

    def test_worker_without_pin_gets_wrong_pin_not_a_hint(self, client, make_assignment, worker):
        assignment = make_assignment()
        ensure_punch_token(assignment)
        db.session.commit()
        response = post_punch(client, assignment, pin="0000")
        assert response.status_code == 401 and response.get_json()["error"] == "wrong_pin"

    def test_another_workers_pin_does_not_work(self, client, pin_assignment):
        other = Employee(first_name="Other", last_name="W", phone_number="+13035550888")
        db.session.add(other)
        db.session.flush()
        other_pin = "7395"
        other.pin_hash = pins.hash_pin(other.id, other_pin)
        db.session.commit()
        assert post_punch(client, pin_assignment, pin=other_pin).status_code == 401

    def test_unconfigured_pepper_is_503(self, client, pin_assignment, monkeypatch):
        monkeypatch.delenv("PIN_PEPPER")
        assert post_punch(client, pin_assignment).status_code == 503


# =========================================================================== #
# Punch rules
# =========================================================================== #
class TestPunchRules:
    def test_check_in_stores_utc_and_source(self, app, pin_assignment):
        _opens, start, _end = check_in_window_utc(pin_assignment)
        now = start + timedelta(minutes=3)
        result = web_punch(token=pin_assignment.punch_token, pin=PIN, action="in", now_utc=now)
        assert result.http_status == 200
        assert result.body["status"] == "checked_in"
        assert result.body["state"] == PunchState.READY_OUT
        db.session.refresh(pin_assignment)
        assert pin_assignment.check_in_at_utc == now and pin_assignment.check_in_at_utc.tzinfo is None
        assert pin_assignment.check_in_source == PunchSource.WEB
        # Display time is venue-local, not UTC.
        local = datetime.combine(pin_assignment.shift.date, pin_assignment.shift.start_time) + timedelta(minutes=3)
        assert result.body["time"] == local.strftime("%I:%M %p").lstrip("0")

    def test_double_tap_in_never_overwrites(self, app, pin_assignment):
        _opens, start, _end = check_in_window_utc(pin_assignment)
        first = web_punch(token=pin_assignment.punch_token, pin=PIN, action="in", now_utc=start)
        second = web_punch(token=pin_assignment.punch_token, pin=PIN, action="in",
                           now_utc=start + timedelta(minutes=9))
        assert first.body["status"] == "checked_in"
        assert second.http_status == 200 and second.body["status"] == "already_checked_in"
        assert second.body["time"] == first.body["time"]
        db.session.refresh(pin_assignment)
        assert pin_assignment.check_in_at_utc == start

    def test_double_tap_in_is_never_treated_as_out(self, client, pin_assignment):
        post_punch(client, pin_assignment, action="in")
        post_punch(client, pin_assignment, action="in")
        db.session.refresh(pin_assignment)
        assert pin_assignment.check_out_at_utc is None

    def test_out_flow(self, app, pin_assignment):
        _opens, start, _end = check_in_window_utc(pin_assignment)
        web_punch(token=pin_assignment.punch_token, pin=PIN, action="in", now_utc=start)
        out = web_punch(token=pin_assignment.punch_token, pin=PIN, action="out",
                        now_utc=start + timedelta(hours=3, minutes=15))
        again = web_punch(token=pin_assignment.punch_token, pin=PIN, action="out",
                          now_utc=start + timedelta(hours=4))
        assert out.body["status"] == "checked_out" and out.body["hours"] == 3.25
        assert out.body["state"] == PunchState.DONE
        assert again.body["status"] == "already_checked_out" and again.body["time"] == out.body["time"]
        db.session.refresh(pin_assignment)
        assert pin_assignment.check_out_source == PunchSource.WEB
        assert pin_assignment.worked_hours == 3.25

    def test_out_before_in_is_409(self, client, pin_assignment):
        response = post_punch(client, pin_assignment, action="out")
        assert response.status_code == 409 and response.get_json()["error"] == "not_checked_in"

    def test_too_early(self, app, pin_assignment):
        opens_at, _start, _end = check_in_window_utc(pin_assignment)
        result = web_punch(token=pin_assignment.punch_token, pin=PIN, action="in",
                           now_utc=opens_at - timedelta(minutes=1))
        assert result.http_status == 409 and result.body["error"] == "too_early"
        assert result.body["opens_at"]

    def test_shift_ended(self, app, pin_assignment):
        _opens, _start, end = check_in_window_utc(pin_assignment)
        result = web_punch(token=pin_assignment.punch_token, pin=PIN, action="in",
                           now_utc=end + timedelta(minutes=1))
        assert result.http_status == 409 and result.body["error"] == "shift_ended"

    @pytest.mark.parametrize("assignment_status,shift_status", [
        (AssignmentStatus.cancelled, ShiftStatus.OPEN),
        (AssignmentStatus.accepted, ShiftStatus.CANCELLED),
        (AssignmentStatus.accepted, ShiftStatus.DRAFT),
    ])
    def test_closed_assignments(self, client, pin_assignment, assignment_status, shift_status):
        pin_assignment.status = assignment_status
        pin_assignment.shift.status = shift_status
        db.session.commit()
        response = post_punch(client, pin_assignment)
        assert response.status_code == 409 and response.get_json()["error"] == "not_available"

    def test_checked_in_elsewhere(self, app, make_assignment, worker):
        worker.pin_hash = pins.hash_pin(worker.id, PIN)
        first = make_assignment(start_in=timedelta(minutes=-60), hours=1)
        second = make_assignment(start_in=timedelta(minutes=0), hours=2)
        for a in (first, second):
            ensure_punch_token(a)
        db.session.commit()
        _o, first_start, _e = check_in_window_utc(first)
        web_punch(token=first.punch_token, pin=PIN, action="in", now_utc=first_start)
        result = web_punch(token=second.punch_token, pin=PIN, action="in",
                           now_utc=first_start + timedelta(minutes=59))
        assert result.http_status == 409 and result.body["error"] == "checked_in_elsewhere"


class TestSmsAndWebShareOneRecord:
    def test_sms_in_then_web_in_is_already_checked_in(self, app, pin_assignment):
        _o, start, _e = check_in_window_utc(pin_assignment)
        handle_inbound_sms(message_sid="SMx1", from_number=WORKER_PHONE, body="IN", now_utc=start)
        result = web_punch(token=pin_assignment.punch_token, pin=PIN, action="in",
                           now_utc=start + timedelta(minutes=2))
        assert result.body["status"] == "already_checked_in"
        db.session.refresh(pin_assignment)
        assert pin_assignment.check_in_source == PunchSource.SMS
        assert pin_assignment.check_in_at_utc == start

    def test_web_in_then_sms_out(self, app, pin_assignment):
        _o, start, _e = check_in_window_utc(pin_assignment)
        web_punch(token=pin_assignment.punch_token, pin=PIN, action="in", now_utc=start)
        outcome = handle_inbound_sms(message_sid="SMx2", from_number=WORKER_PHONE, body="OUT",
                                     now_utc=start + timedelta(hours=2))
        assert "Checked out" in outcome.reply
        db.session.refresh(pin_assignment)
        assert (pin_assignment.check_in_source, pin_assignment.check_out_source) == (PunchSource.WEB, PunchSource.SMS)


# =========================================================================== #
# Token issuance & admin
# =========================================================================== #
class TestIssuance:
    def test_accepting_invite_issues_punch_token(self, client, seed):
        ana = seed["assignments"][0]
        assert ana.punch_token is None
        client.post(f"/accept/{ana.token}")
        db.session.refresh(ana)
        assert ana.punch_token and ana.punch_token != ana.token

    def test_declining_issues_no_token(self, client, seed):
        ben = seed["assignments"][1]
        client.post(f"/decline/{ben.token}")
        db.session.refresh(ben)
        assert ben.punch_token is None

    def test_admin_add_worker_shows_pin_once_and_stores_hash(self, admin_client):
        response = admin_client.post("/admin/employees", data={
            "first_name": "Pia", "last_name": "Pin", "phone_number": "303-555-0911",
        }, follow_redirects=True)
        employee = Employee.query.filter_by(first_name="Pia").one()
        assert employee.pin_hash and len(employee.pin_hash) == 64
        import re
        shown = re.search(rb"check-in PIN is (\d{4})", response.data)
        assert shown and pins.verify_pin(employee, shown.group(1).decode())
        # Not shown again on the next page load.
        assert b"check-in PIN is" not in admin_client.get("/admin/employees").data

    def test_admin_reset_pin_invalidates_old_pin(self, admin_client, worker, monkeypatch):
        worker.pin_hash = pins.hash_pin(worker.id, PIN)
        db.session.commit()
        monkeypatch.setattr(pins, "generate_pin", lambda: "7395")  # deterministic new PIN
        response = admin_client.post(f"/admin/employees/{worker.id}/reset-pin", follow_redirects=True)
        db.session.refresh(worker)
        assert not pins.verify_pin(worker, PIN)
        assert pins.verify_pin(worker, "7395")
        assert b"check-in PIN is 7395" in response.data

    def test_admin_punch_link_only_for_confirmed(self, admin_client, make_assignment):
        pending = make_assignment(status=AssignmentStatus.pending)
        admin_client.post(f"/admin/assignments/{pending.id}/punch-link")
        db.session.refresh(pending)
        assert pending.punch_token is None
        accepted = make_assignment(start_in=timedelta(hours=5))
        response = admin_client.post(f"/admin/assignments/{accepted.id}/punch-link", follow_redirects=True)
        db.session.refresh(accepted)
        assert accepted.punch_token and accepted.punch_token.encode() in response.data

    def test_reminder_sms_includes_punch_link(self, app, make_assignment):
        from utils.alerts import send_shift_reminders

        class FakeService:
            base_url = "https://shifts.example.com"
            sent = []

            def send_shift_reminder(self, phone, **kwargs):
                self.sent.append(kwargs)
                return True

        assignment = make_assignment(start_in=timedelta(hours=3))
        from utils.timeutil import utc_naive_to_local
        from models import utcnow_naive
        service = FakeService()
        send_shift_reminders(now=utc_naive_to_local(utcnow_naive(), "America/Denver").replace(tzinfo=None), service=service)
        db.session.refresh(assignment)
        assert service.sent[0]["punch_url"] == f"https://shifts.example.com/punch/{assignment.punch_token}"

    def test_cli_issue_pins_outputs_csv_once(self, app, worker):
        runner = app.test_cli_runner()
        result = runner.invoke(args=["admin", "issue-pins"])
        assert result.exit_code == 0, result.output
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert lines[0] == "name,phone_number,pin"
        name, phone, pin = lines[1].split(",")
        db.session.refresh(worker)
        assert phone == WORKER_PHONE and pins.verify_pin(worker, pin)
        again = runner.invoke(args=["admin", "issue-pins"])
        assert "No workers need a PIN" in again.output


class TestPunchPage:
    def test_ready_in_renders_keypad(self, client, pin_assignment):
        response = client.get(f"/punch/{pin_assignment.punch_token}")
        page = response.data.decode()
        assert response.status_code == 200
        assert "CHECK IN" in page and 'id="keypad"' in page
        assert page.count('data-digit="') == 10
        assert '"in"' in page  # ACTION constant
        assert response.headers["Cache-Control"] == "no-store"

    def test_ready_out_renders_out_keypad(self, client, pin_assignment):
        _o, start, _e = check_in_window_utc(pin_assignment)
        pin_assignment.check_in_at_utc = start
        db.session.commit()
        page = client.get(f"/punch/{pin_assignment.punch_token}").data.decode()
        assert "CHECK OUT" in page and "In at" in page and '"out"' in page

    @pytest.mark.parametrize("mutate,expected", [
        (lambda a, s, e: (setattr(a, "check_in_at_utc", s), setattr(a, "check_out_at_utc", e)), "DONE"),
        (lambda a, s, e: setattr(a, "pin_locked_until_utc", e + timedelta(days=1)), "LOCKED"),
        (lambda a, s, e: setattr(a, "status", AssignmentStatus.cancelled), "NOT AVAILABLE"),
    ])
    def test_non_actionable_states_have_no_keypad(self, client, pin_assignment, mutate, expected):
        _o, start, end = check_in_window_utc(pin_assignment)
        mutate(pin_assignment, start, end)
        db.session.commit()
        page = client.get(f"/punch/{pin_assignment.punch_token}").data.decode()
        assert expected in page
        assert 'id="keypad"' not in page and "/api/punch" not in page

    def test_too_early_state(self, client, make_assignment, worker):
        assignment = make_assignment(start_in=timedelta(hours=6))
        ensure_punch_token(assignment)
        db.session.commit()
        page = client.get(f"/punch/{assignment.punch_token}").data.decode()
        assert "TOO EARLY" in page and "Opens" in page and 'id="keypad"' not in page

    def test_job_title_is_escaped(self, client, pin_assignment):
        pin_assignment.shift.job.title = '</script><script>alert(1)</script>'
        db.session.commit()
        page = client.get(f"/punch/{pin_assignment.punch_token}").data.decode()
        assert "<script>alert(1)</script>" not in page


def test_punch_state_machine(app, pin_assignment):
    opens_at, start, end = check_in_window_utc(pin_assignment)
    assert punch_state(pin_assignment, opens_at - timedelta(seconds=1)) == PunchState.TOO_EARLY
    assert punch_state(pin_assignment, start) == PunchState.READY_IN
    assert punch_state(pin_assignment, end + timedelta(seconds=1)) == PunchState.ENDED
    pin_assignment.check_in_at_utc = start
    assert punch_state(pin_assignment, end + timedelta(hours=5)) == PunchState.READY_OUT
    pin_assignment.check_out_at_utc = end
    assert punch_state(pin_assignment, end) == PunchState.DONE
    pin_assignment.pin_locked_until_utc = end + timedelta(minutes=5)
    assert punch_state(pin_assignment, end) == PunchState.LOCKED
