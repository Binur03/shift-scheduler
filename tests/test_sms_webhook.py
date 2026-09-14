"""Inbound SMS webhook: signature security, malformed replies, punch rules.

These run on SQLite. True concurrent row-lock behaviour needs InnoDB and is
covered by test_punch_concurrency_mysql.py.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import datetime, time, timedelta

import pytest

from conftest import (
    SMS_PATH,
    TWILIO_TEST_TOKEN,
    WORKER_PHONE,
    sms_params,
    twilio_signature,
)
from extensions import db
from models import (
    AssignmentStatus,
    Employee,
    InboundSms,
    PunchResult,
    Shift,
    ShiftAssignment,
    ShiftStatus,
)
from utils import punch
from utils.punch import handle_inbound_sms, parse_command
from utils.timeutil import local_to_utc_naive


def reply_text(response) -> str | None:
    """The <Message> text from a TwiML response (None when no message)."""
    assert response.mimetype == "application/xml"
    message = ET.fromstring(response.data).find("Message")
    return None if message is None else message.text


# =========================================================================== #
# Signature validation (spoofing)
# =========================================================================== #
class TestTwilioSignature:
    def test_valid_signature_accepted(self, post_sms, make_assignment):
        make_assignment()
        response = post_sms("IN")
        assert response.status_code == 200
        assert "Checked in" in reply_text(response)

    def test_missing_signature_rejected(self, post_sms, make_assignment):
        assignment = make_assignment()
        assert post_sms("IN", signed=False).status_code == 403
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc is None
        assert InboundSms.query.count() == 0  # rejected before any processing

    def test_signature_from_wrong_token_rejected(self, app, client):
        params = sms_params("IN")
        forged = twilio_signature(app, params, token="attacker-guessed-token")
        response = client.post(SMS_PATH, data=params, headers={"X-Twilio-Signature": forged})
        assert response.status_code == 403

    @pytest.mark.parametrize("field,value", [
        ("Body", "OUT"),                 # attacker flips the command
        ("From", "+19995550000"),        # attacker impersonates another worker
        ("MessageSid", "SMforged"),      # attacker replays with a new id
    ])
    def test_tampered_field_rejected(self, app, client, field, value):
        params = sms_params("IN")
        signature = twilio_signature(app, params)
        tampered = dict(params, **{field: value})
        response = client.post(SMS_PATH, data=tampered, headers={"X-Twilio-Signature": signature})
        assert response.status_code == 403

    def test_extra_injected_param_rejected(self, app, client):
        params = sms_params("IN")
        signature = twilio_signature(app, params)
        response = client.post(
            SMS_PATH, data=dict(params, Extra="1"), headers={"X-Twilio-Signature": signature}
        )
        assert response.status_code == 403

    def test_signature_for_different_url_rejected(self, app, client):
        params = sms_params("IN")
        signature = twilio_signature(app, params, path="/webhooks/twilio/other")
        response = client.post(SMS_PATH, data=params, headers={"X-Twilio-Signature": signature})
        assert response.status_code == 403

    def test_foreign_account_sid_rejected(self, app, client):
        # Correctly signed with our token, but for another Twilio account.
        params = sms_params("IN", account_sid="ACsomeoneelse000000000000000000")
        response = client.post(
            SMS_PATH, data=params, headers={"X-Twilio-Signature": twilio_signature(app, params)}
        )
        assert response.status_code == 403

    def test_unconfigured_auth_token_fails_closed(self, app, client, monkeypatch):
        monkeypatch.delenv("TWILIO_AUTH_TOKEN")
        params = sms_params("IN")
        headers = {"X-Twilio-Signature": twilio_signature(app, params, token=TWILIO_TEST_TOKEN)}
        assert client.post(SMS_PATH, data=params, headers=headers).status_code == 503

    def test_get_not_allowed(self, client):
        assert client.get(SMS_PATH).status_code == 405

    def test_signed_but_missing_message_sid_is_400(self, app, client):
        params = sms_params("IN")
        del params["MessageSid"]
        response = client.post(
            SMS_PATH, data=params, headers={"X-Twilio-Signature": twilio_signature(app, params)}
        )
        assert response.status_code == 400

    def test_webhook_is_csrf_exempt_in_production_config(self, monkeypatch):
        """TestingConfig disables CSRF globally, so prove the exemption with it on."""
        from app import create_app
        from config import TestingConfig

        class CsrfOn(TestingConfig):
            WTF_CSRF_ENABLED = True

        app = create_app(CsrfOn)
        with app.app_context():
            db.create_all()
            params = sms_params("hello")
            response = app.test_client().post(
                SMS_PATH, data=params, headers={"X-Twilio-Signature": twilio_signature(app, params)}
            )
            assert response.status_code == 200  # not a 400 CSRF failure
            db.session.remove()
            db.drop_all()


# =========================================================================== #
# Command parsing & malformed replies
# =========================================================================== #
class TestCommandParsing:
    @pytest.mark.parametrize("body,expected", [
        ("IN", "IN"), ("in", "IN"), ("  In  ", "IN"), ("IN.", "IN"), ("in!!", "IN"),
        ("check in", "IN"), ("Check-In", "IN"), ("CLOCKIN", "IN"), ("clock in.", "IN"),
        ("OUT", "OUT"), ("out", "OUT"), ("Check out", "OUT"), ("clock-out!", "OUT"),
        ("\nOUT\n", "OUT"),
    ])
    def test_accepted_variants(self, body, expected):
        assert parse_command(body) == expected

    @pytest.mark.parametrize("body", [
        "", "   ", "I'm in", "INN", "INSIDE", "OUTSIDE", "in out", "IN OUT", "1N", "0UT",
        "checking in", "in 5 min", "IN?", "ÍN", "IN 🏟️", "yes in", "clock", "punch in",
        "IN\nOUT", "ＩＮ",  # full-width letters
    ])
    def test_malformed_variants_rejected(self, body):
        assert parse_command(body) is None

    @pytest.mark.parametrize("body", ["STOP", "stop", "HELP", "START", "unsubscribe", "Cancel."])
    def test_carrier_keywords(self, body):
        assert parse_command(body) == "OPT"

    @pytest.mark.parametrize("body", ["I'm in", "hello?", "INN", "🏟️", "IN OUT"])
    def test_malformed_sms_gets_help_reply_and_is_logged(self, post_sms, worker, body):
        response = post_sms(body)
        assert response.status_code == 200  # never 4xx/5xx: Twilio must not retry
        assert "Text IN" in reply_text(response)
        log = InboundSms.query.one()
        assert log.result == PunchResult.MALFORMED
        assert log.employee_id == worker.id

    def test_empty_body_is_malformed(self, post_sms, worker):
        response = post_sms("")
        assert response.status_code == 200
        assert InboundSms.query.one().result == PunchResult.MALFORMED

    def test_oversized_body_truncated_not_crashed(self, post_sms, worker):
        response = post_sms("A" * 5000)
        assert response.status_code == 200
        assert len(InboundSms.query.one().body) == punch.MAX_BODY_CHARS

    def test_non_ascii_body_logged_intact(self, post_sms, worker):
        body = "¿Dónde está mi turno? 🏟️"
        post_sms(body)
        assert InboundSms.query.one().body == body

    def test_stop_keyword_sends_no_reply(self, post_sms, worker):
        response = post_sms("STOP")
        assert response.status_code == 200
        assert reply_text(response) is None
        assert InboundSms.query.one().result == PunchResult.OPT_KEYWORD

    def test_unknown_sender_gets_generic_reply(self, post_sms, make_assignment):
        response = post_sms("IN", from_="+19995550123")
        text = reply_text(response)
        assert "isn't registered" in text
        assert "Concessions" not in text  # leaks nothing about shifts
        assert InboundSms.query.one().result == PunchResult.UNKNOWN_SENDER

    def test_inactive_worker_treated_as_unknown(self, post_sms, make_assignment, worker):
        make_assignment()
        worker.is_active = False
        db.session.commit()
        assert "isn't registered" in reply_text(post_sms("IN"))


# =========================================================================== #
# Punch state machine
# =========================================================================== #
class TestPunchRules:
    def test_in_then_out_records_utc_and_hours(self, make_assignment):
        assignment = make_assignment()
        shift = assignment.shift
        start_utc = local_to_utc_naive(datetime.combine(shift.date, shift.start_time), "America/Denver")
        t_in = start_utc + timedelta(minutes=5)
        t_out = t_in + timedelta(hours=3, minutes=30)

        first = handle_inbound_sms(message_sid="SM1", from_number=WORKER_PHONE, body="IN", now_utc=t_in)
        second = handle_inbound_sms(message_sid="SM2", from_number=WORKER_PHONE, body="OUT", now_utc=t_out)

        db.session.refresh(assignment)
        assert first.result == PunchResult.CHECKED_IN
        assert second.result == PunchResult.CHECKED_OUT
        assert assignment.check_in_at_utc == t_in      # stored exactly, naive UTC
        assert assignment.check_out_at_utc == t_out
        assert assignment.check_in_at_utc.tzinfo is None
        assert assignment.worked_hours == 3.5
        assert assignment.check_in_message_sid == "SM1"
        assert assignment.check_out_message_sid == "SM2"
        assert "3.5 hrs" in second.reply

    def test_reply_shows_venue_local_time_not_utc(self, make_assignment):
        assignment = make_assignment()
        shift = assignment.shift
        start_utc = local_to_utc_naive(datetime.combine(shift.date, shift.start_time), "America/Denver")
        outcome = handle_inbound_sms(message_sid="SMtz", from_number=WORKER_PHONE, body="IN", now_utc=start_utc)
        local_clock = shift.start_time.strftime("%I:%M %p").lstrip("0")
        assert local_clock in outcome.reply

    def test_double_in_is_duplicate_and_keeps_first_time(self, post_sms, make_assignment):
        assignment = make_assignment()
        post_sms("IN")
        db.session.refresh(assignment)
        first_time = assignment.check_in_at_utc

        response = post_sms("in")
        assert "already checked in" in reply_text(response)
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc == first_time
        assert [r.result for r in InboundSms.query.order_by(InboundSms.id)] == [
            PunchResult.CHECKED_IN, PunchResult.DUPLICATE,
        ]

    def test_out_without_in(self, post_sms, make_assignment):
        make_assignment()
        response = post_sms("OUT")
        assert "not checked in" in reply_text(response)
        assert InboundSms.query.one().result == PunchResult.NO_SHIFT

    def test_double_out_is_duplicate(self, post_sms, make_assignment):
        assignment = make_assignment()
        post_sms("IN")
        post_sms("OUT")
        db.session.refresh(assignment)
        out_time = assignment.check_out_at_utc
        response = post_sms("OUT")
        assert "already checked out" in reply_text(response)
        db.session.refresh(assignment)
        assert assignment.check_out_at_utc == out_time

    @pytest.mark.parametrize("minutes_before,eligible", [
        (61, False),   # just outside the 60-minute early window
        (60, True),    # boundary is inclusive
        (15, True),
    ])
    def test_early_check_in_window(self, make_assignment, minutes_before, eligible):
        assignment = make_assignment()
        shift = assignment.shift
        start_utc = local_to_utc_naive(datetime.combine(shift.date, shift.start_time), "America/Denver")
        outcome = handle_inbound_sms(
            message_sid=f"SMe{minutes_before}", from_number=WORKER_PHONE, body="IN",
            now_utc=start_utc - timedelta(minutes=minutes_before),
        )
        assert (outcome.result == PunchResult.CHECKED_IN) is eligible

    def test_early_window_is_configurable(self, make_assignment, monkeypatch):
        monkeypatch.setenv("PUNCH_EARLY_MINUTES", "15")
        assignment = make_assignment()
        shift = assignment.shift
        start_utc = local_to_utc_naive(datetime.combine(shift.date, shift.start_time), "America/Denver")
        outcome = handle_inbound_sms(
            message_sid="SMcfg", from_number=WORKER_PHONE, body="IN",
            now_utc=start_utc - timedelta(minutes=30),
        )
        assert outcome.result == PunchResult.NO_SHIFT
        assert "15 min" in outcome.reply

    def test_no_check_in_after_shift_ended(self, make_assignment):
        assignment = make_assignment()
        shift = assignment.shift
        start_utc = local_to_utc_naive(datetime.combine(shift.date, shift.start_time), "America/Denver")
        outcome = handle_inbound_sms(
            message_sid="SMlate", from_number=WORKER_PHONE, body="IN",
            now_utc=start_utc + timedelta(hours=4, minutes=1),
        )
        assert outcome.result == PunchResult.NO_SHIFT

    def test_overnight_shift_check_in_after_midnight(self, app, venue_job, worker):
        # 22:00 -> 06:00 local; worker texts IN at 00:30 the next local day.
        shift = Shift(job_id=venue_job.id, date=datetime(2031, 3, 10).date(),
                      start_time=time(22, 0), end_time=time(6, 0), status=ShiftStatus.OPEN)
        db.session.add(shift)
        db.session.flush()
        assignment = ShiftAssignment(shift_id=shift.id, employee_id=worker.id,
                                     status=AssignmentStatus.accepted, token="overnight-token")
        db.session.add(assignment)
        db.session.commit()

        now_utc = local_to_utc_naive(datetime(2031, 3, 11, 0, 30), "America/Denver")
        outcome = handle_inbound_sms(message_sid="SMnight", from_number=WORKER_PHONE, body="IN", now_utc=now_utc)
        assert outcome.result == PunchResult.CHECKED_IN

    @pytest.mark.parametrize("status,shift_status", [
        (AssignmentStatus.pending, ShiftStatus.OPEN),      # never confirmed
        (AssignmentStatus.rejected, ShiftStatus.OPEN),
        (AssignmentStatus.cancelled, ShiftStatus.OPEN),    # removed by manager
        (AssignmentStatus.accepted, ShiftStatus.DRAFT),    # unapproved vendor draft
        (AssignmentStatus.accepted, ShiftStatus.CANCELLED),
    ])
    def test_ineligible_assignments_cannot_check_in(self, post_sms, make_assignment, status, shift_status):
        assignment = make_assignment(status=status, shift_status=shift_status)
        response = post_sms("IN")
        assert "No shift found" in reply_text(response)
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc is None

    def test_other_workers_shift_not_punched(self, post_sms, make_assignment):
        other = Employee(first_name="Other", last_name="Worker", phone_number="+13035550777")
        db.session.add(other)
        db.session.commit()
        assignment = make_assignment(employee=other)
        post_sms("IN")  # sent from WORKER_PHONE, not the other worker's phone
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc is None

    def test_back_to_back_shifts_require_out_first(self, make_assignment):
        first = make_assignment(start_in=timedelta(minutes=-60), hours=1)
        second = make_assignment(start_in=timedelta(minutes=0), hours=2)
        first_start = local_to_utc_naive(
            datetime.combine(first.shift.date, first.shift.start_time), "America/Denver")

        a = handle_inbound_sms(message_sid="SMb1", from_number=WORKER_PHONE, body="IN", now_utc=first_start)
        b = handle_inbound_sms(message_sid="SMb2", from_number=WORKER_PHONE, body="IN",
                               now_utc=first_start + timedelta(minutes=55))
        c = handle_inbound_sms(message_sid="SMb3", from_number=WORKER_PHONE, body="OUT",
                               now_utc=first_start + timedelta(minutes=58))
        d = handle_inbound_sms(message_sid="SMb4", from_number=WORKER_PHONE, body="IN",
                               now_utc=first_start + timedelta(minutes=59))

        assert (a.result, b.result, c.result, d.result) == (
            PunchResult.CHECKED_IN, PunchResult.DUPLICATE, PunchResult.CHECKED_OUT, PunchResult.CHECKED_IN,
        )
        assert a.assignment_id == first.id and d.assignment_id == second.id


# =========================================================================== #
# Idempotency: Twilio retries
# =========================================================================== #
class TestIdempotency:
    def test_retry_with_same_message_sid_replays_reply(self, post_sms, make_assignment):
        assignment = make_assignment()
        first = post_sms("IN", sid="SMretry1")
        db.session.refresh(assignment)
        check_in = assignment.check_in_at_utc

        retry = post_sms("IN", sid="SMretry1")
        assert reply_text(retry) == reply_text(first)
        assert "Checked in" in reply_text(retry)  # not "already checked in"
        assert InboundSms.query.filter_by(message_sid="SMretry1").count() == 1
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc == check_in

    def test_concurrent_duplicate_insert_race_returns_original(self, make_assignment, monkeypatch):
        """Simulate the retry that races the original past the dedupe check.

        The first lookup is forced to miss, so processing proceeds and the
        log insert collides on the unique MessageSid — the IntegrityError
        path must roll back and return the original stored reply.
        """
        assignment = make_assignment()
        shift = assignment.shift
        start_utc = local_to_utc_naive(datetime.combine(shift.date, shift.start_time), "America/Denver")
        original = handle_inbound_sms(message_sid="SMrace", from_number=WORKER_PHONE, body="IN", now_utc=start_utc)

        real_prior = punch._prior_outcome
        calls = {"n": 0}

        def miss_first(message_sid):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_prior(message_sid)

        monkeypatch.setattr(punch, "_prior_outcome", miss_first)
        replay = handle_inbound_sms(message_sid="SMrace", from_number=WORKER_PHONE, body="IN",
                                    now_utc=start_utc + timedelta(seconds=2))

        assert replay == original
        assert InboundSms.query.filter_by(message_sid="SMrace").count() == 1
        db.session.refresh(assignment)
        assert assignment.check_in_at_utc == start_utc

    def test_deadlock_is_retried(self, make_assignment, monkeypatch):
        """An InnoDB deadlock (1213) on the first attempt is retried transparently."""
        from sqlalchemy.exc import OperationalError

        assignment = make_assignment()
        shift = assignment.shift
        start_utc = local_to_utc_naive(datetime.combine(shift.date, shift.start_time), "America/Denver")

        real = punch._process_once
        attempts = {"n": 0}

        def deadlock_once(**kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OperationalError("SELECT ... FOR UPDATE", {}, Exception(1213, "Deadlock found"))
            return real(**kwargs)

        monkeypatch.setattr(punch, "_process_once", deadlock_once)
        monkeypatch.setattr(punch.time, "sleep", lambda _s: None)
        outcome = handle_inbound_sms(message_sid="SMdead", from_number=WORKER_PHONE, body="IN", now_utc=start_utc)
        assert outcome.result == PunchResult.CHECKED_IN
        assert attempts["n"] == 2

    def test_non_retryable_db_error_propagates(self, make_assignment, monkeypatch):
        from sqlalchemy.exc import OperationalError

        make_assignment()

        def db_down(**_kwargs):
            raise OperationalError("SELECT 1", {}, Exception(2003, "Can't connect"))

        monkeypatch.setattr(punch, "_process_once", db_down)
        with pytest.raises(OperationalError):
            handle_inbound_sms(message_sid="SMdown", from_number=WORKER_PHONE, body="IN")
